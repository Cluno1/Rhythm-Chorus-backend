from __future__ import annotations

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.storage.cos_presign import (
    presign_cos_delete,
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


class ClientImageObjectGateway(Protocol):
    def head(self, key: str) -> CosObjectMetadata: ...

    def promote(self, source_key: str, destination_key: str) -> None: ...

    def delete(self, key: str) -> None: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class TencentCosImageGateway:
    """Small ordinary-COS control-plane client that never downloads image bytes."""

    _MAX_METADATA_BODY = 256 * 1024

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bucket = settings.client_image_cos_bucket
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _credentials(self) -> tuple[str, str]:
        if not self.bucket:
            raise CosImageGatewayError("client image COS capability is not enabled")
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
            service_code = ""
            try:
                error_payload = error.read(16 * 1024)
                service_code = (ET.fromstring(error_payload).findtext(".//Code") or "").strip()
            except (ET.ParseError, OSError):
                pass
            if not service_code.replace("-", "").replace("_", "").isalnum():
                service_code = ""
            suffix = f" ({service_code})" if service_code else ""
            raise CosImageGatewayError(f"COS returned HTTP {error.code}{suffix}") from error
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
