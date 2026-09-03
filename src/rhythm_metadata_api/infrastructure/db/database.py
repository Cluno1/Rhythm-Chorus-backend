from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from rhythm_metadata_api.infrastructure.db.models import Base


def sqlite_url(database_path: str) -> str:
    if database_path == ":memory:":
        return "sqlite+pysqlite:///:memory:"
    path = Path(database_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{path}"


def create_v2_engine(database_path: str) -> Engine:
    kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
    if database_path == ":memory:":
        kwargs["poolclass"] = StaticPool
    engine = create_engine(sqlite_url(database_path), **kwargs)

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        if database_path != ":memory:":
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.close()

    return engine


def migrate_v2_database(engine: Engine, database_path: str) -> None:
    if database_path == ":memory:":
        Base.metadata.create_all(engine)
        return
    config = Config()
    migrations = Path(__file__).with_name("migrations")
    config.set_main_option("script_location", str(migrations))
    config.set_main_option("sqlalchemy.url", str(engine.url).replace("%", "%%"))
    command.upgrade(config, "head")


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
