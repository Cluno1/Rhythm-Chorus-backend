"""Exercise destructive migration with synthetic shared and exclusive catalog assets."""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from rhythm_metadata_api.infrastructure.db import database as database_module
from rhythm_metadata_api.infrastructure.db.database import create_v2_engine
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement, Asset, AssetLocation, AuthUser, Rendition, RenditionAsset, Score,
    ScoreRenditionSync, ScoreRevision, ScoreRevisionAsset, Work,
)


def test_chorus_removal_preserves_shared_scores_assets_and_sync(tmp_path: Path) -> None:
    path = tmp_path / "migration.sqlite3"
    config = Config()
    config.set_main_option("script_location", str(Path(database_module.__file__).with_name("migrations")))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(config, "issue124rooms")
    engine = create_v2_engine(str(path))
    with Session(engine) as session, session.begin():
        work = Work(id="work", canonical_title="Kept")
        user = AuthUser(id="user", display_name="User")
        session.add_all([work, user]); session.flush()
        arr = Arrangement(id="arr", work_id=work.id, name="Kept arrangement")
        session.add(arr); session.flush()
        score = Score(id="score", arrangement_id=arr.id, label="Kept score", origin="manual")
        normal = Rendition(id="normal", arrangement_id=arr.id, label="Kept audio", kind="performance")
        chorus = Rendition(id="chorus", arrangement_id=arr.id, label="Removed audio", kind="chorus_track")
        shared = Asset(id="shared", sha256="a" * 64, byte_size=10, detected_media_type="audio/mpeg", state="ready")
        exclusive = Asset(id="exclusive", sha256="b" * 64, byte_size=10, detected_media_type="audio/mpeg", state="ready")
        session.add_all([score, normal, chorus, shared, exclusive]); session.flush()
        revision = ScoreRevision(id="rev", score_id=score.id, revision_no=1)
        session.add(revision); session.flush()
        session.add_all([
            ScoreRevisionAsset(score_revision_id=revision.id, asset_id=shared.id, role="primary_musicxml"),
            RenditionAsset(rendition_id=chorus.id, asset_id=shared.id, role="master"),
            RenditionAsset(rendition_id=chorus.id, asset_id=exclusive.id, role="stream"),
            AssetLocation(asset_id=exclusive.id, provider="cos", storage_key="chorus/x", state="available"),
            ScoreRenditionSync(score_revision_id=revision.id, rendition_id=normal.id, anchor_order=0, score_tick=0, media_ms=0, source="manual"),
            ScoreRenditionSync(score_revision_id=revision.id, rendition_id=chorus.id, anchor_order=0, score_tick=0, media_ms=0, source="manual"),
        ])
        session.flush()
        now = "2026-11-02 00:00:00"
        session.execute(text("""
            INSERT INTO v2_chorus_projects (id,work_id,arrangement_id,score_id,
              alignment_score_revision_id,timeline_hash,title,status,created_by_user_id,
              revision,created_at,updated_at)
            VALUES ('project','work','arr','score','rev',:hash,'Project','open','user',1,:now,:now)
        """), {"hash": "a" * 64, "now": now})
        session.execute(text("""
            INSERT INTO v2_chorus_timelines (id,chorus_project_id,score_revision_id,timeline_hash,
              revision,created_at,updated_at) VALUES ('timeline','project','rev',:hash,1,:now,:now)
        """), {"hash": "a" * 64, "now": now})
        session.execute(text("""
            INSERT INTO v2_chorus_tracks (id,chorus_project_id,chorus_timeline_id,rendition_id,
              uploader_user_id,contribution_kind,display_label,take_no,alignment_state,
              alignment_offset_ms,status,gain_millibels,pan_milli,revision,created_at,updated_at)
            VALUES ('track','project','timeline','chorus','user','harmony','Voice',1,'manual',
              0,'published',0,0,1,:now,:now)
        """), {"now": now})
    engine.dispose()
    command.upgrade(config, "head")
    engine = create_v2_engine(str(path))
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        assert not any(table.startswith("v2_chorus_") for table in tables)
        assert connection.execute(text("select id from v2_assets order by id")).scalars().all() == ["shared"]
        assert connection.execute(text("select id from v2_renditions")).scalars().all() == ["normal"]
        assert connection.execute(text("select rendition_id from v2_score_rendition_sync")).scalars().all() == ["normal"]
        assert connection.execute(text("select count(*) from v2_score_revision_assets")).scalar_one() == 1
        assert connection.execute(text("select count(*) from v2_asset_locations")).scalar_one() == 0
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    engine.dispose()
    with pytest.raises(RuntimeError, match="permanently removed"):
        command.downgrade(config, "issue124rooms")
