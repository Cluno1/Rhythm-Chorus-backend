from __future__ import annotations

import hashlib
import io
import shutil
import struct
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.catalog_service import ActorContext
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.domain.v2.errors import V2NotFound
from rhythm_metadata_api.infrastructure.db.models import Arrangement, Score, ScoreRevision, Work
from rhythm_metadata_api.main import create_app

TOKEN = "test-private-catalog-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Device-ID": "chorus-test-device"}


def _post(client: TestClient, path: str, key: str, body: dict[str, object]):
    return client.post(path, headers={**AUTH, "Idempotency-Key": key}, json=body)


def _wav_bytes(duration_ms: int = 300) -> bytes:
    sample_rate = 48_000
    frame_count = sample_rate * duration_ms // 1000
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            value = 8_000 if (index // 240) % 2 == 0 else -8_000
            frames.extend(struct.pack("<h", value))
        wav.writeframes(frames)
    return output.getvalue()


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required for the chorus worker integration test")
    settings = Settings(
        bootstrap_token=TOKEN,
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _catalog_timeline(client: TestClient) -> tuple[str, str, str, str]:
    with Session(client.app.state.v2_container.engine) as session, session.begin():
        work = Work(canonical_title="Chorus Test")
        session.add(work)
        session.flush()
        arrangement = Arrangement(work_id=work.id, name="SATB")
        session.add(arrangement)
        session.flush()
        score = Score(arrangement_id=arrangement.id, label="Choir", origin="manual")
        session.add(score)
        session.flush()
        revision = ScoreRevision(score_id=score.id, revision_no=1)
        session.add(revision)
        session.flush()
        score.head_revision_id = revision.id
        score.published_revision_id = revision.id
        arrangement.preferred_score_id = score.id
        return work.id, arrangement.id, score.id, revision.id


def test_chorus_upload_process_publish_and_mix(client: TestClient) -> None:
    work_id, arrangement_id, _, revision_id = _catalog_timeline(client)
    created_project = _post(
        client,
        f"/v2/works/{work_id}/chorus-projects",
        "project-create",
        {
            "arrangement_id": arrangement_id,
            "alignment_score_revision_id": revision_id,
            "timeline_hash": "a" * 64,
            "title": "Community SATB",
            "status": "open",
        },
    )
    assert created_project.status_code == 201, created_project.text
    project_id = created_project.json()["id"]

    audio = _wav_bytes()
    created_track = _post(
        client,
        f"/v2/chorus-projects/{project_id}/tracks",
        "track-create",
        {
            "contribution_kind": "harmony",
            "display_label": "Upper harmony",
            "sha256": hashlib.sha256(audio).hexdigest(),
            "byte_size": len(audio),
            "media_type": "audio/wav",
            "original_filename": "take.wav",
            "duration_ms": 300,
            "rights_confirmed": True,
            "initial_anchors": [
                {
                    "anchor_order": 0,
                    "score_tick": 0,
                    "media_ms": 0,
                    "confidence": 1.0,
                    "source": "in_app_clock",
                }
            ],
        },
    )
    assert created_track.status_code == 201, created_track.text
    track_id = created_track.json()["track"]["id"]
    assert created_track.json()["upload_status"] == "upload_required"
    assert created_track.json()["upload"]["url"] == f"/v2/chorus-tracks/{track_id}/content"

    uploaded = client.put(
        f"/v2/chorus-tracks/{track_id}/content",
        headers={**AUTH, "Content-Type": "audio/wav"},
        content=audio,
    )
    assert uploaded.status_code == 200, uploaded.text

    completed = _post(
        client,
        f"/v2/chorus-tracks/{track_id}/complete",
        "track-complete",
        {},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "processing"

    project = client.get(f"/v2/chorus-projects/{project_id}", headers=AUTH)
    assert project.status_code == 200
    processed_track = project.json()["tracks"][0]
    assert processed_track["status"] == "pending_review"
    assert processed_track["alignment_state"] == "automatic"
    assert processed_track["waveform_peaks"]

    aligned = client.patch(
        f"/v2/chorus-tracks/{track_id}/alignment",
        headers={**AUTH, "If-Match": f'"rev-{processed_track["revision"]}"'},
        json={
            "offset_ms": 25,
            "anchors": [
                {
                    "anchor_order": 0,
                    "score_tick": 0,
                    "media_ms": 25,
                    "confidence": 1.0,
                    "source": "manual",
                }
            ],
        },
    )
    assert aligned.status_code == 200, aligned.text
    assert aligned.json()["alignment_state"] == "manual"

    service = client.app.state.v2_container.chorus
    assert service.moderation_settings().automatic_approval is True
    assert (
        service.update_moderation_settings(False, ActorContext()).automatic_approval
        is False
    )
    submitted = _post(
        client,
        f"/v2/chorus-tracks/{track_id}/submit",
        "track-submit-manual-review",
        {},
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "pending_review"

    published = client.patch(
        f"/v2/chorus-tracks/{track_id}/moderation",
        headers={**AUTH, "If-Match": f'"rev-{submitted.json()["revision"]}"'},
        json={"status": "published", "gain_db": -1.5, "pan": 0.0},
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["alignment_state"] == "verified"

    resolved = _post(
        client,
        f"/v2/chorus-projects/{project_id}/mixes:resolve",
        "mix-resolve",
        {"track_ids": [track_id]},
    )
    assert resolved.status_code == 200, resolved.text
    mix_id = resolved.json()["id"]
    ready_mix = client.get(f"/v2/chorus-mixes/{mix_id}", headers=AUTH)
    assert ready_mix.status_code == 200, ready_mix.text
    assert ready_mix.json()["state"] == "ready"
    assert ready_mix.json()["selected_track_ids"] == [track_id]
    assert ready_mix.json()["delivery"]["delivery"] == "authenticated_url"
    assert ready_mix.json()["delivery"]["media_type"] == "audio/mp4"

    assert service.update_moderation_settings(True, ActorContext()).automatic_approval is True
    automatic_audio = _wav_bytes(320)
    automatic_track = _post(
        client,
        f"/v2/chorus-projects/{project_id}/tracks",
        "automatic-track-create",
        {
            "contribution_kind": "harmony",
            "display_label": "Automatically approved harmony",
            "sha256": hashlib.sha256(automatic_audio).hexdigest(),
            "byte_size": len(automatic_audio),
            "media_type": "audio/wav",
            "original_filename": "automatic.wav",
            "duration_ms": 320,
            "rights_confirmed": True,
        },
    ).json()["track"]
    automatic_id = automatic_track["id"]
    assert (
        client.put(
            f"/v2/chorus-tracks/{automatic_id}/content",
            headers={**AUTH, "Content-Type": "audio/wav"},
            content=automatic_audio,
        ).status_code
        == 200
    )
    assert (
        _post(
            client,
            f"/v2/chorus-tracks/{automatic_id}/complete",
            "automatic-track-complete",
            {},
        ).status_code
        == 200
    )
    automatic_submitted = _post(
        client,
        f"/v2/chorus-tracks/{automatic_id}/submit",
        "automatic-track-submit",
        {},
    )
    assert automatic_submitted.status_code == 200, automatic_submitted.text
    assert automatic_submitted.json()["status"] == "published"


def test_non_owner_cannot_read_draft_or_withdraw_track(client: TestClient) -> None:
    work_id, arrangement_id, _, revision_id = _catalog_timeline(client)
    project = _post(
        client,
        f"/v2/works/{work_id}/chorus-projects",
        "project-other",
        {
            "arrangement_id": arrangement_id,
            "alignment_score_revision_id": revision_id,
            "timeline_hash": "b" * 64,
            "title": "Private drafts",
        },
    ).json()
    audio = _wav_bytes(100)
    track = _post(
        client,
        f"/v2/chorus-projects/{project['id']}/tracks",
        "private-track",
        {
            "contribution_kind": "other",
            "display_label": "Private draft",
            "sha256": hashlib.sha256(audio).hexdigest(),
            "byte_size": len(audio),
            "media_type": "audio/wav",
            "original_filename": "draft.wav",
            "duration_ms": 100,
            "rights_confirmed": True,
        },
    ).json()["track"]

    # The private bootstrap API has one owner identity; exercise ownership at the same service
    # boundary used by the public gateway's signed device ActorContext.
    service = client.app.state.v2_container.chorus
    other = ActorContext(actor_id="someone-else")
    hidden = service.get_project(project["id"], other)
    assert hidden.tracks == []
    with pytest.raises(V2NotFound):
        service.withdraw_track(track["id"], other)
    service.withdraw_track(track["id"], ActorContext())
    assert service.get_project(project["id"], ActorContext()).tracks == []
