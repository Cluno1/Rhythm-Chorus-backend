"""Validate or import issue 12 manifests into a clean/staging v2 SQLite database."""

from __future__ import annotations

import argparse
import json
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
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-manifest", type=Path, required=True)
    parser.add_argument("--song-mapping", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-production-path",
        action="store_true",
        help="required if the target filename is rhythm-v2.sqlite3",
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
    if target.name == "rhythm-v2.sqlite3" and not args.allow_production_path:
        parser.error("refusing production-like path; import a staging database first")
    engine = create_v2_engine(str(target))
    migrate_v2_database(engine, str(target))
    with Session(engine) as session, session.begin():
        result = FormalCatalogImporter(session).import_all(score_rows, song_rows)
    print(json.dumps({"mode": "import", "database": str(target), **asdict(result)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
