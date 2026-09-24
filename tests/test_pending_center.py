from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    Asset,
    AssetLocation,
    Score,
    UploadSession,
    Work,
    utc_now,
)
from rhythm_metadata_api.main import create_app


def test_pending_center_aggregates_without_mutating_catalog(tmp_path: Path) -> None:
    settings = Settings(
        bootstrap_token="pending-test-token",
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
    )
    with TestClient(create_app(settings)) as client:
        with Session(client.app.state.v2_container.engine) as session, session.begin():
            work = Work(canonical_title="Review Work")
            session.add(work)
            session.flush()
            arrangement = Arrangement(work_id=work.id, name="SATB")
            session.add(arrangement)
            session.flush()
            score = Score(arrangement_id=arrangement.id, label="Unpublished", origin="manual")
            rejected = Asset(sha256="a" * 64, byte_size=100, detected_media_type="image/png", state="rejected")
            pending = Asset(sha256="b" * 64, byte_size=200, detected_media_type="audio/mpeg", state="pending_inspection")
            missing = Asset(sha256="c" * 64, byte_size=300, detected_media_type="application/xml", state="ready")
            session.add_all((score, rejected, pending, missing))
            session.flush()
            session.add(AssetLocation(asset_id=missing.id, provider="cos", storage_key="missing.xml", state="missing"))
            expired = UploadSession(
                expected_sha256="d" * 64, expected_size=400, media_type="audio/wav",
                original_filename="old.wav", state="created", expires_at=utc_now() - timedelta(days=1),
            )
            failed = UploadSession(
                expected_sha256="e" * 64, expected_size=500, media_type="audio/wav",
                original_filename="failed.wav", state="failed", expires_at=utc_now() + timedelta(days=1),
            )
            session.add_all((expired, failed))

        path = "/v2/management/pending"
        assert client.get(path).status_code == 401
        response = client.get(path, headers={"Authorization": "Bearer pending-test-token"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["tracks"] == {"items": [], "total": 0}
        assert body["track_counts"] == {"pending_review": 0, "published": 0, "rejected": 0}
        assert body["unpublished_scores"]["total"] == 1
        assert body["unpublished_scores"]["items"][0]["label"] == "Unpublished"
        assert body["failed_uploads"]["total"] == 2
        assert {item["state"] for item in body["failed_uploads"]["items"]} == {"failed", "expired"}
        assert body["failed_assets"]["total"] == 2
        assert {item["state"] for item in body["failed_assets"]["items"]} == {"rejected", "location_missing"}
        assert body["pending_assets"]["total"] == 1

        updated = client.patch(
            "/v2/management/chorus-moderation-settings",
            headers={"Authorization": "Bearer pending-test-token"},
            json={"automatic_approval": False},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["automatic_approval"] is False
        assert client.get(path, headers={"Authorization": "Bearer pending-test-token"}).json()["moderation_settings"]["automatic_approval"] is False
