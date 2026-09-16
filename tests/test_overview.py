from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    Asset,
    ChangeEvent,
    ChangeEventWork,
    Rendition,
    Score,
    Work,
    utc_now,
)
from rhythm_metadata_api.main import create_app


def test_management_overview_counts_live_catalog_and_recent_activity(tmp_path: Path) -> None:
    settings = Settings(
        bootstrap_token="overview-test-token",
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
    )
    with TestClient(create_app(settings)) as client:
        with Session(client.app.state.v2_container.engine) as session, session.begin():
            work = Work(canonical_title="Live Work", status="active")
            deleted_work = Work(canonical_title="Deleted Work", deleted_at=utc_now())
            session.add_all((work, deleted_work))
            session.flush()
            arrangement = Arrangement(work_id=work.id, name="SATB")
            deleted_arrangement = Arrangement(work_id=deleted_work.id, name="Old")
            session.add_all((arrangement, deleted_arrangement))
            session.flush()
            session.add_all((
                Score(arrangement_id=arrangement.id, label="Unpublished", origin="manual"),
                Score(arrangement_id=deleted_arrangement.id, label="Hidden", origin="manual"),
                Rendition(arrangement_id=arrangement.id, label="Recording", kind="recording"),
                Asset(sha256="a" * 64, byte_size=100, detected_media_type="image/png", state="ready"),
            ))
            event = ChangeEvent(entity_type="work", entity_id=work.id, entity_revision=1, operation="work.created")
            session.add(event)
            session.flush()
            session.add(ChangeEventWork(event_sequence=event.sequence, work_id=work.id))

        path = "/v2/management/overview"
        assert client.get(path).status_code == 401
        response = client.get(path, headers={"Authorization": "Bearer overview-test-token"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["counts"] == {
            "works": 1,
            "active_works": 1,
            "arrangements": 1,
            "scores": 1,
            "published_scores": 0,
            "renditions": 1,
            "assets": 1,
            "ready_assets": 1,
            "chorus_projects": 0,
        }
        assert body["attention"]["unpublished_scores"] == 1
        assert body["recent_works"][0]["title"] == "Live Work"
        assert body["recent_works"][0]["updated_at"].endswith("+00:00")
        assert body["recent_events"][0]["operation"] == "work.created"
