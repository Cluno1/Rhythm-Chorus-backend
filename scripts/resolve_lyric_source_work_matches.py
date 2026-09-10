"""Resolve extracted lyric-source songs to existing catalog Works.

The resolver deliberately follows catalog relationships instead of requiring an
extracted title to equal ``Work.canonical_title``. An exact normalized match to a
Rendition label is followed through ``Rendition -> Arrangement -> Work`` and all
matching versions are collapsed by Work ID. Existing reviewed Work IDs may be
supplied and are preserved after validation.

No fuzzy, punctuation, or track-number-only matching is performed. When the same
title reaches multiple Works, a release track may disambiguate it only if the
title already matched a Rendition and the track number equals the songbook number.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorkMatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogTitle:
    work_id: str
    canonical_title: str
    source: str
    title: str
    rendition_id: str | None = None


OUTPUT_FIELDS = (
    "songbook_number",
    "folder",
    "title_zh_hans",
    "title_en",
    "status",
    "work_id",
    "canonical_title",
    "detail",
)


def normalize_title(value: str) -> str:
    """Apply the intentionally strict title normalization used for identity lookup."""
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise WorkMatchError(
                    f"invalid manifest JSON on line {line_number}"
                ) from error
            number = int(row.get("songbook_number") or 0)
            if number < 1:
                raise WorkMatchError(f"invalid songbook number on line {line_number}")
            rows.append(row)
    if len({int(row["songbook_number"]) for row in rows}) != len(rows):
        raise WorkMatchError("manifest contains duplicate songbook numbers")
    return rows


def read_existing_matches(path: Path | None) -> dict[int, dict[str, str]]:
    if path is None:
        return {}
    with path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    result = {}
    for row in rows:
        number = int(row["songbook_number"])
        if number in result:
            raise WorkMatchError(f"duplicate existing match for songbook number {number}")
        result[number] = row
    return result


def load_catalog(
    connection: sqlite3.Connection,
    release_key: str | None,
) -> tuple[
    dict[str, str],
    dict[str, list[CatalogTitle]],
    dict[tuple[str, int], set[str]],
]:
    works = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            """
            SELECT id, canonical_title
            FROM v2_works
            WHERE deleted_at IS NULL
            """
        )
    }
    titles: dict[str, list[CatalogTitle]] = defaultdict(list)
    for work_id, title in works.items():
        titles[normalize_title(title)].append(
            CatalogTitle(work_id, title, "work", title)
        )
    for row in connection.execute(
        """
        SELECT w.id, w.canonical_title, r.id, r.label
        FROM v2_renditions r
        JOIN v2_arrangements a ON a.id = r.arrangement_id
        JOIN v2_works w ON w.id = a.work_id
        WHERE r.deleted_at IS NULL
          AND a.deleted_at IS NULL
          AND w.deleted_at IS NULL
        """
    ):
        work_id, canonical_title, rendition_id, label = map(str, row)
        titles[normalize_title(label)].append(
            CatalogTitle(
                work_id,
                canonical_title,
                "rendition",
                label,
                rendition_id,
            )
        )

    release_tracks: dict[tuple[str, int], set[str]] = defaultdict(set)
    if release_key:
        for rendition_id, track_no in connection.execute(
            """
            SELECT ri.rendition_id, ri.track_no
            FROM v2_release_items ri
            JOIN v2_releases rel ON rel.id = ri.release_id
            WHERE rel.key = ?
              AND rel.deleted_at IS NULL
              AND ri.track_no IS NOT NULL
            """,
            (release_key,),
        ):
            release_tracks[(str(rendition_id), int(track_no))].add(release_key)
    return works, titles, release_tracks


def source_titles(song: dict[str, Any]) -> list[str]:
    values = []
    seen = set()
    for key in ("folder", "title_zh_hans", "title_en"):
        title = str(song.get(key) or "").strip()
        normalized = normalize_title(title)
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(title)
    return values


def resolve_song(
    song: dict[str, Any],
    titles: dict[str, list[CatalogTitle]],
    release_tracks: dict[tuple[str, int], set[str]],
) -> tuple[str, str, str, str]:
    evidence: dict[str, list[CatalogTitle]] = defaultdict(list)
    for title in source_titles(song):
        for match in titles.get(normalize_title(title), []):
            evidence[match.work_id].append(match)

    if len(evidence) > 1 and release_tracks:
        number = int(song["songbook_number"])
        tracked_work_ids = {
            work_id
            for work_id, matches in evidence.items()
            if any(
                match.rendition_id is not None
                and (match.rendition_id, number) in release_tracks
                for match in matches
            )
        }
        if len(tracked_work_ids) == 1:
            selected = tracked_work_ids.pop()
            evidence = {selected: evidence[selected]}

    if not evidence:
        return "unmatched_skipped", "", "", "no exact Work or Rendition title match"
    if len(evidence) > 1:
        return (
            "ambiguous_skipped",
            "",
            "",
            "multiple Work IDs: " + ", ".join(sorted(evidence)),
        )

    work_id, matches = next(iter(evidence.items()))
    canonical_title = matches[0].canonical_title
    sources = sorted({match.source for match in matches})
    status = (
        "resolved_existing_rendition"
        if "rendition" in sources
        else "resolved_exact_work_title"
    )
    detail = ",".join(sources) + " exact title -> unique Work"
    return status, work_id, canonical_title, detail


def build_matches(
    songs: list[dict[str, Any]],
    works: dict[str, str],
    titles: dict[str, list[CatalogTitle]],
    release_tracks: dict[tuple[str, int], set[str]],
    existing: dict[int, dict[str, str]],
) -> list[dict[str, str]]:
    output = []
    for song in songs:
        number = int(song["songbook_number"])
        previous = existing.get(number, {})
        previous_work_id = previous.get("work_id", "").strip()
        if previous_work_id:
            canonical_title = works.get(previous_work_id)
            if canonical_title is None:
                raise WorkMatchError(
                    f"existing match for song {number} refers to missing Work {previous_work_id}"
                )
            status = previous.get("status", "reviewed_existing") or "reviewed_existing"
            detail = previous.get("detail", "") or "preserved reviewed Work ID"
            work_id = previous_work_id
        else:
            status, work_id, canonical_title, detail = resolve_song(
                song, titles, release_tracks
            )
        output.append(
            {
                "songbook_number": str(number),
                "folder": str(song.get("folder") or ""),
                "title_zh_hans": str(song.get("title_zh_hans") or ""),
                "title_en": str(song.get("title_en") or ""),
                "status": status,
                "work_id": work_id,
                "canonical_title": canonical_title or "",
                "detail": detail,
            }
        )
    return output


def write_tsv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=OUTPUT_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--existing-matches", type=Path)
    parser.add_argument("--release-key")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest = args.manifest.expanduser().resolve()
    database = args.database.expanduser().resolve()
    if not manifest.is_file():
        parser.error("--manifest must be an existing JSONL file")
    if not database.is_file():
        parser.error("--database must be an existing catalog SQLite file")
    existing_path = (
        args.existing_matches.expanduser().resolve() if args.existing_matches else None
    )
    if existing_path is not None and not existing_path.is_file():
        parser.error("--existing-matches must be an existing TSV file")

    songs = read_jsonl(manifest)
    existing = read_existing_matches(existing_path)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        works, titles, release_tracks = load_catalog(connection, args.release_key)
    finally:
        connection.close()
    rows = build_matches(songs, works, titles, release_tracks, existing)
    write_tsv(args.output.expanduser().resolve(), rows)
    summary = {
        "songs": len(rows),
        "matched": sum(bool(row["work_id"]) for row in rows),
        "resolved_via_existing_rendition": sum(
            row["status"] == "resolved_existing_rendition" for row in rows
        ),
        "ambiguous": sum(row["status"] == "ambiguous_skipped" for row in rows),
        "unmatched": sum(row["status"] == "unmatched_skipped" for row in rows),
        "output": str(args.output.expanduser().resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
