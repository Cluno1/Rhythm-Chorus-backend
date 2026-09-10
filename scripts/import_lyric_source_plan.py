"""Upload a reviewed lyric-source plan to COS and register it in catalog v2.

Dry-run is the default. Applying requires both ``--apply`` and the literal
``--confirm APPLY_LYRIC_SOURCES``. The plan must have been generated with
``build_lyric_source_plan.py --render-dir`` so every full page has immutable
SHA-256, size, and dimensions. This importer never crops or duplicates images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.formal_import import deterministic_id
from rhythm_metadata_api.infrastructure.db.database import create_v2_engine
from rhythm_metadata_api.infrastructure.db.models import (
    Asset,
    AssetLocation,
    AssetSource,
    ChangeEvent,
    ChangeEventWork,
    LyricSourceDocument,
    LyricSourceLink,
    LyricSourcePage,
    Work,
    utc_now,
)


class LyricSourceImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObjectSpec:
    path: Path
    cos_key: str
    sha256: str
    byte_size: int
    media_type: str
    source: str
    source_ref: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def load_plan(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("mode") != "rendered-dry-run":
        raise LyricSourceImportError("plan must be generated with --render-dir")
    if not plan.get("pages") or not plan.get("work_links"):
        raise LyricSourceImportError("plan has no pages or work links")
    return plan


def object_specs(plan: dict[str, Any]) -> list[ObjectSpec]:
    document = plan["document"]
    specs = [
        ObjectSpec(
            path=Path(document["pdf_path"]).expanduser().resolve(),
            cos_key=document["cos_key"],
            sha256=document["pdf_sha256"],
            byte_size=int(document["pdf_byte_size"]),
            media_type="application/pdf",
            source="lyric_source_pdf",
            source_ref=document["source_ref"],
        )
    ]
    for page in plan["pages"]:
        specs.append(
            ObjectSpec(
                path=Path(page["image_path"]).expanduser().resolve(),
                cos_key=page["cos_key"],
                sha256=page["image_sha256"],
                byte_size=int(page["byte_size"]),
                media_type="image/png",
                source="lyric_source_pdf_page",
                source_ref=(
                    f"{document['source_ref']}#page={int(page['physical_page_number'])}"
                ),
            )
        )
    for spec in specs:
        if not spec.path.is_file():
            raise LyricSourceImportError(f"source object is missing: {spec.path}")
        if spec.byte_size != spec.path.stat().st_size:
            raise LyricSourceImportError(f"source object size drifted: {spec.path}")
        if file_sha256(spec.path) != spec.sha256:
            raise LyricSourceImportError(f"source object SHA-256 drifted: {spec.path}")
        if not spec.cos_key or spec.cos_key.startswith("/") or ".." in Path(spec.cos_key).parts:
            raise LyricSourceImportError(f"unsafe COS key: {spec.cos_key}")
    if len({spec.sha256 for spec in specs}) != len(specs):
        raise LyricSourceImportError("plan contains duplicate object content")
    return specs


def require_schema(database: Path) -> None:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    if version != ("issue52lyricsources",):
        raise LyricSourceImportError(f"database is not migrated to issue52lyricsources: {version}")
    if integrity != ("ok",):
        raise LyricSourceImportError(f"database integrity check failed: {integrity}")


def validate_catalog(session: Session, plan: dict[str, Any], specs: list[ObjectSpec]) -> None:
    work_ids = {row["work_id"] for row in plan["work_links"]}
    existing_work_ids = set(
        session.scalars(
            select(Work.id).where(Work.id.in_(work_ids), Work.deleted_at.is_(None))
        )
    )
    missing = sorted(work_ids - existing_work_ids)
    if missing:
        raise LyricSourceImportError(
            f"plan refers to {len(missing)} missing Work IDs: {missing[:5]}"
        )
    for spec in specs:
        asset = session.scalar(select(Asset).where(Asset.sha256 == spec.sha256))
        if asset is not None and (
            asset.byte_size != spec.byte_size
            or asset.detected_media_type != spec.media_type
            or asset.state != "ready"
        ):
            raise LyricSourceImportError(f"existing Asset conflicts with {spec.path}")


def cos_client(region: str) -> Any:
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError as error:
        raise LyricSourceImportError("apply mode requires cos-python-sdk-v5") from error
    secret_id = os.environ.get("COS_SECRET_ID")
    secret_key = os.environ.get("COS_SECRET_KEY")
    if not secret_id or not secret_key:
        raise LyricSourceImportError(
            "COS_SECRET_ID and COS_SECRET_KEY are required in apply mode"
        )
    config = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Token=os.environ.get("COS_SESSION_TOKEN"),
    )
    return CosS3Client(config)


def upload_and_verify(client: Any, bucket: str, spec: ObjectSpec) -> None:
    try:
        head = client.head_object(Bucket=bucket, Key=spec.cos_key)
    except Exception as error:
        status = getattr(error, "get_status_code", lambda: None)()
        if str(status) != "404":
            raise
        head = None
    if head is not None:
        size = int(head.get("Content-Length", -1))
        remote_hash = head.get("x-cos-meta-sha256") or head.get("X-Cos-Meta-Sha256")
        if size != spec.byte_size or remote_hash != spec.sha256:
            raise LyricSourceImportError(
                f"existing COS object conflicts: {bucket}/{spec.cos_key}"
            )
        return
    with spec.path.open("rb") as source:
        client.put_object(
            Bucket=bucket,
            Key=spec.cos_key,
            Body=source,
            ContentType=spec.media_type,
            Metadata={"x-cos-meta-sha256": spec.sha256},
        )
    head = client.head_object(Bucket=bucket, Key=spec.cos_key)
    if int(head.get("Content-Length", -1)) != spec.byte_size:
        raise LyricSourceImportError(f"COS size verification failed: {bucket}/{spec.cos_key}")
    remote_hash = head.get("x-cos-meta-sha256") or head.get("X-Cos-Meta-Sha256")
    if remote_hash != spec.sha256:
        raise LyricSourceImportError(
            f"COS SHA-256 metadata verification failed: {bucket}/{spec.cos_key}"
        )


def ensure_asset(
    session: Session,
    bucket: str,
    spec: ObjectSpec,
) -> Asset:
    asset = session.scalar(select(Asset).where(Asset.sha256 == spec.sha256))
    if asset is None:
        asset = Asset(
            id=deterministic_id("asset", spec.sha256),
            sha256=spec.sha256,
            byte_size=spec.byte_size,
            detected_media_type=spec.media_type,
            state="ready",
        )
        session.add(asset)
        session.flush()
    storage_key = f"{bucket}/{spec.cos_key}"
    location = session.scalar(
        select(AssetLocation).where(
            AssetLocation.provider == "cos",
            AssetLocation.storage_key == storage_key,
        )
    )
    if location is not None and location.asset_id != asset.id:
        raise LyricSourceImportError(f"COS location belongs to another Asset: {storage_key}")
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
            AssetSource.source == spec.source,
            AssetSource.source_ref == spec.source_ref,
        )
    )
    if source is None:
        session.add(
            AssetSource(
                id=deterministic_id(
                    "asset-source", f"{asset.id}:{spec.source}:{spec.source_ref}"
                ),
                asset_id=asset.id,
                original_filename=spec.path.name,
                source=spec.source,
                source_ref=spec.source_ref,
            )
        )
    return asset


def register_plan(
    session: Session,
    plan: dict[str, Any],
    specs: list[ObjectSpec],
    bucket: str,
    actor_id: str,
) -> dict[str, int]:
    assets = {
        spec.cos_key: ensure_asset(session, bucket, spec)
        for spec in specs
    }
    document_data = plan["document"]
    document_id = deterministic_id("lyric-source-document", document_data["pdf_sha256"])
    document = session.get(LyricSourceDocument, document_id)
    if document is None:
        document = LyricSourceDocument(
            id=document_id,
            title=document_data["title"],
            source_kind=document_data["source_kind"],
            document_asset_id=assets[document_data["cos_key"]].id,
            source_ref=document_data["source_ref"],
        )
        session.add(document)
        session.flush()
    elif document.document_asset_id != assets[document_data["cos_key"]].id:
        raise LyricSourceImportError(
            "existing lyric source document has a different PDF Asset"
        )

    pages_by_number: dict[int, LyricSourcePage] = {}
    for page_data in plan["pages"]:
        page_number = int(page_data["physical_page_number"])
        page_id = deterministic_id("lyric-source-page", f"{document.id}:{page_number}")
        page_asset = assets[page_data["cos_key"]]
        page = session.get(LyricSourcePage, page_id)
        if page is None:
            page = LyricSourcePage(
                id=page_id,
                document_id=document.id,
                physical_page_number=page_number,
                image_asset_id=page_asset.id,
                width_px=int(page_data["width_px"]),
                height_px=int(page_data["height_px"]),
                render_dpi=int(plan.get("render_dpi") or 144),
                display_label=page_data.get("display_label"),
            )
            session.add(page)
        elif page.image_asset_id != page_asset.id:
            raise LyricSourceImportError(
                f"existing lyric source page {page_number} has another Asset"
            )
        pages_by_number[page_number] = page
    session.flush()

    new_links = 0
    changed_works = 0
    for work_data in plan["work_links"]:
        work = session.get(Work, work_data["work_id"])
        if work is None or work.deleted_at is not None:
            raise LyricSourceImportError(
                f"Work disappeared during import: {work_data['work_id']}"
            )
        work_changed = False
        linked_page_ids = []
        for link_data in work_data["pages"]:
            page = pages_by_number[int(link_data["physical_page_number"])]
            link_id = deterministic_id("lyric-source-link", f"work:{work.id}:{page.id}")
            link = session.get(LyricSourceLink, link_id)
            if link is None:
                link = LyricSourceLink(
                    id=link_id,
                    work_id=work.id,
                    source_page_id=page.id,
                    display_order=int(link_data["display_order"]),
                    language_relations=link_data.get("language_relations") or [],
                    note=(
                        "IHOP Songbook entries: "
                        + ", ".join(map(str, work_data["songbook_numbers"]))
                    ),
                )
                session.add(link)
                new_links += 1
                work_changed = True
            elif link.work_id != work.id or link.source_page_id != page.id:
                raise LyricSourceImportError(
                    f"deterministic link identity conflict: {link_id}"
                )
            linked_page_ids.append(page.id)
        if work_changed:
            work.revision += 1
            work.updated_at = utc_now()
            event = ChangeEvent(
                entity_type="work",
                entity_id=work.id,
                entity_revision=work.revision,
                operation="work.lyric_source_pages_imported",
                actor_id=actor_id,
                payload_json={"source_page_ids": linked_page_ids},
            )
            session.add(event)
            session.flush()
            session.add(ChangeEventWork(event_sequence=event.sequence, work_id=work.id))
            changed_works += 1
    session.flush()
    return {
        "assets": len(assets),
        "pages": len(pages_by_number),
        "new_links": new_links,
        "changed_works": changed_works,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default=os.environ.get("COS_REGION", "ap-guangzhou"))
    parser.add_argument("--actor-id", default="lyric-source-importer")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()

    plan = load_plan(args.plan.expanduser().resolve())
    specs = object_specs(plan)
    database = args.database.expanduser().resolve()
    if not database.is_file():
        parser.error("--database must be an existing catalog SQLite file")
    require_schema(database)
    engine = create_v2_engine(str(database))
    with Session(engine) as session:
        validate_catalog(session, plan, specs)
    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "database": str(database),
        "bucket": args.bucket,
        "objects": len(specs),
        "physical_pages": len(plan["pages"]),
        "matched_works": len(plan["work_links"]),
        "work_page_links": sum(len(row["pages"]) for row in plan["work_links"]),
    }
    if not args.apply:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    if args.confirm != "APPLY_LYRIC_SOURCES":
        parser.error("apply mode requires --confirm APPLY_LYRIC_SOURCES")
    client = cos_client(args.region)
    for spec in specs:
        upload_and_verify(client, args.bucket, spec)
    with Session(engine) as session, session.begin():
        result = register_plan(
            session,
            plan,
            specs,
            args.bucket.strip(),
            args.actor_id.strip(),
        )
    print(json.dumps(summary | result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
