from __future__ import annotations

import json
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.storage.cos_presign import (
    presign_cos_delete,
    presign_cos_get,
    presign_cos_head,
    presign_cos_put,
)


class CosImageGatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class CosObjectMetadata:
    byte_size: int
    content_type: str
    etag: str | None
    crc64: str | None


@dataclass(frozen=True)
class CosImageInfo:
    image_format: str
    width: int
    height: int
    byte_size: int
    md5_hex: str
    frame_count: int


@dataclass(frozen=True)
class CosFileHash:
    sha256: str
    byte_size: int | None
    etag: str | None


class ClientImageObjectGateway(Protocol):
    def head(self, key: str) -> CosObjectMetadata: ...

    def image_info(self, key: str) -> CosImageInfo: ...

    def sha256(self, key: str) -> CosFileHash: ...

    def promote(self, source_key: str, destination_key: str) -> None: ...

    def delete(self, key: str) -> None: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class TencentCosImageGateway:
    """Small COS/CI control-plane client that never downloads image bytes."""

    _MAX_METADATA_BODY = 256 * 1024

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bucket = settings.client_image_cos_bucket
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _credentials(self) -> tuple[str, str]:
        if not self.bucket or not self.settings.client_image_ci_enabled:
            raise CosImageGatewayError("client image COS/CI capability is not enabled")
        if not self.settings.cos_secret_id or not self.settings.cos_secret_key:
            raise CosImageGatewayError("COS credentials are not configured")
        return self.settings.cos_secret_id, self.settings.cos_secret_key

    def _open(self, request: urllib.request.Request, *, read_body: bool = False) -> tuple[object, bytes]:
        try:
            with self._opener.open(request, timeout=30) as response:
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    raise CosImageGatewayError(f"COS returned HTTP {status}")
                body = response.read(self._MAX_METADATA_BODY + 1) if read_body else b""
                if len(body) > self._MAX_METADATA_BODY:
                    raise CosImageGatewayError("COS metadata response is unexpectedly large")
                return response.headers, body
        except urllib.error.HTTPError as error:
            raise CosImageGatewayError(f"COS returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise CosImageGatewayError("COS request failed") from error

    def head(self, key: str) -> CosObjectMetadata:
        secret_id, secret_key = self._credentials()
        url, _ = presign_cos_head(
            self.bucket,
            self.settings.cos_region,
            key,
            secret_id,
            secret_key,
            self.settings.client_image_presign_expires_seconds,
        )
        headers, _ = self._open(urllib.request.Request(url, method="HEAD"))
        try:
            byte_size = int(headers.get("Content-Length", ""))  # type: ignore[attr-defined]
        except ValueError as error:
            raise CosImageGatewayError("COS HEAD omitted a valid Content-Length") from error
        content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()  # type: ignore[attr-defined]
        return CosObjectMetadata(
            byte_size=byte_size,
            content_type=content_type,
            etag=headers.get("ETag"),  # type: ignore[attr-defined]
            crc64=headers.get("x-cos-hash-crc64ecma"),  # type: ignore[attr-defined]
        )

    def image_info(self, key: str) -> CosImageInfo:
        secret_id, secret_key = self._credentials()
        url, _ = presign_cos_get(
            self.bucket,
            self.settings.cos_region,
            key,
            secret_id,
            secret_key,
            self.settings.client_image_presign_expires_seconds,
            query_parameters=(("imageInfo", None),),
        )
        _, body = self._open(urllib.request.Request(url, method="GET"), read_body=True)
        try:
            payload = json.loads(body)
            return CosImageInfo(
                image_format=str(payload["format"]).lower(),
                width=int(payload["width"]),
                height=int(payload["height"]),
                byte_size=int(payload["size"]),
                md5_hex=str(payload["md5"]).lower(),
                frame_count=int(payload.get("frame_count", "1")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CosImageGatewayError("COS imageInfo returned an invalid response") from error

    def sha256(self, key: str) -> CosFileHash:
        secret_id, secret_key = self._credentials()
        url, _ = presign_cos_get(
            self.bucket,
            self.settings.cos_region,
            key,
            secret_id,
            secret_key,
            self.settings.client_image_presign_expires_seconds,
            query_parameters=(("ci-process", "filehash"), ("type", "sha256")),
        )
        _, body = self._open(urllib.request.Request(url, method="GET"), read_body=True)
        try:
            root = ET.fromstring(body)
            digest = (root.findtext(".//SHA256") or "").strip().lower()
            size_text = (root.findtext(".//FileSize") or "").strip()
            etag = (root.findtext(".//Etag") or root.findtext(".//ETag") or "").strip()
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("invalid SHA-256")
            return CosFileHash(
                sha256=digest,
                byte_size=int(size_text) if size_text else None,
                etag=etag or None,
            )
        except (ET.ParseError, ValueError) as error:
            raise CosImageGatewayError("COS file hash returned an invalid response") from error

    def promote(self, source_key: str, destination_key: str) -> None:
        secret_id, secret_key = self._credentials()
        source = (
            f"{self.bucket}.cos.{self.settings.cos_region}.myqcloud.com/"
            f"{quote(source_key.lstrip('/'), safe='/-_.~')}"
        )
        signed_headers = {"x-cos-copy-source": source}
        url, _ = presign_cos_put(
            self.bucket,
            self.settings.cos_region,
            destination_key,
            secret_id,
            secret_key,
            self.settings.client_image_presign_expires_seconds,
            headers=signed_headers,
        )
        self._open(
            urllib.request.Request(url, data=b"", method="PUT", headers=signed_headers),
            read_body=True,
        )

    def delete(self, key: str) -> None:
        secret_id, secret_key = self._credentials()
        url, _ = presign_cos_delete(
            self.bucket,
            self.settings.cos_region,
            key,
            secret_id,
            secret_key,
            self.settings.client_image_presign_expires_seconds,
        )
        self._open(urllib.request.Request(url, method="DELETE"))
