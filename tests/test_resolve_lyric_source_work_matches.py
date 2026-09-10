from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "resolve_lyric_source_work_matches.py"
SPEC = importlib.util.spec_from_file_location("resolve_lyric_source_work_matches", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

WorkMatchError = MODULE.WorkMatchError
build_matches = MODULE.build_matches
load_catalog = MODULE.load_catalog
normalize_title = MODULE.normalize_title


def catalog() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE v2_works (
            id TEXT PRIMARY KEY, canonical_title TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE v2_arrangements (
            id TEXT PRIMARY KEY, work_id TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE v2_renditions (
            id TEXT PRIMARY KEY, arrangement_id TEXT NOT NULL,
            label TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE v2_releases (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE v2_release_items (
            id TEXT PRIMARY KEY, release_id TEXT NOT NULL,
            rendition_id TEXT NOT NULL, track_no INTEGER
        );
        """
    )
    return connection


def add_version(
    connection: sqlite3.Connection,
    *,
    work_id: str,
    canonical_title: str,
    rendition_id: str,
    label: str,
    track_no: int,
) -> None:
    arrangement_id = f"arrangement-{rendition_id}"
    connection.execute(
        "INSERT OR IGNORE INTO v2_works VALUES (?, ?, NULL)",
        (work_id, canonical_title),
    )
    connection.execute(
        "INSERT INTO v2_arrangements VALUES (?, ?, NULL)",
        (arrangement_id, work_id),
    )
    connection.execute(
        "INSERT INTO v2_renditions VALUES (?, ?, ?, NULL)",
        (rendition_id, arrangement_id, label),
    )
    connection.execute(
        "INSERT OR IGNORE INTO v2_releases VALUES ('release-ihope', 'ihope', NULL)"
    )
    connection.execute(
        "INSERT INTO v2_release_items VALUES (?, 'release-ihope', ?, ?)",
        (f"item-{rendition_id}", rendition_id, track_no),
    )


def song(number: int, title: str) -> dict[str, object]:
    return {
        "songbook_number": number,
        "folder": title,
        "title_zh_hans": title,
        "title_en": "",
    }


def test_simplified_rendition_title_resolves_traditional_work() -> None:
    connection = catalog()
    add_version(
        connection,
        work_id="work-redeemer",
        canonical_title="我確知我救主活著",
        rendition_id="rendition-redeemer",
        label="我确知我救主活着",
        track_no=118,
    )
    works, titles, tracks = load_catalog(connection, "ihope")

    rows = build_matches([song(118, "我确知我救主活着")], works, titles, tracks, {})

    assert rows[0]["work_id"] == "work-redeemer"
    assert rows[0]["canonical_title"] == "我確知我救主活著"
    assert rows[0]["status"] == "resolved_existing_rendition"


def test_versions_are_collapsed_by_work_id() -> None:
    connection = catalog()
    add_version(
        connection,
        work_id="same-work",
        canonical_title="Traditional title",
        rendition_id="version-a",
        label="Shared title",
        track_no=10,
    )
    add_version(
        connection,
        work_id="same-work",
        canonical_title="Traditional title",
        rendition_id="version-b",
        label="Shared title",
        track_no=20,
    )
    works, titles, tracks = load_catalog(connection, "ihope")

    rows = build_matches([song(10, "Shared title")], works, titles, tracks, {})

    assert rows[0]["work_id"] == "same-work"


def test_same_title_different_works_requires_unique_relationship() -> None:
    connection = catalog()
    add_version(
        connection,
        work_id="work-a",
        canonical_title="A",
        rendition_id="rendition-a",
        label="Shared title",
        track_no=10,
    )
    add_version(
        connection,
        work_id="work-b",
        canonical_title="B",
        rendition_id="rendition-b",
        label="Shared title",
        track_no=11,
    )
    works, titles, tracks = load_catalog(connection, "ihope")

    resolved = build_matches([song(10, "Shared title")], works, titles, tracks, {})
    ambiguous = build_matches([song(12, "Shared title")], works, titles, tracks, {})

    assert resolved[0]["work_id"] == "work-a"
    assert ambiguous[0]["status"] == "ambiguous_skipped"
    assert ambiguous[0]["work_id"] == ""


def test_reviewed_work_id_is_preserved_and_validated() -> None:
    connection = catalog()
    add_version(
        connection,
        work_id="reviewed-work",
        canonical_title="Reviewed",
        rendition_id="reviewed-rendition",
        label="Different title",
        track_no=1,
    )
    works, titles, tracks = load_catalog(connection, "ihope")
    existing = {1: {"work_id": "reviewed-work", "status": "updated", "detail": "reviewed"}}

    rows = build_matches([song(1, "Source title")], works, titles, tracks, existing)

    assert rows[0]["work_id"] == "reviewed-work"
    with pytest.raises(WorkMatchError, match="missing Work"):
        build_matches(
            [song(1, "Source title")],
            works,
            titles,
            tracks,
            {1: {"work_id": "missing-work"}},
        )


def test_normalization_is_strict_but_ignores_case_and_whitespace() -> None:
    assert normalize_title("  I   Have DECIDED ") == "i have decided"
    assert normalize_title("唱，哈利路亚") != normalize_title("唱,哈利路亚")
