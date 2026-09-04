"""Build verified issue 12 manifests from Mongo metadata and existing COS objects.

This utility is read-only: it calls Mongo ``find`` and COS ``get_object`` only. Runtime
dependencies ``pymongo`` and ``cos-python-sdk-v5`` intentionally remain outside the API package.
Run it in the existing ingestion environment after sourcing that environment's secrets.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

GMUSIC_ID = re.compile(r"^gmusic:([^:]+):rev(\d+)$")


class ManifestBuildError(RuntimeError):
    pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--score-output", type=Path, required=True)
    parser.add_argument("--song-mapping", type=Path, required=True)
    parser.add_argument("--song-output", type=Path, required=True)
    parser.add_argument("--song-bucket", default="bible-1328751369")
    parser.add_argument("--allow-count-drift", action="store_true")
    args = parser.parse_args()

    try:
        from pymongo import MongoClient
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as error:
        raise SystemExit("requires pymongo and cos-python-sdk-v5") from error

    region = required_env("COS_REGION")
    score_bucket = required_env("COS_BUCKET")
    cos = CosS3Client(
        CosConfig(
            Region=region,
            SecretId=required_env("COS_SECRET_ID"),
            SecretKey=required_env("COS_SECRET_KEY"),
            Scheme="https",
        )
    )
    mongo = MongoClient(required_env("MONGO_URI"), serverSelectionTimeoutMS=8000)
    collection = mongo[os.environ.get("MONGO_DB", "musicxml")][
        os.environ.get("MONGO_COLL", "scores")
    ]

    score_rows = []
    for document in collection.find({"_id": {"$regex": r"^gmusic:[^:]+:rev\d+$"}}):
        mongo_id = str(document["_id"])
        match = GMUSIC_ID.fullmatch(mongo_id)
        if match is None:
            continue
        key = required_text(document, "cos_key")
        actual_sha256, actual_size = hash_cos_object(cos, score_bucket, key)
        expected_sha256 = required_text(document, "sha256").lower()
        expected_size = int(document["bytes"])
        if actual_sha256 != expected_sha256 or actual_size != expected_size:
            raise ManifestBuildError(f"score object verification failed: {mongo_id}")
        score_rows.append(
            {
                "mongo_id": mongo_id,
                "work_key": f"gmusic:{match.group(1)}",
                "revision_no": int(match.group(2)),
                "title": required_text(document, "title"),
                "status": required_text(document, "status"),
                "cos_bucket": score_bucket,
                "cos_key": key,
                "sha256": actual_sha256,
                "byte_size": actual_size,
                "media_type": "application/vnd.recordare.musicxml+xml",
                "language": optional_text(document.get("lyrics_lang")),
                "lyrics": lyrics_from_document(document),
                "composers": names_from_document(document.get("composer")),
                "lyricists": names_from_document(document.get("lyricist")),
                "key_signature": optional_text(document.get("key_signature")),
                "time_signature": optional_text(document.get("time_signature")),
                "tempo": optional_text(document.get("tempo")),
                "year": optional_text(document.get("year")),
                "verified": True,
            }
        )
    score_rows.sort(key=lambda row: (row["work_key"], row["revision_no"]))
    write_jsonl(args.score_output, score_rows)

    with args.song_mapping.open(encoding="utf-8", newline="") as source:
        song_rows = list(csv.DictReader(source))
    if not song_rows:
        raise ManifestBuildError("song mapping is empty")
    for row in song_rows:
        key = required_text(row, "mp3_cos_key")
        actual_sha256, actual_size = hash_cos_object(cos, args.song_bucket, key)
        if actual_sha256 != required_text(row, "mp3_sha256").lower():
            raise ManifestBuildError(f"song SHA-256 verification failed: {key}")
        if actual_size != int(row.get("byte_size") or row["bytes"]):
            raise ManifestBuildError(f"song byte-size verification failed: {key}")
        row["cos_bucket"] = args.song_bucket
        row["verified"] = "true"
    if not args.allow_count_drift and (len(score_rows), len(song_rows)) != (101, 73):
        raise ManifestBuildError(
            f"issue 12 source count drift: scores={len(score_rows)}, songs={len(song_rows)}"
        )
    write_csv(args.song_output, song_rows)
    print(
        json.dumps(
            {
                "scores": len(score_rows),
                "score_objects_verified": len(score_rows),
                "songs": len(song_rows),
                "song_objects_verified": len(song_rows),
            },
            ensure_ascii=False,
        )
    )


def hash_cos_object(client: Any, bucket: str, key: str) -> tuple[str, int]:
    response = client.get_object(Bucket=bucket, Key=key)
    stream = response["Body"].get_raw_stream()
    digest = hashlib.sha256()
    size = 0
    try:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    finally:
        stream.close()
    return digest.hexdigest(), size


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as target:
        for row in rows:
            target.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    for extra in ("cos_bucket", "verified"):
        if extra not in fieldnames:
            fieldnames.append(extra)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ManifestBuildError(f"missing environment variable {name}")
    return value


def required_text(value: dict[str, Any], key: str) -> str:
    normalized = str(value.get(key) or "").strip()
    if not normalized:
        raise ManifestBuildError(f"missing field {key}")
    return normalized


def optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        normalized = "\n".join(str(item) for item in value if str(item).strip()).strip()
    else:
        normalized = str(value).strip()
    return normalized or None


def lyrics_from_document(document: dict[str, Any]) -> str | None:
    """GMUSIC uses lyrics_text; lyrics remains a backwards-compatible fallback."""
    return optional_text(document.get("lyrics_text") or document.get("lyrics"))


def names_from_document(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in values if str(item).strip()]


if __name__ == "__main__":
    main()
