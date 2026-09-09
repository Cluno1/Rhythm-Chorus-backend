import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db import database as database_module
from rhythm_metadata_api.infrastructure.db.database import create_v2_engine, migrate_v2_database
from rhythm_metadata_api.infrastructure.db.models import Asset, AssetLocation
from rhythm_metadata_api.main import create_app


def seed_cos_asset(path: Path, *, local_fallback: bool) -> None:
    engine = create_v2_engine(str(path))
    migrate_v2_database(engine, str(path))
    with Session(engine) as session, session.begin():
        asset = Asset(
            sha256="9" * 64,
            byte_size=9,
            detected_media_type="audio/mpeg",
            state="ready",
        )
        session.add(asset)
        session.flush()
        session.add(
            AssetLocation(
                asset_id=asset.id,
                provider="cos",
                storage_key="bucket/music/test.mp3",
            )
        )
        if local_fallback:
            session.add(
                AssetLocation(
                    asset_id=asset.id,
                    provider="local",
                    storage_key="sha256/99/test.mp3",
                )
            )
    engine.dispose()


def test_startup_rejects_cos_only_catalog_without_credentials(tmp_path: Path) -> None:
    database = tmp_path / "cos-only.sqlite3"
    seed_cos_asset(database, local_fallback=False)
    settings = Settings(bootstrap_token="test", v2_database_path=str(database))
    with (
        pytest.raises(ValueError, match="COS credentials are required"),
        TestClient(create_app(settings)),
    ):
        pass


def test_startup_allows_local_fallback_without_cos_credentials(tmp_path: Path) -> None:
    database = tmp_path / "with-local.sqlite3"
    seed_cos_asset(database, local_fallback=True)
    settings = Settings(bootstrap_token="test", v2_database_path=str(database))
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200


def test_settings_requires_cos_credential_pair() -> None:
    with pytest.raises(ValueError, match="must be set together"):
        Settings(cos_secret_id="only-id")


def test_multilingual_lyrics_migration_backfills_existing_rows(tmp_path: Path) -> None:
    database = tmp_path / "multilingual-upgrade.sqlite3"
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(database_module.__file__).with_name("migrations")),
    )
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+pysqlite:///{database}",
    )
    command.upgrade(config, "issue15updateidentity")

    timestamp = "2026-09-09T00:00:00+00:00"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executemany(
        """
        INSERT INTO v2_works (
            id, canonical_title, language, status, lyrics,
            revision, created_at, updated_at, deleted_at
        ) VALUES (?, ?, ?, 'active', ?, 1, ?, ?, NULL)
        """,
        [
            ("work-zh", "中文作品", "zh-Hans", "中文歌词", timestamp, timestamp),
            ("work-und", "Unknown Work", None, "Unknown lyrics", timestamp, timestamp),
        ],
    )
    connection.executemany(
        """
        INSERT INTO v2_arrangements (
            id, work_id, name, revision, created_at, updated_at, deleted_at
        ) VALUES (?, ?, 'Default', 1, ?, ?, NULL)
        """,
        [
            ("arrangement-zh", "work-zh", timestamp, timestamp),
            ("arrangement-und", "work-und", timestamp, timestamp),
        ],
    )
    connection.execute(
        """
        INSERT INTO v2_scores (
            id, arrangement_id, label, origin, lyrics,
            revision, created_at, updated_at, deleted_at
        ) VALUES (
            'score-zh', 'arrangement-zh', 'Score', 'manual', '乐谱歌词',
            1, ?, ?, NULL
        )
        """,
        (timestamp, timestamp),
    )
    connection.execute(
        """
        INSERT INTO v2_renditions (
            id, arrangement_id, label, kind, lyrics,
            revision, created_at, updated_at, deleted_at
        ) VALUES (
            'rendition-und', 'arrangement-und', 'Song', 'performance', 'Song lyrics',
            1, ?, ?, NULL
        )
        """,
        (timestamp, timestamp),
    )
    connection.commit()
    connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(database)
    work_rows = connection.execute(
        "SELECT id, lyrics_language, lyrics_translations FROM v2_works ORDER BY id"
    ).fetchall()
    score_row = connection.execute(
        "SELECT lyrics_language, lyrics_translations FROM v2_scores WHERE id = 'score-zh'"
    ).fetchone()
    rendition_row = connection.execute(
        """
        SELECT lyrics_language, lyrics_translations
          FROM v2_renditions
         WHERE id = 'rendition-und'
        """
    ).fetchone()
    version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    connection.close()

    assert version == "issue32multilyrics"
    assert [(row[0], row[1], json.loads(row[2])) for row in work_rows] == [
        ("work-und", "und", []),
        ("work-zh", "zh-Hans", []),
    ]
    assert (score_row[0], json.loads(score_row[1])) == ("zh-Hans", [])
    assert (rendition_row[0], json.loads(rendition_row[1])) == ("und", [])
