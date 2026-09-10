"""Resolve extracted lyric-source songs to existing catalog Works.

The resolver deliberately follows catalog relationships instead of requiring an
extracted title to equal ``Work.canonical_title``. A reviewed PDF-to-Catalog bridge
uses the MP3's content SHA-256 to follow ``Asset -> Rendition -> Arrangement ->
Work``. This authoritative relationship takes precedence over historical title
matches. For sources without a reviewed bridge, an exact normalized match to a
Rendition label may still be used as a fallback.

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


@dataclass(frozen=True)
class ReviewedCatalogLink:
    songbook_number: int
    mp3_sha256: str
    decision: str
    detail: str


REVIEW_DECISIONS = {"link_existing_work", "different_composition"}


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


def read_reviewed_catalog_links(path: Path | None) -> list[ReviewedCatalogLink]:
    if path is None:
        return []
    with path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    result = []
    identities = set()
    for line_number, row in enumerate(rows, 2):
        number = int(row.get("songbook_number") or 0)
        sha256 = (row.get("mp3_sha256") or "").strip().lower()
        decision = (row.get("decision") or "").strip()
        if number < 1:
            raise WorkMatchError(f"invalid reviewed songbook number on line {line_number}")
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise WorkMatchError(f"invalid reviewed MP3 SHA-256 on line {line_number}")
        if decision not in REVIEW_DECISIONS:
            raise WorkMatchError(f"invalid reviewed decision on line {line_number}: {decision}")
        identity = (number, sha256)
        if identity in identities:
            raise WorkMatchError(
                f"duplicate reviewed Catalog link for song {number} and MP3 {sha256}"
            )
        identities.add(identity)
        result.append(
            ReviewedCatalogLink(
                songbook_number=number,
                mp3_sha256=sha256,
                decision=decision,
                detail=(row.get("detail") or "").strip(),
            )
        )
    return result


def resolve_reviewed_catalog_links(
    connection: sqlite3.Connection,
    reviewed: list[ReviewedCatalogLink],
) -> dict[int, tuple[str, str, str]]:
    """Resolve approved MP3 identities to Works without comparing any title."""
    grouped: dict[int, list[ReviewedCatalogLink]] = defaultdict(list)
    for row in reviewed:
        grouped[row.songbook_number].append(row)

    overrides = {}
    for number, rows in grouped.items():
        decisions = {row.decision for row in rows}
        if len(decisions) != 1:
            raise WorkMatchError(
                f"reviewed Catalog links for song {number} contain conflicting decisions"
            )
        if decisions == {"different_composition"}:
            continue

        resolved_work_ids = set()
        canonical_titles = {}
        for row in rows:
            matches = list(
                connection.execute(
                    """
                    SELECT DISTINCT w.id, w.canonical_title
                    FROM v2_assets asset
                    JOIN v2_rendition_assets rendition_asset
                      ON rendition_asset.asset_id = asset.id
                    JOIN v2_renditions rendition
                      ON rendition.id = rendition_asset.rendition_id
                    JOIN v2_arrangements arrangement
                      ON arrangement.id = rendition.arrangement_id
                    JOIN v2_works w ON w.id = arrangement.work_id
                    WHERE lower(asset.sha256) = ?
                      AND asset.state = 'ready'
                      AND rendition.deleted_at IS NULL
                      AND arrangement.deleted_at IS NULL
                      AND w.deleted_at IS NULL
                    """,
                    (row.mp3_sha256,),
                )
            )
            work_ids = {str(match[0]) for match in matches}
            if len(work_ids) != 1:
                raise WorkMatchError(
                    f"reviewed MP3 {row.mp3_sha256} for song {number} resolves to "
                    f"{len(work_ids)} Works"
                )
            work_id = work_ids.pop()
            resolved_work_ids.add(work_id)
            canonical_titles[work_id] = str(matches[0][1])
        if len(resolved_work_ids) != 1:
            raise WorkMatchError(
                f"reviewed Catalog versions for song {number} resolve to multiple Works: "
                + ", ".join(sorted(resolved_work_ids))
            )
        work_id = resolved_work_ids.pop()
        overrides[number] = (
            work_id,
            canonical_titles[work_id],
            f"reviewed MP3 SHA-256 -> Rendition -> Work ({len(rows)} version(s))",
        )
    return overrides


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
    reviewed_catalog_overrides: dict[int, tuple[str, str, str]] | None = None,
) -> list[dict[str, str]]:
    reviewed_catalog_overrides = reviewed_catalog_overrides or {}
    output = []
    for song in songs:
        number = int(song["songbook_number"])
        previous = existing.get(number, {})
        previous_work_id = previous.get("work_id", "").strip()
        reviewed_override = reviewed_catalog_overrides.get(number)
        if reviewed_override is not None:
            work_id, canonical_title, detail = reviewed_override
            if work_id not in works:
                raise WorkMatchError(
                    f"reviewed Catalog relation for song {number} refers to missing Work {work_id}"
                )
            status = "resolved_reviewed_catalog_relation"
        elif previous_work_id:
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
    parser.add_argument("--reviewed-catalog-links", type=Path)
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
    reviewed_path = (
        args.reviewed_catalog_links.expanduser().resolve()
        if args.reviewed_catalog_links
        else None
    )
    if reviewed_path is not None and not reviewed_path.is_file():
        parser.error("--reviewed-catalog-links must be an existing TSV file")

    songs = read_jsonl(manifest)
    existing = read_existing_matches(existing_path)
    reviewed = read_reviewed_catalog_links(reviewed_path)
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        works, titles, release_tracks = load_catalog(connection, args.release_key)
        reviewed_catalog_overrides = resolve_reviewed_catalog_links(connection, reviewed)
    finally:
        connection.close()
    song_numbers = {int(song["songbook_number"]) for song in songs}
    unknown_reviewed_numbers = sorted(
        {row.songbook_number for row in reviewed} - song_numbers
    )
    if unknown_reviewed_numbers:
        raise WorkMatchError(
            f"reviewed Catalog links contain unknown songbook numbers: {unknown_reviewed_numbers}"
        )
    rows = build_matches(
        songs,
        works,
        titles,
        release_tracks,
        existing,
        reviewed_catalog_overrides,
    )
    write_tsv(args.output.expanduser().resolve(), rows)
    summary = {
        "songs": len(rows),
        "matched": sum(bool(row["work_id"]) for row in rows),
        "resolved_via_existing_rendition": sum(
            row["status"] == "resolved_existing_rendition" for row in rows
        ),
        "resolved_via_reviewed_catalog_relation": sum(
            row["status"] == "resolved_reviewed_catalog_relation" for row in rows
        ),
        "ambiguous": sum(row["status"] == "ambiguous_skipped" for row in rows),
        "unmatched": sum(row["status"] == "unmatched_skipped" for row in rows),
        "output": str(args.output.expanduser().resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
