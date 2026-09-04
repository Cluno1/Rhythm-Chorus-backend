from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, aliased

from rhythm_metadata_api.application.catalog_service import CatalogService
from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.database import (
    create_session_factory,
    create_v2_engine,
    migrate_v2_database,
)
from rhythm_metadata_api.infrastructure.db.models import AssetLocation
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
        with Session(engine) as session:
            cos_location = aliased(AssetLocation)
            local_location = aliased(AssetLocation)
            cos_only_locations = session.scalar(
                select(func.count(cos_location.id)).where(
                    cos_location.provider == "cos",
                    cos_location.state == "available",
                    ~select(local_location.id)
                    .where(
                        local_location.asset_id == cos_location.asset_id,
                        local_location.provider == "local",
                        local_location.state == "available",
                    )
                    .exists(),
                )
            )
        if cos_only_locations and not (settings.cos_secret_id and settings.cos_secret_key):
            engine.dispose()
            raise ValueError("COS credentials are required when the catalog has COS asset locations")
        sessions = create_session_factory(engine)
        storage = LocalAssetStorage(settings.local_object_root)
        catalog = CatalogService(UnitOfWorkFactory(sessions), storage, settings)
        return cls(settings=settings, engine=engine, catalog=catalog)

    def close(self) -> None:
        self.engine.dispose()
