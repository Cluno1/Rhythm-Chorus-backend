"""Validate or import issue 12 manifests into a clean/staging v2 SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.orm import Session

from rhythm_metadata_api.application.formal_import import (
    FormalCatalogImporter,
    read_score_manifest,
    read_song_mapping,
)
from rhythm_metadata_api.infrastructure.db.database import create_v2_engine, migrate_v2_database

ISSUE12_EXPECTED_COUNTS = {
    "works": 107,
    "scores": 77,
    "score_revisions": 93,
    "songs": 73,
    "albums": 1,
    "release_items": 73,
    "assets": 166,
    "cos_locations": 166,
    "contributors": 108,
    "work_credits": 129,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-manifest", type=Path, required=True)
    parser.add_argument("--song-mapping", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--replay-existing-staging",
        action="store_true",
        help="replay an already-validated staging DB; never enables a production path",
    )
    args = parser.parse_args()
    score_rows = read_score_manifest(args.score_manifest)
    song_rows = read_song_mapping(args.song_mapping)

    validation_engine = create_v2_engine(":memory:")
    migrate_v2_database(validation_engine, ":memory:")
    with Session(validation_engine) as validation_session:
        expected = FormalCatalogImporter(validation_session).validate(score_rows, song_rows)
    actual_counts = asdict(expected)
    if actual_counts != ISSUE12_EXPECTED_COUNTS:
        raise SystemExit(
            "issue 12 manifest count drift: "
            f"expected={ISSUE12_EXPECTED_COUNTS}, actual={actual_counts}"
        )

    if args.dry_run:
        print(json.dumps({"mode": "dry-run", **actual_counts}, ensure_ascii=False, indent=2))
        return

    if args.database is None:
        parser.error("--database is required unless --dry-run is used")
    target = args.database.expanduser().resolve()
    if "staging" not in target.name.casefold():
        parser.error("target filename must contain 'staging'; production DB import is forbidden")
    if target.exists() and not args.replay_existing_staging:
        parser.error("target already exists; use a new staging file")
    if target.exists():
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
        if version != ("issue12release",) or integrity != ("ok",):
            parser.error("existing staging DB is not an intact issue12release database")
    engine = create_v2_engine(str(target))
    if not args.replay_existing_staging:
        migrate_v2_database(engine, str(target))
    with Session(engine) as session, session.begin():
        result = FormalCatalogImporter(session).import_all(score_rows, song_rows)
    print(json.dumps({"mode": "import", "database": str(target), **asdict(result)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
