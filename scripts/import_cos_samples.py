"""Import a small, representative GMUSIC sample set from COS into catalog v2.

The script deliberately imports only five works. It is safe to run again: existing
works are resolved by their ``gmusic`` alias, existing score branches/revisions are
inspected before appending, and assets are content-addressed by the API.

Runtime dependencies ``pymongo`` and ``cos-python-sdk-v5`` are intentionally not
backend dependencies. Run this with the existing ingestion environment that owns
the COS and Mongo configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

SAMPLE_IDS = ("321", "348", "528", "test1", "110")
OCR_SCORE_LABEL = "GMUSIC OCR"
MIDI_SCORE_LABEL = "天国之歌 MIDI 直转谱"
ARRANGEMENT_NAME = "原始编配（COS 样本）"
MIDI_RENDITION_LABEL = "天国之歌源 MIDI"
MUSICXML_MEDIA_TYPE = "application/vnd.recordare.musicxml+xml"
STANDARD_MUSICXML_DOCTYPE = re.compile(
    rb'<!DOCTYPE\s+score-partwise\s+PUBLIC\s+"-//Recordare//DTD MusicXML [^"]+ Partwise//EN"'
    rb'\s+"https?://(?:www\.)?musicxml\.org/dtds/partwise\.dtd"\s*>\s*',
    re.IGNORECASE,
)


class ImportFailure(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.common_headers = {
            "Authorization": f"Bearer {token}",
            "X-Device-ID": "cos-sample-importer",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[Any, dict[str, str], int]:
        request_headers = dict(self.common_headers)
        request_headers.update(headers or {})
        data = content
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read()
                status = response.status
                response_headers = dict(response.headers.items())
        except HTTPError as error:
            raw = error.read()
            detail = raw.decode("utf-8", errors="replace")
            raise ImportFailure(f"{method} {path} returned {error.code}: {detail}") from error
        if status not in expected:
            raise ImportFailure(f"{method} {path} returned unexpected status {status}")
        body = json.loads(raw) if raw else None
        return body, response_headers, status

    def get(self, path: str) -> dict[str, Any]:
        body, _, _ = self.request("GET", path)
        return body

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        key: str | None = None,
        *,
        expected: tuple[int, ...] = (200, 201),
        if_match: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        headers: dict[str, str] = {}
        if key is not None:
            headers["Idempotency-Key"] = key
        if if_match is not None:
            headers["If-Match"] = if_match
        body, response_headers, _ = self.request(
            "POST", path, payload=payload, headers=headers, expected=expected
        )
        return body, response_headers

    def patch(
        self, path: str, payload: dict[str, Any], if_match: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        body, response_headers, _ = self.request(
            "PATCH",
            path,
            payload=payload,
            headers={"If-Match": if_match},
            expected=(200,),
        )
        return body, response_headers


@dataclass(frozen=True)
class CosObject:
    key: str
    content: bytes
    sha256: str
    media_type: str
    original_sha256: str
    original_size: int
    transform: str | None = None


class SampleImporter:
    def __init__(
        self,
        *,
        api: ApiClient,
        collection: Any,
        cos_client: Any,
        bucket: str,
        dry_run: bool,
    ) -> None:
        self.api = api
        self.collection = collection
        self.cos = cos_client
        self.bucket = bucket
        self.dry_run = dry_run
        self.objects: dict[str, CosObject] = {}
        self.assets_by_key: dict[str, dict[str, Any]] = {}

    def run(self) -> list[dict[str, Any]]:
        result = []
        for sample_id in SAMPLE_IDS:
            result.append(self.import_work(sample_id))
        return result

    def import_work(self, sample_id: str) -> dict[str, Any]:
        documents = list(
            self.collection.find({"_id": {"$regex": rf"^gmusic:{re.escape(sample_id)}:rev\d+$"}})
        )
        documents.sort(key=lambda item: self.revision_number(item["_id"]))
        if not documents:
            raise ImportFailure(f"no Mongo revisions found for gmusic:{sample_id}")
        canonical = [item for item in documents if item.get("status") == "canonical"]
        if len(canonical) != 1:
            raise ImportFailure(f"gmusic:{sample_id} must have exactly one canonical revision")
        current = canonical[0]
        xml_objects = [
            self.cos_object(
                item["cos_key"],
                MUSICXML_MEDIA_TYPE,
                expected_sha=item.get("sha256"),
            )
            for item in documents
        ]
        parts = self.musicxml_parts(xml_objects[-1].content)
        scan_objects = [self.optional_scan(item) for item in documents]

        print(
            f"[{sample_id}] {current['title']}: {len(documents)} OCR revision(s), "
            f"{len(parts)} part(s), MIDI={bool(current.get('source_midi_cos_key'))}"
        )
        if self.dry_run:
            return {
                "sample_id": sample_id,
                "title": current["title"],
                "ocr_revisions": len(documents),
                "parts": len(parts),
                "has_midi": bool(current.get("source_midi_cos_key")),
            }

        work = self.resolve_or_create_work(sample_id, current)
        bundle = self.api.get(f"/v2/works/{work['id']}/bundle")
        arrangement = self.find_named(bundle["arrangements"], ARRANGEMENT_NAME)
        if arrangement is None:
            arrangement, _ = self.api.post(
                f"/v2/works/{work['id']}/arrangements",
                {
                    "name": ARRANGEMENT_NAME,
                    "voicing": f"{len(parts)} parts",
                    "key_signature": current.get("key_signature"),
                    "parts": parts,
                },
                self.key("arrangement", sample_id),
            )

        ocr_score = self.ensure_score(arrangement["id"], OCR_SCORE_LABEL, "ocr", sample_id)
        ocr_assets = []
        for document, xml_object, scan_object in zip(documents, xml_objects, scan_objects):
            assets = [
                {
                    "asset_id": self.ensure_asset(xml_object)["id"],
                    "role": "primary_musicxml",
                }
            ]
            if scan_object is not None:
                assets.append({"asset_id": self.ensure_asset(scan_object)["id"], "role": "scan"})
            ocr_assets.append((document, assets, xml_object.sha256))
        self.ensure_revisions(ocr_score["id"], ocr_assets, sample_id, "ocr")

        midi_asset = None
        midi_xml_asset = None
        if current.get("source_midi_cos_key"):
            midi_object = self.cos_object(
                current["source_midi_cos_key"],
                "audio/midi",
                expected_sha=current.get("source_midi_sha256"),
            )
            midi_asset = self.ensure_asset(midi_object)
        if current.get("midi_direct_musicxml_cos_key"):
            midi_xml_object = self.cos_object(
                current["midi_direct_musicxml_cos_key"],
                MUSICXML_MEDIA_TYPE,
                expected_sha=current.get("midi_direct_musicxml_sha256"),
            )
            midi_xml_asset = self.ensure_asset(midi_xml_object)

        if midi_asset and midi_xml_asset:
            midi_score = self.ensure_score(
                arrangement["id"], MIDI_SCORE_LABEL, "midi_transcription", sample_id
            )
            self.ensure_revisions(
                midi_score["id"],
                [
                    (
                        {"status": "canonical", "_id": f"gmusic:{sample_id}:midi-direct"},
                        [
                            {"asset_id": midi_xml_asset["id"], "role": "primary_musicxml"},
                            {"asset_id": midi_asset["id"], "role": "source_midi"},
                        ],
                        midi_xml_asset["sha256"],
                    )
                ],
                sample_id,
                "midi-score",
            )
            self.ensure_rendition(arrangement["id"], midi_asset, sample_id)

        arrangement_now = self.api.get(f"/v2/arrangements/{arrangement['id']}")
        if arrangement_now.get("preferred_score_id") != ocr_score["id"]:
            self.api.patch(
                f"/v2/arrangements/{arrangement['id']}",
                {"preferred_score_id": ocr_score["id"]},
                f'"rev-{arrangement_now["revision"]}"',
            )

        verified = self.verify_work(work["id"], sample_id, len(documents), bool(midi_asset))
        return verified

    def resolve_or_create_work(self, sample_id: str, current: dict[str, Any]) -> dict[str, Any]:
        resolved, _ = self.api.post(
            "/v2/works/resolve",
            {"aliases": [{"namespace": "gmusic", "external_id": sample_id}]},
            None,
        )
        if resolved["result"] == "exact":
            return resolved["work"]
        if resolved["result"] != "none":
            raise ImportFailure(f"gmusic:{sample_id} resolved ambiguously: {resolved}")

        credits = []
        positions: dict[str, int] = {}
        for role, field in (("composer", "composer"), ("lyricist", "lyricist")):
            name = current.get(field)
            if not name:
                continue
            contributor, _ = self.api.post(
                "/v2/contributors",
                {"display_name": name},
                self.key("contributor", name),
            )
            positions[role] = positions.get(role, 0) + 1
            credits.append(
                {
                    "contributor_id": contributor["id"],
                    "role": role,
                    "position": positions[role],
                }
            )
        work, _ = self.api.post(
            "/v2/works",
            {
                "canonical_title": current["title"],
                "language": current.get("lyrics_lang"),
                "status": "active",
                "aliases": [{"namespace": "gmusic", "external_id": sample_id}],
                "credits": credits,
            },
            self.key("work", sample_id),
        )
        return work

    def ensure_score(
        self, arrangement_id: str, label: str, origin: str, sample_id: str
    ) -> dict[str, Any]:
        bundle = self.api.get(f"/v2/works/{self.work_id_for_arrangement(arrangement_id)}/bundle")
        arrangement = next(item for item in bundle["arrangements"] if item["id"] == arrangement_id)
        existing = self.find_named(arrangement["scores"], label, field="label")
        if existing is not None:
            return existing
        score, _ = self.api.post(
            f"/v2/arrangements/{arrangement_id}/scores",
            {"label": label, "origin": origin},
            self.key("score", sample_id, label),
        )
        return score

    def ensure_revisions(
        self,
        score_id: str,
        desired: list[tuple[dict[str, Any], list[dict[str, str]], str]],
        sample_id: str,
        branch: str,
    ) -> None:
        score = self.api.get(f"/v2/scores/{score_id}")
        chain = self.score_revision_chain(score.get("head_revision_id"))
        actual_hashes = [self.primary_hash(revision) for revision in chain]
        desired_hashes = [item[2] for item in desired]
        if actual_hashes != desired_hashes[: len(actual_hashes)]:
            raise ImportFailure(
                f"score {score_id} has a revision chain that is not a prefix of the COS source"
            )
        head_id = score.get("head_revision_id")
        for index in range(len(chain), len(desired)):
            document, assets, _ = desired[index]
            score = self.api.get(f"/v2/scores/{score_id}")
            payload = {
                "based_on_revision_id": head_id,
                "edit_message": (
                    f"COS import {document['_id']} ({document.get('status', 'unknown')})"
                ),
                "assets": assets,
            }
            revision, _ = self.api.post(
                f"/v2/scores/{score_id}/revisions",
                payload,
                self.key("revision", sample_id, branch, str(index + 1)),
                if_match=f'"rev-{score["revision"]}"',
                expected=(201,),
            )
            head_id = revision["id"]

    def ensure_rendition(
        self, arrangement_id: str, midi_asset: dict[str, Any], sample_id: str
    ) -> None:
        work_id = self.work_id_for_arrangement(arrangement_id)
        bundle = self.api.get(f"/v2/works/{work_id}/bundle")
        arrangement = next(item for item in bundle["arrangements"] if item["id"] == arrangement_id)
        existing = self.find_named(arrangement["renditions"], MIDI_RENDITION_LABEL, field="label")
        if existing is not None:
            hashes = {item["sha256"] for item in existing["assets"]}
            if midi_asset["sha256"] not in hashes:
                raise ImportFailure(f"existing MIDI rendition for {sample_id} has another asset")
            return
        self.api.post(
            f"/v2/arrangements/{arrangement_id}/renditions",
            {
                "label": MIDI_RENDITION_LABEL,
                "kind": "reference_midi",
                "ensemble": "天国之歌",
                "assets": [
                    {
                        "asset_id": midi_asset["id"],
                        "role": "midi",
                        "codec_profile": "Standard MIDI File",
                    }
                ],
            },
            self.key("rendition", sample_id, "source-midi"),
        )

    def ensure_asset(self, item: CosObject) -> dict[str, Any]:
        if item.key in self.assets_by_key:
            return self.assets_by_key[item.key]
        payload = {
            "sha256": item.sha256,
            "byte_size": len(item.content),
            "media_type": item.media_type,
            "original_filename": PurePosixPath(item.key).name,
            "source": "cos_musicxml_normalized" if item.transform else "cos_import",
            "source_ref": self.source_ref(item),
        }
        created, _ = self.api.post(
            "/v2/uploads", payload, self.key("upload", item.key, item.sha256), expected=(200, 201)
        )
        if created["status"] == "reused":
            asset = created["asset"]
        else:
            upload_id = created["upload"]["id"]
            upload = self.api.get(f"/v2/uploads/{upload_id}")
            if upload["state"] != "completed":
                self.api.request(
                    "PUT",
                    f"/v2/uploads/{upload_id}/content",
                    content=item.content,
                    headers={"Content-Type": "application/octet-stream"},
                    expected=(200,),
                )
                upload, _ = self.api.post(
                    f"/v2/uploads/{upload_id}/complete",
                    {},
                    self.key("upload-complete", upload_id),
                    expected=(200,),
                )
            asset = upload["asset"]
        if asset["sha256"] != item.sha256 or asset["byte_size"] != len(item.content):
            raise ImportFailure(f"asset verification failed for {item.key}")
        self.assets_by_key[item.key] = asset
        return asset

    def verify_work(
        self, work_id: str, sample_id: str, expected_ocr_revisions: int, has_midi: bool
    ) -> dict[str, Any]:
        bundle, headers, _ = self.api.request("GET", f"/v2/works/{work_id}/bundle")
        arrangement = self.find_named(bundle["arrangements"], ARRANGEMENT_NAME)
        if arrangement is None:
            raise ImportFailure(f"verification failed: arrangement missing for {sample_id}")
        ocr_score = self.find_named(arrangement["scores"], OCR_SCORE_LABEL, field="label")
        if ocr_score is None:
            raise ImportFailure(f"verification failed: OCR score missing for {sample_id}")
        chain = self.score_revision_chain(ocr_score["head_revision_id"])
        if len(chain) != expected_ocr_revisions:
            raise ImportFailure(f"verification failed: OCR revision count differs for {sample_id}")

        playback = None
        range_verified = False
        if has_midi:
            rendition = self.find_named(
                arrangement["renditions"], MIDI_RENDITION_LABEL, field="label"
            )
            if rendition is None:
                raise ImportFailure(f"verification failed: MIDI rendition missing for {sample_id}")
            playback = self.api.get(f"/v2/renditions/{rendition['id']}/playback?prefer=midi")

        result = {
            "sample_id": sample_id,
            "work_id": work_id,
            "title": bundle["work"]["canonical_title"],
            "arrangements": len(bundle["arrangements"]),
            "scores": len(arrangement["scores"]),
            "renditions": len(arrangement["renditions"]),
            "ocr_revisions": len(chain),
            "bundle_etag": next(
                (value for name, value in headers.items() if name.lower() == "etag"), None
            ),
            "playback": bool(playback),
            "range_verified": range_verified,
        }
        return result

    def verify_ranges(self, results: list[dict[str, Any]]) -> None:
        for result in results:
            if not result.get("playback"):
                continue
            bundle = self.api.get(f"/v2/works/{result['work_id']}/bundle")
            arrangement = self.find_named(bundle["arrangements"], ARRANGEMENT_NAME)
            rendition = self.find_named(
                arrangement["renditions"], MIDI_RENDITION_LABEL, field="label"
            )
            playback = self.api.get(f"/v2/renditions/{rendition['id']}/playback?prefer=midi")
            request = Request(
                urljoin(self.api.base_url, playback["url"].lstrip("/")),
                headers={**self.api.common_headers, "Range": "bytes=0-15"},
                method="GET",
            )
            with urlopen(request, timeout=60) as response:
                content = response.read()
                if response.status != 206 or len(content) != 16 or not content.startswith(b"MThd"):
                    raise ImportFailure(f"Range verification failed for {result['sample_id']}")
                result["range_verified"] = True

    def cos_object(self, key: str, media_type: str, expected_sha: str | None = None) -> CosObject:
        if key in self.objects:
            return self.objects[key]
        response = self.cos.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        stream = body.get_raw_stream() if hasattr(body, "get_raw_stream") else body
        original_content = stream.read()
        original_digest = hashlib.sha256(original_content).hexdigest()
        if expected_sha and original_digest != expected_sha.lower():
            raise ImportFailure(f"COS SHA-256 differs from Mongo for {key}")
        content = original_content
        transform = None
        if media_type == MUSICXML_MEDIA_TYPE and b"<!DOCTYPE" in content.upper():
            content, substitutions = STANDARD_MUSICXML_DOCTYPE.subn(b"", content, count=1)
            if substitutions != 1 or b"<!DOCTYPE" in content.upper():
                raise ImportFailure(f"unsupported MusicXML DOCTYPE in {key}")
            transform = "strip-standard-musicxml-doctype-v1"
        digest = hashlib.sha256(content).hexdigest()
        item = CosObject(
            key=key,
            content=content,
            sha256=digest,
            media_type=media_type,
            original_sha256=original_digest,
            original_size=len(original_content),
            transform=transform,
        )
        self.objects[key] = item
        return item

    def source_ref(self, item: CosObject) -> str:
        base = f"cos://{self.bucket}/{item.key}"
        if item.transform is None:
            return base
        return (
            f"{base}#original_sha256={item.original_sha256}"
            f"&original_bytes={item.original_size}&transform={item.transform}"
        )

    def optional_scan(self, document: dict[str, Any]) -> CosObject | None:
        key = str(PurePosixPath(document["cos_key"]).with_name("source.jpg"))
        try:
            return self.cos_object(key, "image/jpeg")
        except Exception as error:
            status = getattr(error, "get_status_code", lambda: None)()
            if status == 404:
                return None
            raise

    def work_id_for_arrangement(self, arrangement_id: str) -> str:
        return self.api.get(f"/v2/arrangements/{arrangement_id}")["work_id"]

    def score_revision_chain(self, head_revision_id: str | None) -> list[dict[str, Any]]:
        reversed_chain = []
        revision_id = head_revision_id
        while revision_id:
            revision = self.api.get(f"/v2/score-revisions/{revision_id}")
            reversed_chain.append(revision)
            revision_id = revision.get("based_on_revision_id")
        return list(reversed(reversed_chain))

    @staticmethod
    def primary_hash(revision: dict[str, Any]) -> str:
        matches = [
            item["sha256"] for item in revision["assets"] if item["role"] == "primary_musicxml"
        ]
        if len(matches) != 1:
            raise ImportFailure(f"revision {revision['id']} lacks one primary MusicXML")
        return matches[0]

    @staticmethod
    def musicxml_parts(content: bytes) -> list[dict[str, Any]]:
        root = ET.fromstring(content)
        score_parts = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "score-part"]
        result = []
        used = set()
        for index, node in enumerate(score_parts, start=1):
            raw_code = re.sub(r"[^A-Za-z0-9_-]", "", node.attrib.get("id", ""))[:50]
            code = raw_code.upper() or f"P{index}"
            if code in used:
                code = f"P{index}"
            used.add(code)
            part_name = next(
                (
                    child.text.strip()
                    for child in node.iter()
                    if child.tag.rsplit("}", 1)[-1] == "part-name"
                    and child.text
                    and child.text.strip()
                ),
                code,
            )
            result.append({"code": code, "name": part_name[:200], "display_order": index})
        if not result:
            raise ImportFailure("MusicXML contains no score-part definitions")
        return result

    @staticmethod
    def find_named(
        items: list[dict[str, Any]], name: str, *, field: str = "name"
    ) -> dict[str, Any] | None:
        matches = [item for item in items if item.get(field) == name]
        if len(matches) > 1:
            raise ImportFailure(f"multiple {field}={name!r} records exist")
        return matches[0] if matches else None

    @staticmethod
    def revision_number(document_id: str) -> int:
        match = re.search(r":rev(\d+)$", document_id)
        if not match:
            raise ImportFailure(f"invalid GMUSIC revision id: {document_id}")
        return int(match.group(1))

    @staticmethod
    def key(*parts: str) -> str:
        raw = "\0".join(parts).encode()
        return f"cos-samples-v1-{parts[0]}-{hashlib.sha256(raw).hexdigest()[:32]}"


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ImportFailure(f"required environment variable {name} is missing")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://10.88.0.1:8010")
    parser.add_argument("--dry-run", action="store_true", help="read and validate COS/Mongo only")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        from pymongo import MongoClient
        from qcloud_cos import CosConfig, CosS3Client

        token = required_env("RHYTHM_BOOTSTRAP_TOKEN")
        region = required_env("COS_REGION")
        bucket = required_env("COS_BUCKET")
        mongo = MongoClient(required_env("MONGO_URI"))
        collection = mongo[required_env("MONGO_DB")][required_env("MONGO_COLL")]
        cos = CosS3Client(
            CosConfig(
                Region=region,
                SecretId=required_env("COS_SECRET_ID"),
                SecretKey=required_env("COS_SECRET_KEY"),
            )
        )
        importer = SampleImporter(
            api=ApiClient(args.api_base, token),
            collection=collection,
            cos_client=cos,
            bucket=bucket,
            dry_run=args.dry_run,
        )
        results = importer.run()
        if not args.dry_run:
            importer.verify_ranges(results)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    except (ImportFailure, ET.ParseError) as error:
        print(f"import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
