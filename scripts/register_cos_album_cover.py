"""Register an existing COS image as a catalog album cover.

The image bytes must already exist in COS and be verified by the operator. This
script stores only content metadata and the stable bucket/key in SQLite. It is
dry-run by default and refuses to replace a different existing cover.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.formal_import import deterministic_id
from rhythm_metadata_api.infrastructure.db.database import create_v2_engine
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    Asset,
    AssetLocation,
    AssetSource,
    ChangeEvent,
    ChangeEventWork,
    Release,
    ReleaseItem,
    Rendition,
    utc_now,
)


class CoverRegistrationError(RuntimeError):
    pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--release-key", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--cos-key", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--byte-size", required=True, type=int)
    parser.add_argument("--media-type", default="image/png")
    parser.add_argument("--original-filename", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    digest = args.sha256.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise CoverRegistrationError("sha256 must be 64 lowercase hexadecimal characters")
    if args.byte_size <= 0:
        raise CoverRegistrationError("byte-size must be positive")
    if not args.media_type.startswith("image/"):
        raise CoverRegistrationError("media-type must be an image")
    cos_key = args.cos_key.strip().lstrip("/")
    if not cos_key:
        raise CoverRegistrationError("cos-key is empty")

    engine = create_v2_engine(str(args.database.expanduser().resolve()))
    with Session(engine) as session:
        release = session.scalar(
            select(Release).where(Release.key == args.release_key, Release.deleted_at.is_(None))
        )
        if release is None:
            raise CoverRegistrationError(f"release not found: {args.release_key}")

        asset = session.scalar(select(Asset).where(Asset.sha256 == digest))
        if asset is not None and (
            asset.byte_size != args.byte_size
            or asset.detected_media_type != args.media_type
            or asset.state != "ready"
        ):
            raise CoverRegistrationError("existing asset metadata conflicts with the cover")
        asset_id = asset.id if asset is not None else deterministic_id("asset", digest)
        if release.cover_asset_id not in {None, asset_id}:
            raise CoverRegistrationError(
                f"release already has a different cover: {release.cover_asset_id}"
            )

        storage_key = f"{args.bucket.strip()}/{cos_key}"
        location = session.scalar(
            select(AssetLocation).where(
                AssetLocation.provider == "cos", AssetLocation.storage_key == storage_key
            )
        )
        if location is not None and location.asset_id != asset_id:
            raise CoverRegistrationError("COS location is already linked to different content")

        plan = {
            "mode": "apply" if args.apply else "dry-run",
            "release_id": release.id,
            "release_key": release.key,
            "asset_id": asset_id,
            "sha256": digest,
            "byte_size": args.byte_size,
            "media_type": args.media_type,
            "storage_key": storage_key,
            "already_linked": release.cover_asset_id == asset_id,
        }
        if not args.apply:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return

        if asset is None:
            asset = Asset(
                id=asset_id,
                sha256=digest,
                byte_size=args.byte_size,
                detected_media_type=args.media_type,
                state="ready",
            )
            session.add(asset)
            session.flush()
        if location is None:
            session.add(
                AssetLocation(
                    id=deterministic_id("asset-location", storage_key),
                    asset_id=asset.id,
                    provider="cos",
                    storage_key=storage_key,
                    state="available",
                )
            )
        source = session.scalar(
            select(AssetSource).where(
                AssetSource.asset_id == asset.id,
                AssetSource.source == "album_cover_pdf",
                AssetSource.source_ref == args.source_ref,
            )
        )
        if source is None:
            session.add(
                AssetSource(
                    id=deterministic_id("asset-source", f"{asset.id}:{args.source_ref}"),
                    asset_id=asset.id,
                    original_filename=args.original_filename,
                    source="album_cover_pdf",
                    source_ref=args.source_ref,
                )
            )

        if release.cover_asset_id != asset.id:
            release.cover_asset_id = asset.id
            release.revision += 1
            release.updated_at = utc_now()
            session.flush()
            work_ids = sorted(
                set(
                    session.scalars(
                        select(Arrangement.work_id)
                        .join(Rendition, Rendition.arrangement_id == Arrangement.id)
                        .join(ReleaseItem, ReleaseItem.rendition_id == Rendition.id)
                        .where(ReleaseItem.release_id == release.id)
                    )
                )
            )
            event = ChangeEvent(
                entity_type="release",
                entity_id=release.id,
                entity_revision=release.revision,
                operation="release.cover.updated",
                actor_id="album-cover-importer",
                payload_json={"asset_id": asset.id, "sha256": digest, "cos_key": storage_key},
            )
            session.add(event)
            session.flush()
            session.add_all(
                [
                    ChangeEventWork(event_sequence=event.sequence, work_id=work_id)
                    for work_id in work_ids
                ]
            )
            plan["change_sequence"] = event.sequence
            plan["affected_works"] = len(work_ids)

        session.commit()
        print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
