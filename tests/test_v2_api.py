from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rhythm_metadata_api.core.config import Settings
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
    ]


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
