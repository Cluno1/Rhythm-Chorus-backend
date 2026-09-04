from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rhythm_metadata_api.core.config import Settings
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
