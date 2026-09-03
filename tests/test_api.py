from fastapi.testclient import TestClient

from rhythm_metadata_api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer change-me-in-development"}


def test_health_is_public() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "sqlite"}


def test_tracks_require_authentication() -> None:
    response = client.post("/v1/tracks/match", json={"audio_sha256": "a" * 64})
    assert response.status_code == 401


def test_match_patch_and_stale_revision_conflict() -> None:
    matched = client.post(
        "/v1/tracks/match",
        headers=AUTH,
        json={"audio_sha256": "c" * 64},
    )
    assert matched.status_code == 200
    track = matched.json()

    payload = {
        "fields": {
            "lyrics": {
                "value": "hello",
                "content_hash": "lyrics-v1",
                "pin": True,
            }
        }
    }
    patched = client.patch(
        f"/v1/tracks/{track['id']}/overrides",
        headers={**AUTH, "If-Match": str(track["revision"])},
        json=payload,
    )
    assert patched.status_code == 200
    assert patched.json()["resolved"]["lyrics"]["value"] == "hello"

    stale = client.patch(
        f"/v1/tracks/{track['id']}/overrides",
        headers={**AUTH, "If-Match": str(track["revision"])},
        json=payload,
    )
    assert stale.status_code == 409


def test_artifact_round_trip_and_history() -> None:
    matched = client.post(
        "/v1/tracks/match",
        headers=AUTH,
        json={"audio_sha256": "d" * 64},
    ).json()
    uploaded = client.put(
        f"/v1/tracks/{matched['id']}/artifacts/lyrics",
        headers={**AUTH, "If-Match": str(matched["revision"]), "Content-Type": "text/plain"},
        content=b"[00:00.00] hello",
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["revision"] == 2

    downloaded = client.get(
        f"/v1/tracks/{matched['id']}/artifacts/lyrics",
        headers=AUTH,
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"[00:00.00] hello"

    history = client.get(f"/v1/tracks/{matched['id']}/history", headers=AUTH)
    assert history.status_code == 200
    assert [event["operation"] for event in history.json()["events"]] == [
        "artifact.stored",
        "track.created",
    ]


def test_binary_artifact_kinds_round_trip() -> None:
    artifact_payloads = {
        "artwork": (b"fake-png", "image/png"),
        "audio": (b"fake-audio", "audio/mpeg"),
        "musicxml": (
            b"<?xml version='1.0'?><score-partwise/>",
            "application/vnd.recordare.musicxml+xml",
        ),
        "midi": (b"MThd\x00\x00\x00\x06", "audio/midi"),
    }
    for index, (kind, (payload, content_type)) in enumerate(artifact_payloads.items()):
        matched = client.post(
            "/v1/tracks/match",
            headers=AUTH,
            json={"audio_sha256": f"{index + 1:064x}"},
        ).json()
        uploaded = client.put(
            f"/v1/tracks/{matched['id']}/artifacts/{kind}",
            headers={**AUTH, "If-Match": str(matched["revision"]), "Content-Type": content_type},
            content=payload,
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["kind"] == kind
        assert uploaded.json()["size"] == len(payload)

        downloaded = client.get(
            f"/v1/tracks/{matched['id']}/artifacts/{kind}",
            headers=AUTH,
        )
        assert downloaded.status_code == 200
        assert downloaded.content == payload
        assert downloaded.headers["content-type"] == content_type


def test_two_edited_candidates_create_an_explicit_conflict() -> None:
    matched = client.post(
        "/v1/tracks/match",
        headers=AUTH,
        json={"audio_sha256": "9" * 64},
    ).json()
    local = client.put(
        f"/v1/tracks/{matched['id']}/candidates",
        headers={**AUTH, "If-Match": str(matched["revision"])},
        json={
            "fields": {
                "lyrics": [
                    {
                        "value": "local lyrics",
                        "content_hash": "local-v1",
                        "source": "local_sidecar",
                        "source_ref": "device:mac",
                        "user_edited": True,
                    }
                ]
            }
        },
    )
    assert local.status_code == 200
    cloud = client.put(
        f"/v1/tracks/{matched['id']}/candidates",
        headers={**AUTH, "If-Match": str(local.json()["revision"])},
        json={
            "policy": "ask_on_difference",
            "fields": {
                "lyrics": [
                    {
                        "value": "cloud lyrics",
                        "content_hash": "cloud-v1",
                        "source": "rhythm_cloud",
                        "source_ref": "server:primary",
                        "user_edited": True,
                    }
                ]
            },
        },
    )
    assert cloud.status_code == 200
    assert "lyrics" not in cloud.json()["resolved"]
    assert len(cloud.json()["conflicts"]["lyrics"]) == 2
