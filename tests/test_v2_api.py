from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    Asset,
    AssetLocation,
    AssetSource,
    AuthUser,
    ChangeEvent,
    ChangeEventWork,
    ChorusMixVariant,
    ChorusProject,
    ChorusTimeline,
    ChorusTrack,
    Contributor,
    Release,
    ReleaseItem,
    Rendition,
    RenditionAsset,
    RenditionCredit,
    Score,
    ScoreRenditionSync,
    ScoreRevision,
    ScoreRevisionAsset,
    UploadSession,
    Work,
    WorkCredit,
    utc_now,
)
from rhythm_metadata_api.main import create_app

TOKEN = "test-private-catalog-token"
AUTH = {
    "Authorization": f"Bearer {TOKEN}",
    "X-Device-ID": "android-test-device",
}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        bootstrap_token=TOKEN,
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def post(client: TestClient, path: str, key: str, json: dict[str, object]):
    return client.post(path, headers={**AUTH, "Idempotency-Key": key}, json=json)


def upload_asset(
    client: TestClient,
    *,
    key: str,
    content: bytes,
    media_type: str,
    filename: str,
) -> dict[str, object]:
    digest = hashlib.sha256(content).hexdigest()
    created = post(
        client,
        "/v2/uploads",
        f"{key}-create",
        {
            "sha256": digest,
            "byte_size": len(content),
            "media_type": media_type,
            "original_filename": filename,
            "source": "saf_import",
        },
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload"]["id"]
    written = client.put(
        f"/v2/uploads/{upload_id}/content",
        headers={**AUTH, "Content-Type": "application/octet-stream"},
        content=content,
    )
    assert written.status_code == 200, written.text
    completed = post(client, f"/v2/uploads/{upload_id}/complete", f"{key}-complete", {})
    assert completed.status_code == 200, completed.text
    return completed.json()["asset"]


def test_create_score_with_initial_revision_is_atomic_and_idempotent(
    client: TestClient,
) -> None:
    work = post(client, "/v2/works", "atomic-work", {"canonical_title": "Amazing Grace"})
    assert work.status_code == 201
    arrangement = post(
        client,
        f"/v2/works/{work.json()['id']}/arrangements",
        "atomic-arrangement",
        {"name": "SATB", "voicing": "SATB"},
    )
    assert arrangement.status_code == 201
    asset = upload_asset(
        client,
        key="atomic-score-file",
        content=b"<score-partwise version='4.0'><part-list/></score-partwise>",
        media_type="application/vnd.recordare.musicxml+xml",
        filename="amazing-grace.musicxml",
    )
    payload = {
        "label": "SATB total score",
        "origin": "external_import",
        "edit_message": "Initial reviewed score",
        "assets": [{"asset_id": asset["id"], "role": "primary_musicxml"}],
        "publish": True,
        "preferred": True,
    }
    created = post(
        client,
        f"/v2/arrangements/{arrangement.json()['id']}/scores-with-revision",
        "atomic-score",
        payload,
    )
    assert created.status_code == 201, created.text
    assert created.headers["etag"] == '"rev-1"'
    result = created.json()
    assert result["score"]["head_revision_id"] == result["revision"]["id"]
    assert result["score"]["published_revision_id"] == result["revision"]["id"]
    assert result["revision"]["revision_no"] == 1
    assert result["arrangement"]["preferred_score_id"] == result["score"]["id"]

    replay = post(
        client,
        f"/v2/arrangements/{arrangement.json()['id']}/scores-with-revision",
        "atomic-score",
        payload,
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["score"]["id"] == result["score"]["id"]

    with Session(client.app.state.v2_container.engine) as session:
        assert session.query(Score).count() == 1
        assert session.query(ScoreRevision).count() == 1
        assert session.query(ScoreRevisionAsset).count() == 1

    invalid = post(
        client,
        f"/v2/arrangements/{arrangement.json()['id']}/scores-with-revision",
        "atomic-score-invalid",
        {**payload, "publish": False, "preferred": True},
    )
    assert invalid.status_code == 422


def test_delete_score_removes_its_branch_and_exclusive_asset(client: TestClient) -> None:
    work = post(client, "/v2/works", "delete-work", {"canonical_title": "Bad score work"})
    arrangement = post(
        client,
        f"/v2/works/{work.json()['id']}/arrangements",
        "delete-arrangement",
        {"name": "SATB", "voicing": "SATB"},
    )
    asset = upload_asset(
        client,
        key="delete-score-file",
        content=b"<score-partwise version='4.0'><part-list/></score-partwise>",
        media_type="application/vnd.recordare.musicxml+xml",
        filename="bad.musicxml",
    )
    created = post(
        client,
        f"/v2/arrangements/{arrangement.json()['id']}/scores-with-revision",
        "delete-score",
        {
            "label": "Wrong score",
            "origin": "external_import",
            "assets": [{"asset_id": asset["id"], "role": "primary_musicxml"}],
            "publish": True,
            "preferred": True,
        },
    )
    assert created.status_code == 201, created.text
    score = created.json()["score"]

    with Session(client.app.state.v2_container.engine) as session:
        location = session.query(AssetLocation).filter_by(asset_id=asset["id"]).one()
        object_path = Path(client.app.state.v2_container.settings.local_object_root) / location.storage_key
    assert object_path.is_file()

    impact = client.get(f"/v2/scores/{score['id']}/delete-impact", headers=AUTH)
    assert impact.status_code == 200, impact.text
    assert impact.json() == {
        "score_id": score["id"],
        "work_id": work.json()["id"],
        "arrangement_id": arrangement.json()["id"],
        "revisions": 1,
        "score_file_links": 1,
        "lyric_source_links": 0,
        "sync_anchors": 0,
        "detached_derived_scores": 0,
        "chorus_projects": 0,
        "chorus_timelines": 0,
        "chorus_tracks": 0,
        "upload_sessions": 0,
        "chorus_renditions": 0,
        "release_items": 0,
        "candidate_assets": 1,
        "garbage_collect_assets": 1,
        "shared_assets": 0,
        "clears_preferred_score": True,
    }

    stale = client.delete(
        f"/v2/scores/{score['id']}",
        headers={**AUTH, "If-Match": '"rev-99"'},
    )
    assert stale.status_code == 412

    deleted = client.delete(
        f"/v2/scores/{score['id']}",
        headers={**AUTH, "If-Match": '"rev-1"'},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
    assert deleted.json()["garbage_collected_assets"] == 1
    assert deleted.json()["pending_asset_cleanup"] == 0
    assert client.get(f"/v2/scores/{score['id']}", headers=AUTH).status_code == 404
    assert client.get(f"/v2/works/{work.json()['id']}", headers=AUTH).status_code == 200
    assert not object_path.exists()

    with Session(client.app.state.v2_container.engine) as session:
        kept_arrangement = session.get(Arrangement, arrangement.json()["id"])
        assert kept_arrangement is not None
        assert kept_arrangement.preferred_score_id is None
        assert session.get(Asset, asset["id"]) is None
        assert session.query(AssetLocation).filter_by(asset_id=asset["id"]).count() == 0
        assert session.query(AssetSource).filter_by(asset_id=asset["id"]).count() == 0
        event = (
            session.query(ChangeEvent)
            .filter_by(entity_id=score["id"], operation="score.deleted")
            .one()
        )
        assert event.tombstone is True
        assert (
            session.query(ChangeEventWork)
            .filter_by(event_sequence=event.sequence, work_id=work.json()["id"])
            .count()
            == 1
        )


def test_delete_score_preserves_shared_and_sibling_resources(client: TestClient) -> None:
    work = post(client, "/v2/works", "shared-work", {"canonical_title": "Shared work"})
    arrangement = post(
        client,
        f"/v2/works/{work.json()['id']}/arrangements",
        "shared-arrangement",
        {"name": "SATB", "voicing": "SATB"},
    )
    asset = upload_asset(
        client,
        key="shared-score-file",
        content=b"<score-partwise version='4.0'><part-list/></score-partwise>",
        media_type="application/vnd.recordare.musicxml+xml",
        filename="shared.musicxml",
    )
    target = post(
        client,
        f"/v2/arrangements/{arrangement.json()['id']}/scores-with-revision",
        "shared-target-score",
        {
            "label": "Bad score",
            "origin": "external_import",
            "assets": [{"asset_id": asset["id"], "role": "primary_musicxml"}],
        },
    ).json()
    sibling = post(
        client,
        f"/v2/arrangements/{arrangement.json()['id']}/scores-with-revision",
        "shared-sibling-score",
        {
            "label": "Good score",
            "origin": "manual",
            "derived_from_revision_id": target["revision"]["id"],
            "assets": [{"asset_id": asset["id"], "role": "primary_musicxml"}],
        },
    ).json()
    with Session(client.app.state.v2_container.engine) as session, session.begin():
        rendition = Rendition(
            arrangement_id=arrangement.json()["id"],
            label="Ordinary reference recording",
            kind="reference",
        )
        session.add(rendition)
        session.flush()
        rendition_id = rendition.id
        session.add(
            ScoreRenditionSync(
                score_revision_id=target["revision"]["id"],
                rendition_id=rendition.id,
                anchor_order=0,
                score_tick=0,
                media_ms=0,
                confidence_milli=1000,
                source="manual",
            )
        )

    impact = client.get(
        f"/v2/scores/{target['score']['id']}/delete-impact", headers=AUTH
    )
    assert impact.status_code == 200
    assert impact.json()["shared_assets"] == 1
    assert impact.json()["garbage_collect_assets"] == 0
    assert impact.json()["detached_derived_scores"] == 1
    assert impact.json()["sync_anchors"] == 1

    deleted = client.delete(
        f"/v2/scores/{target['score']['id']}",
        headers={**AUTH, "If-Match": '"rev-1"'},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["garbage_collected_assets"] == 0

    with Session(client.app.state.v2_container.engine) as session:
        kept_score = session.get(Score, sibling["score"]["id"])
        assert kept_score is not None
        assert kept_score.derived_from_revision_id is None
        assert session.get(Rendition, rendition_id) is not None
        assert session.query(ScoreRenditionSync).count() == 0
        assert session.get(Asset, asset["id"]) is not None
        assert (
            session.query(ScoreRevisionAsset)
            .filter_by(score_revision_id=sibling["revision"]["id"], asset_id=asset["id"])
            .count()
            == 1
        )


def test_delete_score_removes_dependent_chorus_branch(client: TestClient) -> None:
    work = post(client, "/v2/works", "chorus-delete-work", {"canonical_title": "Chorus"})
    arrangement = post(
        client,
        f"/v2/works/{work.json()['id']}/arrangements",
        "chorus-delete-arrangement",
        {"name": "Choir", "voicing": "SATB"},
    )
    asset = upload_asset(
        client,
        key="chorus-delete-file",
        content=b"<score-partwise version='4.0'><part-list/></score-partwise>",
        media_type="application/vnd.recordare.musicxml+xml",
        filename="chorus.musicxml",
    )
    created = post(
        client,
        f"/v2/arrangements/{arrangement.json()['id']}/scores-with-revision",
        "chorus-delete-score",
        {
            "label": "Wrong chorus score",
            "origin": "external_import",
            "assets": [{"asset_id": asset["id"], "role": "primary_musicxml"}],
        },
    ).json()

    with Session(client.app.state.v2_container.engine) as session, session.begin():
        upload = session.query(UploadSession).filter_by(completed_asset_id=asset["id"]).one()
        user = AuthUser(id="chorus-delete-user", display_name="Chorus singer")
        session.add(user)
        project = ChorusProject(
            work_id=work.json()["id"],
            arrangement_id=arrangement.json()["id"],
            score_id=created["score"]["id"],
            alignment_score_revision_id=created["revision"]["id"],
            timeline_hash=asset["sha256"],
            title="Bad chorus project",
            status="open",
            created_by_user_id=user.id,
        )
        session.add(project)
        session.flush()
        timeline = ChorusTimeline(
            chorus_project_id=project.id,
            score_revision_id=created["revision"]["id"],
            timeline_hash=asset["sha256"],
        )
        rendition = Rendition(
            arrangement_id=arrangement.json()["id"],
            label="Chorus submission",
            kind="chorus_track",
        )
        release = Release(key="chorus-delete-release", title="Kept release")
        session.add_all([timeline, rendition, release])
        session.flush()
        track = ChorusTrack(
            chorus_project_id=project.id,
            chorus_timeline_id=timeline.id,
            rendition_id=rendition.id,
            uploader_user_id=user.id,
            upload_session_id=upload.id,
            contribution_kind="vocal_part",
            display_label="Soprano",
        )
        session.add(track)
        session.flush()
        session.add_all(
            [
                RenditionAsset(
                    rendition_id=rendition.id,
                    asset_id=asset["id"],
                    role="original",
                ),
                ScoreRenditionSync(
                    score_revision_id=created["revision"]["id"],
                    rendition_id=rendition.id,
                    anchor_order=0,
                    score_tick=0,
                    media_ms=0,
                    confidence_milli=1000,
                    source="manual",
                ),
                ChorusMixVariant(
                    chorus_project_id=project.id,
                    chorus_timeline_id=timeline.id,
                    selection_hash="0" * 64,
                    selected_track_ids=[track.id],
                    selected_track_count=1,
                    mix_profile="default",
                    state="ready",
                    asset_id=asset["id"],
                ),
                ReleaseItem(
                    release_id=release.id,
                    rendition_id=rendition.id,
                    display_order=1,
                ),
            ]
        )
        project_id = project.id
        timeline_id = timeline.id
        track_id = track.id
        rendition_id = rendition.id
        release_id = release.id
        upload_id = upload.id

    impact = client.get(
        f"/v2/scores/{created['score']['id']}/delete-impact", headers=AUTH
    )
    assert impact.status_code == 200, impact.text
    assert {
        key: impact.json()[key]
        for key in (
            "chorus_projects",
            "chorus_timelines",
            "chorus_tracks",
            "upload_sessions",
            "chorus_renditions",
            "release_items",
            "sync_anchors",
        )
    } == {
        "chorus_projects": 1,
        "chorus_timelines": 1,
        "chorus_tracks": 1,
        "upload_sessions": 1,
        "chorus_renditions": 1,
        "release_items": 1,
        "sync_anchors": 1,
    }

    deleted = client.delete(
        f"/v2/scores/{created['score']['id']}",
        headers={**AUTH, "If-Match": '"rev-1"'},
    )
    assert deleted.status_code == 200, deleted.text
    with Session(client.app.state.v2_container.engine) as session:
        assert session.get(ChorusProject, project_id) is None
        assert session.get(ChorusTimeline, timeline_id) is None
        assert session.get(ChorusTrack, track_id) is None
        assert session.get(Rendition, rendition_id) is None
        assert session.get(UploadSession, upload_id) is None
        assert session.get(Release, release_id) is not None
        assert session.query(ReleaseItem).filter_by(release_id=release_id).count() == 0


def test_publishing_revision_opens_matching_chorus_project(client: TestClient) -> None:
    with Session(client.app.state.v2_container.engine) as session, session.begin():
        work = Work(canonical_title="Versioned chorus")
        session.add(work)
        session.flush()
        arrangement = Arrangement(work_id=work.id, name="Main")
        session.add(arrangement)
        session.flush()
        score = Score(arrangement_id=arrangement.id, label="Score", origin="manual")
        session.add(score)
        session.flush()
        first_asset = Asset(
            sha256="a" * 64,
            byte_size=100,
            detected_media_type="application/vnd.recordare.musicxml+xml",
            state="ready",
        )
        second_asset = Asset(
            sha256="b" * 64,
            byte_size=120,
            detected_media_type="application/vnd.recordare.musicxml+xml",
            state="ready",
        )
        session.add_all([first_asset, second_asset])
        session.flush()
        first_revision = ScoreRevision(score_id=score.id, revision_no=1)
        session.add(first_revision)
        session.flush()
        second_revision = ScoreRevision(
            score_id=score.id,
            revision_no=2,
            based_on_revision_id=first_revision.id,
        )
        session.add(second_revision)
        session.flush()
        session.add_all(
            [
                ScoreRevisionAsset(
                    score_revision_id=first_revision.id,
                    asset_id=first_asset.id,
                    role="primary_musicxml",
                ),
                ScoreRevisionAsset(
                    score_revision_id=second_revision.id,
                    asset_id=second_asset.id,
                    role="primary_musicxml",
                ),
            ]
        )
        project = ChorusProject(
            work_id=work.id,
            arrangement_id=arrangement.id,
            score_id=score.id,
            alignment_score_revision_id=first_revision.id,
            timeline_hash=first_asset.sha256,
            title="在线合唱",
            status="open",
            created_by_user_id="owner",
        )
        session.add(project)
        session.flush()
        session.add(
            ChorusTimeline(
                chorus_project_id=project.id,
                score_revision_id=first_revision.id,
                timeline_hash=first_asset.sha256,
            )
        )
        score.head_revision_id = second_revision.id
        score.published_revision_id = first_revision.id
        work_id = work.id
        score_id = score.id
        first_revision_id = first_revision.id
        second_revision_id = second_revision.id

    published = client.patch(
        f"/v2/scores/{score_id}",
        headers={**AUTH, "If-Match": '"rev-1"'},
        json={"published_revision_id": second_revision_id},
    )
    assert published.status_code == 200, published.text

    chorus = client.get(f"/v2/works/{work_id}/chorus", headers=AUTH)
    assert chorus.status_code == 200, chorus.text
    projects = chorus.json()["projects"]
    assert len(projects) == 1
    project = projects[0]
    assert project["score_id"] == score_id
    assert project["alignment_score_revision_id"] != second_revision_id
    assert {item["score_revision_id"] for item in project["timelines"]} == {
        first_revision_id,
        second_revision_id,
    }
    generated = next(
        item for item in project["timelines"] if item["score_revision_id"] == second_revision_id
    )
    assert generated["timeline_hash"] == "b" * 64
    assert project["title"] == "在线合唱"
    assert project["status"] == "open"


def test_asset_delivery_and_native_library_projection(client: TestClient) -> None:
    musicxml = b"<?xml version='1.0'?><score-partwise/>"
    musicxml_asset = upload_asset(
        client,
        key="delivery-musicxml",
        content=musicxml,
        media_type="application/vnd.recordare.musicxml+xml",
        filename="score.musicxml",
    )
    delivery = client.get(f"/v2/assets/{musicxml_asset['id']}/delivery", headers=AUTH)
    assert delivery.status_code == 200
    assert delivery.json()["delivery"] == "authenticated_url"
    assert delivery.json()["sha256"] == musicxml_asset["sha256"]

    container = client.app.state.v2_container
    with Session(container.engine) as session, session.begin():
        work = Work(
            canonical_title="Your Faithfulness",
            lyrics="work lyrics",
            lyrics_language="en",
            lyrics_translations=[{"language": "zh-Hans", "lyrics": "作品简体歌词"}],
        )
        session.add(work)
        session.flush()
        arrangement = Arrangement(work_id=work.id, name="Imported")
        session.add(arrangement)
        session.flush()
        score = Score(
            arrangement_id=arrangement.id,
            label="Published score",
            origin="ocr",
            lyrics="score lyrics",
            lyrics_language="en",
            lyrics_translations=[{"language": "zh-Hant", "lyrics": "樂譜繁體歌詞"}],
        )
        contributor = Contributor(display_name="Composer")
        session.add_all([score, contributor])
        session.flush()
        revision = ScoreRevision(score_id=score.id, revision_no=1)
        session.add(revision)
        session.flush()
        score.head_revision_id = revision.id
        score.published_revision_id = revision.id
        arrangement.preferred_score_id = score.id
        session.add_all(
            [
                ScoreRevisionAsset(
                    score_revision_id=revision.id,
                    asset_id=musicxml_asset["id"],
                    role="primary_musicxml",
                ),
                WorkCredit(
                    work_id=work.id,
                    contributor_id=contributor.id,
                    role="composer",
                    position=1,
                ),
            ]
        )
        rendition = Rendition(
            arrangement_id=arrangement.id,
            label="Your Faithfulness",
            kind="performance",
            duration_ms=123000,
            lyrics="rendition lyrics",
            lyrics_language="en",
            lyrics_translations=[{"language": "zh-Hans", "lyrics": "演唱简体歌词"}],
        )
        session.add(rendition)
        session.flush()
        audio = Asset(
            sha256="a" * 64,
            byte_size=321,
            detected_media_type="audio/mpeg",
            state="ready",
        )
        session.add(audio)
        session.flush()
        session.add_all(
            [
                AssetLocation(
                    asset_id=audio.id,
                    provider="local",
                    storage_key="sha256/aa/example.mp3",
                ),
                RenditionAsset(rendition_id=rendition.id, asset_id=audio.id, role="stream"),
            ]
        )
        release = Release(key="ihope", title="ihope")
        session.add(release)
        session.flush()
        session.add(
            ReleaseItem(
                release_id=release.id,
                rendition_id=rendition.id,
                track_no=109,
                display_order=1,
            )
        )
        second_audio = Asset(
            sha256="b" * 64,
            byte_size=654,
            detected_media_type="audio/mpeg",
            state="ready",
        )
        second_rendition = Rendition(
            arrangement_id=arrangement.id,
            label="Second Song",
            kind="performance",
        )
        session.add_all([second_audio, second_rendition])
        session.flush()
        session.add_all(
            [
                AssetLocation(
                    asset_id=second_audio.id,
                    provider="local",
                    storage_key="sha256/bb/second.mp3",
                ),
                RenditionAsset(
                    rendition_id=second_rendition.id,
                    asset_id=second_audio.id,
                    role="stream",
                ),
                ReleaseItem(
                    release_id=release.id,
                    rendition_id=second_rendition.id,
                    track_no=110,
                    display_order=2,
                ),
            ]
        )
        midi_asset = Asset(
            sha256="c" * 64,
            byte_size=42,
            detected_media_type="audio/midi",
            state="ready",
        )
        midi_rendition = Rendition(
            arrangement_id=arrangement.id,
            label="Not a Song",
            kind="reference_midi",
        )
        session.add_all([midi_asset, midi_rendition])
        session.flush()
        session.add_all(
            [
                RenditionAsset(
                    rendition_id=midi_rendition.id,
                    asset_id=midi_asset.id,
                    role="midi",
                ),
                ReleaseItem(
                    release_id=release.id,
                    rendition_id=midi_rendition.id,
                    display_order=3,
                ),
            ]
        )
        no_location_audio = Asset(
            sha256="f" * 64,
            byte_size=100,
            detected_media_type="audio/mpeg",
            state="ready",
        )
        no_location_rendition = Rendition(
            arrangement_id=arrangement.id,
            label="No Location Song",
            kind="performance",
        )
        session.add_all([no_location_audio, no_location_rendition])
        session.flush()
        session.add_all(
            [
                RenditionAsset(
                    rendition_id=no_location_rendition.id,
                    asset_id=no_location_audio.id,
                    role="stream",
                ),
                ReleaseItem(
                    release_id=release.id,
                    rendition_id=no_location_rendition.id,
                    display_order=5,
                ),
            ]
        )
        deleted_audio = Asset(
            sha256="e" * 64,
            byte_size=99,
            detected_media_type="audio/mpeg",
            state="ready",
        )
        deleted_rendition = Rendition(
            arrangement_id=arrangement.id,
            label="Deleted Song",
            kind="performance",
            deleted_at=utc_now(),
        )
        session.add_all([deleted_audio, deleted_rendition])
        session.flush()
        session.add_all(
            [
                RenditionAsset(
                    rendition_id=deleted_rendition.id,
                    asset_id=deleted_audio.id,
                    role="stream",
                ),
                ReleaseItem(
                    release_id=release.id,
                    rendition_id=deleted_rendition.id,
                    display_order=4,
                ),
            ]
        )
        work_id = work.id
        arrangement_id = arrangement.id
        rendition_id = rendition.id
        release_id = release.id

    first_page = client.get("/v2/library/songs?limit=1", headers=AUTH)
    assert first_page.status_code == 200
    assert first_page.json()["next_cursor"] is not None
    assert first_page.json()["items"] == [
        {
            "work_id": work_id,
                "arrangement_id": arrangement_id,
                "rendition_id": rendition_id,
                "rendition_revision": 1,
            "album_id": release_id,
            "title": "Your Faithfulness",
            "artist": "Composer",
            "album_title": "ihope",
            "duration_ms": 123000,
            "track_no": 109,
            "cover_asset_id": None,
            "cover_url": None,
            "lyrics": "rendition lyrics",
            "lyrics_language": "en",
            "lyrics_translations": [
                {"language": "zh-Hans", "lyrics": "演唱简体歌词"},
                {"language": "zh-Hant", "lyrics": "樂譜繁體歌詞"},
            ],
            "lyrics_source_images": [],
                "lyric_source_count": 0,
                "lyrics_formats": [
                    {"language": "en", "format": "plain"},
                    {"language": "zh-Hans", "format": "plain"},
                    {"language": "zh-Hant", "format": "plain"},
                ],
            }
    ]
    second_page = client.get(
        "/v2/library/songs",
        headers=AUTH,
        params={"limit": 1, "cursor": first_page.json()["next_cursor"]},
    )
    assert second_page.status_code == 200
    assert second_page.json()["next_cursor"] is None
    assert second_page.json()["items"][0]["title"] == "Second Song"
    assert second_page.json()["items"][0]["lyrics"] == "score lyrics"
    assert second_page.json()["items"][0]["lyrics_translations"] == [
        {"language": "zh-Hans", "lyrics": "作品简体歌词"},
        {"language": "zh-Hant", "lyrics": "樂譜繁體歌詞"},
    ]
    assert client.get("/v2/library/songs?cursor=invalid!", headers=AUTH).status_code == 422
    albums = client.get("/v2/library/albums", headers=AUTH)
    assert albums.status_code == 200
    assert albums.json()["items"][0]["key"] == "ihope"
    assert albums.json()["items"][0]["song_count"] == 2
    detail = client.get(f"/v2/library/albums/{release_id}", headers=AUTH)
    assert detail.status_code == 200
    assert detail.json()["album"]["id"] == release_id
    assert [item["title"] for item in detail.json()["songs"]] == [
        "Your Faithfulness",
        "Second Song",
    ]
    score_works = client.get("/v2/library/score-works", headers=AUTH)
    assert score_works.status_code == 200, score_works.text
    assert score_works.json()["next_cursor"] is None
    score_work = score_works.json()["items"][0]
    assert score_work["work_id"] == work_id
    assert score_work["artist"] == "Composer"
    assert score_work["default_score_id"] == score_work["score_options"][0]["score_id"]
    assert score_work["score_count"] == 1
    assert score_work["origins"] == ["ocr"]
    assert score_work["score_options"][0]["preferred"] is True

    archived = client.patch(
        f"/v2/works/{work_id}",
        headers={**AUTH, "If-Match": '"rev-1"'},
        json={"status": "archived"},
    )
    assert archived.status_code == 200, archived.text
    assert client.get("/v2/library/songs", headers=AUTH).json()["items"] == []
    archived_albums = client.get("/v2/library/albums", headers=AUTH).json()["items"]
    assert archived_albums[0]["song_count"] == 0
    assert client.get("/v2/library/score-works", headers=AUTH).json()["items"] == []
    archived_album = client.get(f"/v2/library/albums/{release_id}", headers=AUTH).json()
    assert archived_album["album"]["song_count"] == 0
    assert archived_album["songs"] == []


def test_asset_delivery_returns_signed_cos_url_without_auth_in_url(tmp_path: Path) -> None:
    settings = Settings(
        bootstrap_token=TOKEN,
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
        cos_secret_id="AKID-test",
        cos_secret_key="secret-test",
    )
    with TestClient(create_app(settings)) as cos_client:
        with Session(cos_client.app.state.v2_container.engine) as session, session.begin():
            asset = Asset(
                sha256="d" * 64,
                byte_size=456,
                detected_media_type="application/vnd.recordare.musicxml+xml",
                state="ready",
            )
            session.add(asset)
            session.flush()
            session.add(
                AssetLocation(
                    asset_id=asset.id,
                    provider="cos",
                    storage_key="musicxml-1328751369/gmusic/343/rev1.musicxml",
                )
            )
            asset_id = asset.id

        response = cos_client.get(f"/v2/assets/{asset_id}/delivery", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["delivery"] == "signed_url"
        assert body["url"].startswith("https://musicxml-1328751369.cos.")
        assert "Authorization" not in body["url"]
        assert body["expires_at"] is not None
        assert body["sha256"] == "d" * 64


def test_library_album_returns_signed_cos_cover_url(tmp_path: Path) -> None:
    settings = Settings(
        bootstrap_token=TOKEN,
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
        cos_secret_id="AKID-test",
        cos_secret_key="secret-test",
    )
    with TestClient(create_app(settings)) as cos_client:
        with Session(cos_client.app.state.v2_container.engine) as session, session.begin():
            cover = Asset(
                sha256="e" * 64,
                byte_size=1234,
                detected_media_type="image/png",
                state="ready",
            )
            session.add(cover)
            session.flush()
            cover_id = cover.id
            session.add_all(
                [
                    AssetLocation(
                        asset_id=cover.id,
                        provider="cos",
                        storage_key="bible-1328751369/artwork/albums/ihope/cover.png",
                    ),
                    Release(key="ihope", title="ihope", cover_asset_id=cover.id),
                ]
            )

        response = cos_client.get("/v2/library/albums", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["items"][0]["cover_asset_id"] == cover_id
        cover_url = response.json()["items"][0]["cover_url"]
        assert cover_url.startswith("https://bible-1328751369.cos.")
        assert "/artwork/albums/ihope/cover.png?" in cover_url
        assert "q-signature=" in cover_url


def test_v2_requires_auth_and_problem_details(client: TestClient) -> None:
    unauthorized = client.get("/v2/works")
    assert unauthorized.status_code == 401

    missing_key = client.post(
        "/v2/works",
        headers=AUTH,
        json={"canonical_title": "No key"},
    )
    assert missing_key.status_code == 422
    assert missing_key.headers["content-type"].startswith("application/problem+json")
    assert missing_key.json()["type"].endswith("/domain-validation")


def test_work_edit_replaces_people_languages_and_external_ids(client: TestClient) -> None:
    composer = post(
        client,
        "/v2/contributors",
        "editable-composer",
        {"display_name": "Original Composer"},
    )
    lyricist = post(
        client,
        "/v2/contributors",
        "editable-lyricist",
        {"display_name": "New Lyricist", "sort_name": "Lyricist, New"},
    )
    work = post(
        client,
        "/v2/works",
        "editable-work",
        {
            "canonical_title": "Editable Work",
            "credits": [
                {
                    "contributor_id": composer.json()["id"],
                    "role": "composer",
                    "position": 1,
                }
            ],
        },
    )

    contributors = client.get("/v2/contributors?q=Lyricist", headers=AUTH)
    assert contributors.status_code == 200
    assert [item["display_name"] for item in contributors.json()["items"]] == [
        "New Lyricist"
    ]

    patched = client.patch(
        f"/v2/works/{work.json()['id']}",
        headers={**AUTH, "If-Match": '"rev-1"'},
        json={
            "language": "zh_hant",
            "lyrics": "主要歌词",
            "lyrics_language": "zh-hant",
            "lyrics_translations": [{"language": "en_us", "lyrics": "English lyrics"}],
            "aliases": [{"namespace": "legacy", "external_id": "work-99"}],
            "credits": [
                {
                    "contributor_id": lyricist.json()["id"],
                    "role": "lyricist",
                    "position": 1,
                },
                {
                    "contributor_id": composer.json()["id"],
                    "role": "composer",
                    "position": 2,
                },
            ],
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.headers["etag"] == '"rev-2"'
    assert patched.json()["language"] == "zh-Hant"
    assert patched.json()["lyrics_language"] == "zh-Hant"
    assert patched.json()["lyrics_translations"] == [
        {"language": "en-US", "lyrics": "English lyrics"}
    ]
    assert patched.json()["aliases"] == [{"namespace": "legacy", "external_id": "work-99"}]
    assert [item["display_name"] for item in patched.json()["credits"]] == [
        "New Lyricist",
        "Original Composer",
    ]


def test_private_catalog_end_to_end(client: TestClient) -> None:
    contributor = post(
        client,
        "/v2/contributors",
        "contributor-1",
        {"display_name": "Composer"},
    )
    assert contributor.status_code == 201

    work_payload = {
        "canonical_title": "Example Work",
        "aliases": [{"namespace": "personal", "external_id": "work-001"}],
        "credits": [
            {
                "contributor_id": contributor.json()["id"],
                "role": "composer",
                "position": 1,
            }
        ],
    }
    work = post(client, "/v2/works", "work-1", work_payload)
    assert work.status_code == 201
    work_id = work.json()["id"]

    replay = post(client, "/v2/works", "work-1", work_payload)
    assert replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["id"] == work_id

    conflict = post(
        client,
        "/v2/works",
        "work-1",
        {"canonical_title": "Different payload"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["type"].endswith("/idempotency-key-reused")

    resolved = client.post(
        "/v2/works/resolve",
        headers=AUTH,
        json={"aliases": [{"namespace": "personal", "external_id": "work-001"}]},
    )
    assert resolved.json()["result"] == "exact"
    assert resolved.json()["work"]["id"] == work_id

    arrangement = post(
        client,
        f"/v2/works/{work_id}/arrangements",
        "arrangement-1",
        {
            "name": "SATB",
            "voicing": "SATB",
            "parts": [
                {"code": "S", "name": "Soprano", "display_order": 1},
                {"code": "A", "name": "Alto", "display_order": 2},
            ],
        },
    )
    assert arrangement.status_code == 201
    arrangement_id = arrangement.json()["id"]

    musicxml = b"<?xml version='1.0'?><score-partwise version='4.0'><part-list/></score-partwise>"
    score_asset = upload_asset(
        client,
        key="score-file",
        content=musicxml,
        media_type="application/vnd.recordare.musicxml+xml",
        filename="score.musicxml",
    )

    reused = post(
        client,
        "/v2/uploads",
        "score-file-reuse",
        {
            "sha256": score_asset["sha256"],
            "byte_size": len(musicxml),
            "media_type": "application/vnd.recordare.musicxml+xml",
            "original_filename": "same-score.musicxml",
        },
    )
    assert reused.status_code == 200
    assert reused.json()["status"] == "reused"
    assert reused.json()["asset"]["id"] == score_asset["id"]

    score = post(
        client,
        f"/v2/arrangements/{arrangement_id}/scores",
        "score-1",
        {"label": "Imported score", "origin": "external_import"},
    )
    assert score.headers["etag"] == '"rev-1"'
    score_id = score.json()["id"]

    revision = client.post(
        f"/v2/scores/{score_id}/revisions",
        headers={**AUTH, "Idempotency-Key": "revision-1", "If-Match": '"rev-1"'},
        json={
            "edit_message": "initial import",
            "assets": [{"asset_id": score_asset["id"], "role": "primary_musicxml"}],
        },
    )
    assert revision.status_code == 201, revision.text
    assert revision.headers["etag"] == '"rev-2"'
    revision_id = revision.json()["id"]

    stale = client.post(
        f"/v2/scores/{score_id}/revisions",
        headers={**AUTH, "Idempotency-Key": "revision-stale", "If-Match": '"rev-1"'},
        json={
            "based_on_revision_id": revision_id,
            "assets": [{"asset_id": score_asset["id"], "role": "primary_musicxml"}],
        },
    )
    assert stale.status_code == 412
    assert stale.json()["current_etag"] == '"rev-2"'

    preferred = client.patch(
        f"/v2/arrangements/{arrangement_id}",
        headers={**AUTH, "If-Match": '"rev-1"'},
        json={"preferred_score_id": score_id},
    )
    assert preferred.status_code == 200
    assert preferred.json()["preferred_score_id"] == score_id

    audio = b"ID3" + bytes(range(64))
    audio_asset = upload_asset(
        client,
        key="audio-file",
        content=audio,
        media_type="audio/mpeg",
        filename="performance.mp3",
    )
    rendition = post(
        client,
        f"/v2/arrangements/{arrangement_id}/renditions",
        "rendition-1",
        {
            "label": "Choir performance",
            "kind": "performance",
            "duration_ms": 120000,
            "assets": [{"asset_id": audio_asset["id"], "role": "master"}],
        },
    )
    assert rendition.status_code == 201, rendition.text
    rendition_id = rendition.json()["id"]

    playback = client.get(f"/v2/renditions/{rendition_id}/playback", headers=AUTH)
    assert playback.status_code == 200
    assert playback.json()["asset_id"] == audio_asset["id"]
    assert playback.json()["supports_range"] is True
    assert playback.json()["cache_key"].startswith("rhythm:asset:")

    ranged = client.get(
        playback.json()["url"],
        headers={**AUTH, "Range": "bytes=3-8"},
    )
    assert ranged.status_code == 206
    assert ranged.content == audio[3:9]
    assert ranged.headers["content-range"] == f"bytes 3-8/{len(audio)}"

    midi_asset = upload_asset(
        client,
        key="midi-source-file",
        content=b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0",
        media_type="audio/midi",
        filename="source.mid",
    )
    midi_rendition = post(
        client,
        f"/v2/arrangements/{arrangement_id}/renditions",
        "midi-rendition",
        {
            "label": "Source MIDI",
            "kind": "reference_midi",
            "assets": [{"asset_id": midi_asset["id"], "role": "midi"}],
        },
    )
    assert midi_rendition.status_code == 201, midi_rendition.text
    midi_playback = client.get(
        f"/v2/renditions/{midi_rendition.json()['id']}/playback?prefer=midi",
        headers=AUTH,
    )
    assert midi_playback.status_code == 404
    assert midi_playback.json()["detail"] == "rendition has no playable real-audio assets"

    bundle = client.get(f"/v2/works/{work_id}/bundle", headers=AUTH)
    assert bundle.status_code == 200
    assert bundle.json()["arrangements"][0]["scores"][0]["head_revision_id"] == revision_id
    assert bundle.json()["arrangements"][0]["renditions"][0]["id"] == rendition_id

    unchanged = client.get(
        f"/v2/works/{work_id}/bundle",
        headers={**AUTH, "If-None-Match": bundle.headers["etag"]},
    )
    assert unchanged.status_code == 304

    changes = client.get("/v2/sync/changes?after=0", headers=AUTH)
    operations = [item["operation"] for item in changes.json()["changes"]]
    assert operations == [
        "work.created",
        "arrangement.created",
        "score.created",
        "score.revision_created",
        "arrangement.updated",
        "rendition.created",
        "rendition.created",
    ]


def test_rendition_management_projection_and_relationship_updates(
    client: TestClient,
) -> None:
    work = post(client, "/v2/works", "rendition-management-work", {"canonical_title": "Gloria"})
    arrangement = post(
        client,
        f"/v2/works/{work.json()['id']}/arrangements",
        "rendition-management-arrangement",
        {"name": "SATB", "voicing": "SATB"},
    )
    contributor = post(
        client,
        "/v2/contributors",
        "rendition-management-contributor",
        {"display_name": "Festival Choir"},
    ).json()
    png_header = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00" * 8
        + (800).to_bytes(4, "big")
        + (800).to_bytes(4, "big")
        + b"\x00" * 8
    )
    cover = upload_asset(
        client,
        key="rendition-management-cover",
        content=png_header + b"cover",
        media_type="image/png",
        filename="cover.png",
    )
    audio = upload_asset(
        client,
        key="rendition-management-audio",
        content=b"ID3" + bytes(range(64)),
        media_type="audio/mpeg",
        filename="gloria.mp3",
    )
    with Session(client.app.state.v2_container.engine) as session, session.begin():
        release = Release(key="festival-album", title="Festival Album")
        session.add(release)
        session.flush()
        release_id = release.id

    releases = client.get("/v2/releases", headers=AUTH)
    assert releases.status_code == 200
    assert releases.json()["items"][0]["id"] == release_id

    created = post(
        client,
        f"/v2/arrangements/{arrangement.json()['id']}/renditions",
        "rendition-management-create",
        {
            "label": "Festival performance",
            "kind": "performance",
            "ensemble": "Festival Choir",
            "cover_asset_id": cover["id"],
            "lyrics": "[00:01.00]Gloria",
            "lyrics_language": "la",
            "lyrics_formats": [{"language": "la", "format": "lrc"}],
            "credits": [
                {
                    "contributor_id": contributor["id"],
                    "role": "performer",
                    "position": 1,
                }
            ],
            "release_placements": [
                {
                    "release_id": release_id,
                    "disc_no": 1,
                    "track_no": 2,
                    "display_order": 2,
                }
            ],
            "assets": [{"asset_id": audio["id"], "role": "master"}],
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["management_category"] == "finished_audio"
    assert body["cover_asset_id"] == cover["id"]
    assert body["lyrics_formats"] == [{"language": "la", "format": "lrc"}]
    assert body["credits"][0]["display_name"] == "Festival Choir"
    assert body["release_placements"][0] == {
        "release_id": release_id,
        "disc_no": 1,
        "track_no": 2,
        "display_order": 2,
        "id": body["release_placements"][0]["id"],
        "release_title": "Festival Album",
    }
    with Session(client.app.state.v2_container.engine) as session:
        assert session.query(RenditionCredit).filter_by(rendition_id=body["id"]).count() == 1

    patched = client.patch(
        f"/v2/renditions/{body['id']}",
        headers={**AUTH, "If-Match": '"rev-1"'},
        json={
            "kind": "reference_audio",
            "cover_asset_id": None,
            "credits": [],
            "release_placements": [],
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["management_category"] == "reference_resource"
    assert patched.json()["cover_asset_id"] is None
    assert patched.json()["credits"] == []
    assert patched.json()["release_placements"] == []

    link_id = patched.json()["assets"][0]["id"]
    removed = client.delete(
        f"/v2/renditions/{body['id']}/assets/{link_id}",
        headers={**AUTH, "If-Match": '"rev-2"'},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["assets"] == []


def test_score_admin_list_and_revision_history(client: TestClient) -> None:
    work = post(
        client,
        "/v2/works",
        "score-list-work",
        {"canonical_title": "夜空中最亮的星", "language": "zh-Hans"},
    ).json()
    arrangement = post(
        client,
        f"/v2/works/{work['id']}/arrangements",
        "score-list-arrangement",
        {
            "name": "正式编配",
            "voicing": "SATB",
            "key_signature": "C",
            "parts": [
                {"code": "S", "name": "女高音", "display_order": 1},
                {"code": "A", "name": "女低音", "display_order": 2},
            ],
        },
    ).json()
    score_response = post(
        client,
        f"/v2/arrangements/{arrangement['id']}/scores",
        "score-list-score",
        {"label": "精校谱", "origin": "midi_transcription"},
    )
    score = score_response.json()

    musicxml = b"<?xml version='1.0'?><score-partwise version='4.0'><part-list/></score-partwise>"
    asset = upload_asset(
        client,
        key="score-list-musicxml",
        content=musicxml,
        media_type="application/vnd.recordare.musicxml+xml",
        filename="score.musicxml",
    )
    revision_response = client.post(
        f"/v2/scores/{score['id']}/revisions",
        headers={**AUTH, "Idempotency-Key": "score-list-revision", "If-Match": '"rev-1"'},
        json={
            "edit_message": "修正节拍与歌词",
            "assets": [{"asset_id": asset["id"], "role": "primary_musicxml"}],
        },
    )
    assert revision_response.status_code == 201, revision_response.text
    revision = revision_response.json()
    published = client.patch(
        f"/v2/scores/{score['id']}",
        headers={**AUTH, "If-Match": '"rev-2"'},
        json={"published_revision_id": revision["id"]},
    )
    assert published.status_code == 200, published.text
    preferred = client.patch(
        f"/v2/arrangements/{arrangement['id']}",
        headers={**AUTH, "If-Match": '"rev-1"'},
        json={"preferred_score_id": score["id"]},
    )
    assert preferred.status_code == 200, preferred.text

    second_work = post(
        client,
        "/v2/works",
        "score-list-other-work",
        {"canonical_title": "另一首歌"},
    ).json()
    second_arrangement = post(
        client,
        f"/v2/works/{second_work['id']}/arrangements",
        "score-list-other-arrangement",
        {"name": "默认编配"},
    ).json()
    second_score = post(
        client,
        f"/v2/arrangements/{second_arrangement['id']}/scores",
        "score-list-other-score",
        {"label": "粗谱", "origin": "ocr"},
    ).json()

    filtered = client.get("/v2/scores?q=夜空", headers=AUTH)
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["next_cursor"] is None
    assert filtered.json()["items"] == [
        {
            "id": score["id"],
            "arrangement_id": arrangement["id"],
            "work_id": work["id"],
            "work_title": "夜空中最亮的星",
            "arrangement_name": "正式编配",
            "arrangement_voicing": "SATB",
            "arrangement_key_signature": "C",
            "part_count": 2,
            "label": "精校谱",
            "origin": "midi_transcription",
            "head_revision_id": revision["id"],
            "head_revision_no": 1,
            "published_revision_id": revision["id"],
            "published_revision_no": 1,
            "preferred": True,
            "revision": 3,
            "created_at": filtered.json()["items"][0]["created_at"],
            "updated_at": filtered.json()["items"][0]["updated_at"],
        }
    ]

    first_page = client.get("/v2/scores?limit=1", headers=AUTH).json()
    assert len(first_page["items"]) == 1
    assert first_page["next_cursor"] is not None
    second_page = client.get(
        f"/v2/scores?limit=1&cursor={first_page['next_cursor']}", headers=AUTH
    ).json()
    assert len(second_page["items"]) == 1
    assert {first_page["items"][0]["id"], second_page["items"][0]["id"]} == {
        score["id"],
        second_score["id"],
    }

    revisions = client.get(f"/v2/scores/{score['id']}/revisions", headers=AUTH)
    assert revisions.status_code == 200, revisions.text
    assert len(revisions.json()["items"]) == 1
    listed_revision = revisions.json()["items"][0]
    assert listed_revision["id"] == revision["id"]
    assert listed_revision["revision_no"] == 1
    assert listed_revision["edit_message"] == "修正节拍与歌词"
    assert listed_revision["assets"] == revision["assets"]
    missing = client.get("/v2/scores/missing/revisions", headers=AUTH)
    assert missing.status_code == 404


def test_multilingual_lyrics_crud_and_validation(client: TestClient) -> None:
    work = post(
        client,
        "/v2/works",
        "multilingual-work",
        {
            "canonical_title": "Multilingual Work",
            "language": "zh-hans",
            "lyrics": "默认歌词",
            "lyrics_translations": [
                {"language": "en_us", "lyrics": "English lyrics"},
            ],
        },
    )
    assert work.status_code == 201, work.text
    assert work.json()["lyrics"] == "默认歌词"
    assert work.json()["lyrics_language"] == "zh-Hans"
    assert work.json()["lyrics_translations"] == [{"language": "en-US", "lyrics": "English lyrics"}]
    work_id = work.json()["id"]

    arrangement = post(
        client,
        f"/v2/works/{work_id}/arrangements",
        "multilingual-arrangement",
        {"name": "Default"},
    )
    assert arrangement.status_code == 201, arrangement.text
    arrangement_id = arrangement.json()["id"]

    score = post(
        client,
        f"/v2/arrangements/{arrangement_id}/scores",
        "multilingual-score",
        {
            "label": "Localized score",
            "origin": "manual",
            "lyrics": "乐谱歌词",
            "lyrics_translations": [
                {"language": "en", "lyrics": "Score lyrics"},
            ],
        },
    )
    assert score.status_code == 201, score.text
    assert score.json()["lyrics_language"] == "zh-Hans"
    score_id = score.json()["id"]

    patched_score = client.patch(
        f"/v2/scores/{score_id}",
        headers={**AUTH, "If-Match": '"rev-1"'},
        json={
            "lyrics_translations": [
                {"language": "en", "lyrics": "Updated score lyrics"},
                {"language": "zh-Hant", "lyrics": "樂譜歌詞"},
            ]
        },
    )
    assert patched_score.status_code == 200, patched_score.text
    assert patched_score.json()["lyrics_translations"] == [
        {"language": "en", "lyrics": "Updated score lyrics"},
        {"language": "zh-Hant", "lyrics": "樂譜歌詞"},
    ]

    rendition = post(
        client,
        f"/v2/arrangements/{arrangement_id}/renditions",
        "multilingual-rendition",
        {
            "label": "English performance",
            "kind": "performance",
            "lyrics": "Performance lyrics",
            "lyrics_language": "en",
            "lyrics_translations": [
                {"language": "zh-Hans", "lyrics": "演唱歌词"},
            ],
        },
    )
    assert rendition.status_code == 201, rendition.text
    assert rendition.json()["lyrics_language"] == "en"
    assert rendition.json()["lyrics_translations"] == [
        {"language": "zh-Hans", "lyrics": "演唱歌词"}
    ]
    rendition_id = rendition.json()["id"]

    replaced = client.put(
        f"/v2/renditions/{rendition_id}/lyrics/zh-Hans",
        headers={
            **AUTH,
            "If-Match": '"rev-1"',
            "Idempotency-Key": "replace-zh-hans-lyrics",
        },
        json={"lyrics": "[00:01.000]更新演唱歌词", "format": "lrc"},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.headers["etag"] == '"rev-2"'
    assert replaced.json() == {
        "rendition_id": rendition_id,
        "revision": 2,
        "language": "zh-Hans",
        "lyrics": "[00:01.000]更新演唱歌词",
        "format": "lrc",
        "lyrics_language": "en",
        "lyrics_translations": [
            {"language": "zh-Hans", "lyrics": "[00:01.000]更新演唱歌词"}
        ],
        "lyrics_formats": [
            {"language": "en", "format": "plain"},
            {"language": "zh-Hans", "format": "lrc"},
        ],
    }

    replayed = client.put(
        f"/v2/renditions/{rendition_id}/lyrics/zh-Hans",
        headers={
            **AUTH,
            "If-Match": '"rev-1"',
            "Idempotency-Key": "replace-zh-hans-lyrics",
        },
        json={"lyrics": "[00:01.000]更新演唱歌词", "format": "lrc"},
    )
    assert replayed.status_code == 200
    assert replayed.headers["idempotency-replayed"] == "true"

    stale = client.put(
        f"/v2/renditions/{rendition_id}/lyrics/en",
        headers={
            **AUTH,
            "If-Match": '"rev-1"',
            "Idempotency-Key": "stale-english-lyrics",
        },
        json={"lyrics": "Stale edit", "format": "plain"},
    )
    assert stale.status_code == 412
    assert stale.json()["current_etag"] == '"rev-2"'

    stored = client.get(f"/v2/renditions/{rendition_id}", headers=AUTH)
    assert stored.json()["lyrics"] == "Performance lyrics"
    assert stored.json()["lyrics_translations"] == [
        {"language": "zh-Hans", "lyrics": "[00:01.000]更新演唱歌词"}
    ]

    invalid_default_duplicate = post(
        client,
        "/v2/works",
        "invalid-default-duplicate",
        {
            "canonical_title": "Invalid duplicate",
            "lyrics": "Primary",
            "lyrics_language": "en",
            "lyrics_translations": [{"language": "EN", "lyrics": "Duplicate"}],
        },
    )
    assert invalid_default_duplicate.status_code == 422

    invalid_translation_without_primary = post(
        client,
        "/v2/works",
        "invalid-without-primary",
        {
            "canonical_title": "Missing primary",
            "lyrics_language": "en",
            "lyrics_translations": [{"language": "zh", "lyrics": "翻译"}],
        },
    )
    assert invalid_translation_without_primary.status_code == 422


def test_rejects_invalid_musicxml_at_completion(client: TestClient) -> None:
    invalid = b"<not-a-score/>"
    digest = hashlib.sha256(invalid).hexdigest()
    created = post(
        client,
        "/v2/uploads",
        "bad-xml-create",
        {
            "sha256": digest,
            "byte_size": len(invalid),
            "media_type": "application/vnd.recordare.musicxml+xml",
            "original_filename": "bad.musicxml",
        },
    ).json()
    upload_id = created["upload"]["id"]
    assert (
        client.put(
            f"/v2/uploads/{upload_id}/content",
            headers=AUTH,
            content=invalid,
        ).status_code
        == 200
    )
    completed = post(client, f"/v2/uploads/{upload_id}/complete", "bad-xml-complete", {})
    assert completed.status_code == 422
    assert completed.json()["type"].endswith("/invalid-upload")


def test_shared_lyric_source_pages_and_effective_precedence(client: TestClient) -> None:
    pdf_asset = upload_asset(
        client,
        key="lyric-source-pdf",
        content=b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n",
        media_type="application/pdf",
        filename="songbook.pdf",
    )
    png_header = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00" * 8
        + (1200).to_bytes(4, "big")
        + (1800).to_bytes(4, "big")
        + b"\x00" * 8
    )
    first_image = upload_asset(
        client,
        key="lyric-source-page-1",
        content=png_header + b"page-one",
        media_type="image/png",
        filename="page-001.png",
    )
    second_image = upload_asset(
        client,
        key="lyric-source-page-2",
        content=png_header + b"page-two",
        media_type="image/png",
        filename="page-002.png",
    )
    document = post(
        client,
        "/v2/lyric-source-documents",
        "create-lyric-source-document",
        {
            "title": "IHOP Songbook 2024",
            "source_kind": "pdf",
            "document_asset_id": pdf_asset["id"],
            "source_ref": "2024-IHOP-Songbook.pdf",
        },
    )
    assert document.status_code == 201, document.text
    document_id = document.json()["id"]

    pages = []
    for number, asset in ((1, first_image), (2, second_image)):
        page = post(
            client,
            f"/v2/lyric-source-documents/{document_id}/pages",
            f"create-lyric-source-page-{number}",
            {
                "physical_page_number": number,
                "image_asset_id": asset["id"],
                "width_px": 1200,
                "height_px": 1800,
                "render_dpi": 144,
                "display_label": f"PDF page {number}",
            },
        )
        assert page.status_code == 201, page.text
        pages.append(page.json())

    work_ids = []
    for index in (1, 2):
        work = post(
            client,
            "/v2/works",
            f"create-source-work-{index}",
            {"canonical_title": f"Shared page song {index}"},
        )
        assert work.status_code == 201, work.text
        work_ids.append(work.json()["id"])

    shared_link_body = {
        "source_page_id": pages[0]["id"],
        "display_order": 2,
        "language_relations": [
            {"language": "zh-Hans", "relation": "printed"},
            {
                "language": "zh-Hant",
                "relation": "converted",
                "derived_from_language": "zh-Hans",
            },
        ],
    }
    for index, work_id in enumerate(work_ids, 1):
        headers = {
            **AUTH,
            "If-Match": '"rev-1"',
            "Idempotency-Key": f"link-shared-{index}",
        }
        linked = client.post(
            f"/v2/works/{work_id}/lyric-source-pages",
            headers=headers,
            json=shared_link_body,
        )
        assert linked.status_code == 201, linked.text
        assert linked.json()["image_asset_id"] == first_image["id"]
        replay = client.post(
            f"/v2/works/{work_id}/lyric-source-pages",
            headers=headers,
            json=shared_link_body,
        )
        assert replay.status_code == 201, replay.text
        assert replay.headers["Idempotency-Replayed"] == "true"

    second_work_page = client.post(
        f"/v2/works/{work_ids[0]}/lyric-source-pages",
        headers={**AUTH, "If-Match": '"rev-2"', "Idempotency-Key": "link-work-page-2"},
        json={"source_page_id": pages[1]["id"], "display_order": 1},
    )
    assert second_work_page.status_code == 201, second_work_page.text
    work_response = client.get(f"/v2/works/{work_ids[0]}", headers=AUTH).json()
    assert [item["physical_page_number"] for item in work_response["lyrics_source_images"]] == [
        2,
        1,
    ]

    arrangement = post(
        client,
        f"/v2/works/{work_ids[0]}/arrangements",
        "create-source-arrangement",
        {"name": "Default"},
    ).json()
    score = post(
        client,
        f"/v2/arrangements/{arrangement['id']}/scores",
        "create-source-score",
        {"label": "Source score", "origin": "external_import"},
    ).json()
    rendition = post(
        client,
        f"/v2/arrangements/{arrangement['id']}/renditions",
        "create-source-rendition",
        {"label": "Source rendition", "kind": "performance"},
    ).json()
    patched_arrangement = client.patch(
        f"/v2/arrangements/{arrangement['id']}",
        headers={**AUTH, "If-Match": '"rev-1"'},
        json={"preferred_score_id": score["id"]},
    )
    assert patched_arrangement.status_code == 200, patched_arrangement.text
    score_link = client.post(
        f"/v2/scores/{score['id']}/lyric-source-pages",
        headers={**AUTH, "If-Match": '"rev-1"', "Idempotency-Key": "link-score-page-2"},
        json={"source_page_id": pages[1]["id"], "display_order": 1},
    )
    assert score_link.status_code == 201, score_link.text
    rendition_link = client.post(
        f"/v2/renditions/{rendition['id']}/lyric-source-pages",
        headers={
            **AUTH,
            "If-Match": '"rev-1"',
            "Idempotency-Key": "link-rendition-page-1",
        },
        json={"source_page_id": pages[0]["id"], "display_order": 1},
    )
    assert rendition_link.status_code == 201, rendition_link.text

    effective = client.get(
        f"/v2/renditions/{rendition['id']}/effective-lyric-sources", headers=AUTH
    )
    assert effective.status_code == 200, effective.text
    assert [item["owner_type"] for item in effective.json()["items"]] == [
        "rendition",
        "score",
    ]
    assert [item["source_page_id"] for item in effective.json()["items"]] == [
        pages[0]["id"],
        pages[1]["id"],
    ]
    assert (
        client.get(f"/v2/works/{work_ids[1]}", headers=AUTH).json()["lyrics_source_images"][0][
            "source_page_id"
        ]
        == pages[0]["id"]
    )
