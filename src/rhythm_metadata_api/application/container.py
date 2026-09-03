from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine

from rhythm_metadata_api.application.catalog_service import CatalogService
from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.database import (
    create_session_factory,
    create_v2_engine,
    migrate_v2_database,
)
from rhythm_metadata_api.infrastructure.storage.local import LocalAssetStorage


@dataclass
class V2Container:
    settings: Settings
    engine: Engine
    catalog: CatalogService

    @classmethod
    def build(cls, settings: Settings) -> V2Container:
        engine = create_v2_engine(settings.v2_database_path)
        migrate_v2_database(engine, settings.v2_database_path)
        sessions = create_session_factory(engine)
        storage = LocalAssetStorage(settings.local_object_root)
        catalog = CatalogService(UnitOfWorkFactory(sessions), storage, settings)
        return cls(settings=settings, engine=engine, catalog=catalog)

    def close(self) -> None:
        self.engine.dispose()
