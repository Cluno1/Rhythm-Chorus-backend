"""Build and optionally render a full-page lyric-source import plan.

The command is deliberately production-safe: it never opens the catalog database,
calls the API, or uploads to COS. Its JSON output is the reviewed input for the
separate production import step. PDF pages are rendered in full with Poppler;
there is no crop, split, annotation, or per-song image duplication.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import struct
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any


class PlanError(RuntimeError):
    pass


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise PlanError(f"invalid manifest JSON on line {line_number}") from error
    return rows


def read_work_matches(path: Path) -> dict[int, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    result = {}
    for row in rows:
        number = int(row["songbook_number"])
        if number in result:
            raise PlanError(f"duplicate work match for songbook number {number}")
        result[number] = row
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def language_relations(song: dict[str, Any]) -> list[dict[str, str]]:
    counts = song.get("line_counts") or {}
    result = []
    for source_key, language in (
        ("chinese", "zh-Hans"),
        ("pinyin", "zh-Latn"),
        ("english", "en"),
    ):
        if int(counts.get(source_key) or 0) > 0:
            result.append({"language": language, "relation": "printed"})
    if int(counts.get("traditional") or 0) > 0:
        result.append(
            {
                "language": "zh-Hant",
                "relation": "converted",
                "derived_from_language": "zh-Hans",
            }
        )
    return result


def render_page(pdf: Path, page_number: int, dpi: int, destination: Path) -> None:
    command = shutil.which("pdftoppm")
    if command is None:
        raise PlanError("pdftoppm is required for --render")
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = destination.with_suffix("")
    subprocess.run(
        [
            command,
            "-f",
            str(page_number),
            "-l",
            str(page_number),
            "-singlefile",
            "-r",
            str(dpi),
            "-png",
            str(pdf),
            str(prefix),
        ],
        check=True,
    )
    if not destination.is_file():
        raise PlanError(f"renderer did not create {destination}")


def png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as source:
        header = source.read(24)
    if len(header) != 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PlanError(f"rendered page is not PNG: {path}")
    return struct.unpack(">II", header[16:24])


def build_plan(
    pdf: Path,
    manifest_path: Path,
    work_matches_path: Path,
    *,
    dpi: int,
    output_dir: Path | None,
) -> dict[str, Any]:
    songs = read_jsonl(manifest_path)
    matches = read_work_matches(work_matches_path)
    pdf_digest = sha256(pdf)
    page_to_songs: dict[int, list[dict[str, Any]]] = defaultdict(list)
    work_to_songs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmatched = []

    for song in songs:
        number = int(song["songbook_number"])
        pages = song.get("source_pdf_pages")
        if not isinstance(pages, list) or not pages or any(int(page) < 1 for page in pages):
            raise PlanError(f"song {number} has invalid source_pdf_pages")
        for page in pages:
            page_to_songs[int(page)].append(song)
        match = matches.get(number)
        work_id = (match or {}).get("work_id", "").strip()
        if work_id:
            work_to_songs[work_id].append(song)
        else:
            unmatched.append(
                {
                    "songbook_number": number,
                    "title": song.get("title_zh_hans") or song.get("title_en") or song["folder"],
                    "status": (match or {}).get("status", "missing_match_row"),
                }
            )

    rendered: dict[int, dict[str, Any]] = {}
    if output_dir is not None:
        pages_dir = output_dir.expanduser().resolve() / "pages"
        for page_number in sorted(page_to_songs):
            path = pages_dir / f"page-{page_number:04d}.png"
            if not path.exists():
                render_page(pdf, page_number, dpi, path)
            width, height = png_dimensions(path)
            rendered[page_number] = {
                "image_path": str(path),
                "image_sha256": sha256(path),
                "byte_size": path.stat().st_size,
                "width_px": width,
                "height_px": height,
            }

    page_rows = []
    for page_number, page_songs in sorted(page_to_songs.items()):
        page_rows.append(
            {
                "physical_page_number": page_number,
                "display_label": f"PDF page {page_number}",
                "cos_key": f"lyric-sources/{pdf_digest}/pages/page-{page_number:04d}.png",
                "songbook_numbers": sorted(int(song["songbook_number"]) for song in page_songs),
                **rendered.get(page_number, {}),
            }
        )

    work_links = []
    for work_id, work_songs in sorted(work_to_songs.items()):
        pages = sorted(
            {
                int(page)
                for song in work_songs
                for page in song["source_pdf_pages"]
            }
        )
        relations_by_key: dict[tuple[str, str, str], dict[str, str]] = {}
        for song in work_songs:
            for relation in language_relations(song):
                key = (
                    relation["language"],
                    relation["relation"],
                    relation.get("derived_from_language", ""),
                )
                relations_by_key[key] = relation
        work_links.append(
            {
                "work_id": work_id,
                "songbook_numbers": sorted(
                    int(song["songbook_number"]) for song in work_songs
                ),
                "pages": [
                    {
                        "physical_page_number": page,
                        "display_order": index,
                        "language_relations": list(relations_by_key.values()),
                    }
                    for index, page in enumerate(pages, 1)
                ],
            }
        )

    shared_pages = sum(len(page_songs) > 1 for page_songs in page_to_songs.values())
    song_page_links = sum(len(song["source_pdf_pages"]) for song in songs)
    return {
        "mode": "rendered-dry-run" if output_dir is not None else "dry-run",
        "render_dpi": dpi,
        "document": {
            "title": "IHOP Songbook 2024",
            "source_kind": "pdf",
            "source_ref": pdf.name,
            "pdf_path": str(pdf),
            "pdf_sha256": pdf_digest,
            "pdf_byte_size": pdf.stat().st_size,
            "cos_key": f"lyric-sources/{pdf_digest}/{pdf.name}",
        },
        "counts": {
            "songs": len(songs),
            "song_page_links": song_page_links,
            "distinct_physical_pages": len(page_to_songs),
            "shared_physical_pages": shared_pages,
            "matched_works": len(work_to_songs),
            "unmatched_songs": len(unmatched),
        },
        "pages": page_rows,
        "work_links": work_links,
        "unmatched": unmatched,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--work-matches", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=144)
    parser.add_argument(
        "--render-dir",
        type=Path,
        help="render every distinct physical page as a full PNG; omitted for metadata-only dry-run",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.pdf.is_file():
        parser.error("--pdf must be an existing file")
    if not 72 <= args.dpi <= 600:
        parser.error("--dpi must be between 72 and 600")
    plan = build_plan(
        args.pdf.expanduser().resolve(),
        args.manifest.expanduser().resolve(),
        args.work_matches.expanduser().resolve(),
        dpi=args.dpi,
        output_dir=args.render_dir,
    )
    encoded = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
