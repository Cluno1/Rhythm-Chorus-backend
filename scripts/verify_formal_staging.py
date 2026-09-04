"""Fail-fast structural verification for an issue 12 staging SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

EXPECTED_COUNTS = {
    "v2_works": 107,
    "v2_scores": 77,
    "v2_score_revisions": 93,
    "v2_renditions": 73,
    "v2_releases": 1,
    "v2_release_items": 73,
    "v2_assets": 166,
    "v2_asset_locations": 166,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database.resolve()}?mode=ro", uri=True)
    failures: list[str] = []
    report: dict[str, object] = {}

    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    report["integrity"] = integrity
    report["foreign_key_violations"] = len(foreign_keys)
    if integrity != "ok" or foreign_keys:
        failures.append("SQLite integrity or foreign keys failed")

    counts = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in EXPECTED_COUNTS
    }
    report["counts"] = counts
    if counts != EXPECTED_COUNTS:
        failures.append(f"count mismatch: expected={EXPECTED_COUNTS}, actual={counts}")

    checks = {
        "ihope_albums": "SELECT count(*) FROM v2_releases WHERE key='ihope' AND title='ihope'",
        "ihope_items": """
            SELECT count(*) FROM v2_release_items ri
            JOIN v2_releases r ON r.id=ri.release_id WHERE r.key='ihope'
        """,
        "local_locations": "SELECT count(*) FROM v2_asset_locations WHERE provider='local'",
        "cos_locations": """
            SELECT count(*) FROM v2_asset_locations
            WHERE provider='cos' AND state='available'
        """,
        "published_scores": """
            SELECT count(*) FROM v2_scores
            WHERE published_revision_id IS NOT NULL AND head_revision_id IS NOT NULL
        """,
        "preferred_arrangements": """
            SELECT count(*) FROM v2_arrangements WHERE preferred_score_id IS NOT NULL
        """,
        "primary_musicxml_links": """
            SELECT count(*) FROM v2_score_revision_assets WHERE role='primary_musicxml'
        """,
        "stream_links": "SELECT count(*) FROM v2_rendition_assets WHERE role='stream'",
        "change_events": "SELECT count(*) FROM v2_change_events",
        "works_with_bundle_version": """
            SELECT count(DISTINCT work_id) FROM v2_change_event_works
        """,
        "broken_revision_parents": """
            SELECT count(*) FROM v2_score_revisions r
            WHERE r.revision_no > (
                SELECT min(r2.revision_no) FROM v2_score_revisions r2 WHERE r2.score_id=r.score_id
            ) AND r.based_on_revision_id IS NULL
        """,
    }
    values = {name: connection.execute(query).fetchone()[0] for name, query in checks.items()}
    report["checks"] = values
    required = {
        "ihope_albums": 1,
        "ihope_items": 73,
        "local_locations": 0,
        "cos_locations": 166,
        "published_scores": 77,
        "preferred_arrangements": 77,
        "primary_musicxml_links": 93,
        "stream_links": 73,
        "change_events": 458,
        "works_with_bundle_version": 107,
        "broken_revision_parents": 0,
    }
    if values != required:
        failures.append(f"business invariant mismatch: expected={required}, actual={values}")

    report["status"] = "failed" if failures else "ok"
    report["failures"] = failures
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
