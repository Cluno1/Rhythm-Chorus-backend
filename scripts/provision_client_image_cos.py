"""Provision and smoke-test the private COS bucket used by Labs images.

The command is intentionally explicit and idempotent. It never prints COS
credentials and it removes the small smoke-test objects before exiting.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.storage.cos_images import (
    CosImageGatewayError,
    TencentCosImageGateway,
)
from rhythm_metadata_api.infrastructure.storage.cos_presign import (
    presign_cos_get,
    presign_cos_post,
    presign_cos_put,
    presign_cos_request,
)

_CONFIRMATION = "APPLY_CLIENT_IMAGE_COS"
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


class ProvisioningError(RuntimeError):
    pass


class CosProvisioner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bucket = settings.client_image_cos_bucket
        self.region = settings.cos_region
        self.secret_id = settings.cos_secret_id
        self.secret_key = settings.cos_secret_key
        self.cos_host = f"{self.bucket}.cos.{self.region}.myqcloud.com"
        self.ci_host = f"{self.bucket}.ci.{self.region}.myqcloud.com"

    def _url(
        self,
        method: str,
        key: str = "",
        *,
        query: Iterable[tuple[str, str | None]] = (),
        headers: Mapping[str, str] | None = None,
        host: str | None = None,
    ) -> str:
        url, _ = presign_cos_request(
            method=method,
            bucket=self.bucket,
            region=self.region,
            key=key,
            secret_id=self.secret_id,
            secret_key=self.secret_key,
            expires_seconds=self.settings.client_image_presign_expires_seconds,
            query_parameters=query,
            headers=headers,
            host=host,
        )
        return url

    @staticmethod
    def _open(
        url: str,
        method: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        accepted: tuple[int, ...] = (200,),
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read(512 * 1024)
                if response.status not in accepted:
                    raise ProvisioningError(f"unexpected HTTP {response.status}")
                return response.status, payload
        except urllib.error.HTTPError as error:
            payload = error.read(16 * 1024)
            if error.code in accepted:
                return error.code, payload
            detail = payload.decode("utf-8", errors="replace")[:500]
            raise ProvisioningError(f"HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise ProvisioningError(f"request failed: {error.reason}") from error

    def ensure_bucket(self) -> None:
        try:
            self._open(self._url("HEAD"), "HEAD", accepted=(200,))
            print(f"bucket_exists={self.bucket}")
        except ProvisioningError as error:
            if "HTTP 404" not in str(error):
                raise
            headers = {"x-cos-acl": "private"}
            self._open(
                self._url("PUT", headers=headers),
                "PUT",
                body=b"",
                headers=headers,
                accepted=(200,),
            )
            print(f"bucket_created={self.bucket}")

        headers = {"x-cos-acl": "private"}
        self._open(
            self._url("PUT", query=(("acl", None),), headers=headers),
            "PUT",
            body=b"",
            headers=headers,
            accepted=(200,),
        )
        _, acl = self._open(
            self._url("GET", query=(("acl", None),)),
            "GET",
            accepted=(200,),
        )
        if b"AllUsers" in acl:
            raise ProvisioningError("bucket ACL still grants the global AllUsers group")
        print("bucket_private=ok")

    def _put_xml(self, feature: str, body: bytes) -> None:
        headers = {
            "Content-Type": "application/xml",
            "Content-MD5": base64.b64encode(hashlib.md5(body).digest()).decode(),
        }
        self._open(
            self._url(
                "PUT",
                query=((feature, None),),
                headers=headers,
            ),
            "PUT",
            body=body,
            headers=headers,
            accepted=(200,),
        )

    def configure_cors(self, allowed_origins: list[str]) -> None:
        root = ET.Element("CORSConfiguration")
        root.append(ET.Element("ResponseVary"))
        root[-1].text = "true"
        rule = ET.SubElement(root, "CORSRule")
        ET.SubElement(rule, "ID").text = "sonorus-client-images"
        for origin in allowed_origins:
            ET.SubElement(rule, "AllowedOrigin").text = origin
        for method in ("GET", "HEAD", "PUT"):
            ET.SubElement(rule, "AllowedMethod").text = method
        for header in ("Content-Type", "Content-MD5"):
            ET.SubElement(rule, "AllowedHeader").text = header
        for header in ("ETag", "x-cos-hash-crc64ecma"):
            ET.SubElement(rule, "ExposeHeader").text = header
        ET.SubElement(rule, "MaxAgeSeconds").text = "600"
        self._put_xml("cors", ET.tostring(root, encoding="utf-8", xml_declaration=True))
        _, configured = self._open(
            self._url("GET", query=(("cors", None),)),
            "GET",
            accepted=(200,),
        )
        if not all(origin.encode() in configured for origin in allowed_origins):
            raise ProvisioningError("CORS verification did not return every allowed origin")
        print(f"cors_origins={len(allowed_origins)}")

    def configure_lifecycle(self) -> None:
        root = ET.Element("LifecycleConfiguration")
        expiration = ET.SubElement(root, "Rule")
        ET.SubElement(expiration, "ID").text = "expire-client-image-temporary-objects"
        expiration_filter = ET.SubElement(expiration, "Filter")
        ET.SubElement(expiration_filter, "Prefix").text = "labs/images/tmp/"
        ET.SubElement(expiration, "Status").text = "Enabled"
        expiration_action = ET.SubElement(expiration, "Expiration")
        ET.SubElement(expiration_action, "Days").text = "1"

        multipart = ET.SubElement(root, "Rule")
        ET.SubElement(multipart, "ID").text = "abort-client-image-multipart-uploads"
        multipart_filter = ET.SubElement(multipart, "Filter")
        ET.SubElement(multipart_filter, "Prefix").text = "labs/images/"
        ET.SubElement(multipart, "Status").text = "Enabled"
        abort = ET.SubElement(multipart, "AbortIncompleteMultipartUpload")
        ET.SubElement(abort, "DaysAfterInitiation").text = "1"

        self._put_xml("lifecycle", ET.tostring(root, encoding="utf-8", xml_declaration=True))
        _, configured = self._open(
            self._url("GET", query=(("lifecycle", None),)),
            "GET",
            accepted=(200,),
        )
        if b"labs/images/tmp/" not in configured or b"<Days>1</Days>" not in configured:
            raise ProvisioningError("lifecycle verification failed")
        print("temporary_object_lifecycle_days=1")

    def _open_ci_feature(self, path: str, label: str) -> None:
        headers = {"Content-Type": "application/xml"}
        url, _ = presign_cos_post(
            self.bucket,
            self.region,
            path,
            self.secret_id,
            self.secret_key,
            self.settings.client_image_presign_expires_seconds,
            headers=headers,
            host=self.ci_host,
        )
        try:
            self._open(url, "POST", body=b"", headers=headers, accepted=(200,))
            print(f"{label}=enabled")
        except ProvisioningError as error:
            detail = str(error).lower()
            if "http 409" in detail or "already" in detail or "exist" in detail:
                print(f"{label}=already_enabled")
                return
            raise

    def enable_ci(self) -> None:
        self._open_ci_feature("", "ci_binding")
        self._open_ci_feature("file_bucket", "ci_file_processing")

    def smoke_test(self) -> None:
        token = uuid.uuid4().hex
        source_key = f"labs/images/tmp/provisioning/{token}/original"
        destination_key = f"labs/images/provisioning-check/{token}/original"
        content_md5 = base64.b64encode(hashlib.md5(_PNG_1X1).digest()).decode()
        content_sha256 = hashlib.sha256(_PNG_1X1).hexdigest()
        upload_headers = {"Content-Type": "image/png", "Content-MD5": content_md5}
        upload_url, _ = presign_cos_put(
            self.bucket,
            self.region,
            source_key,
            self.secret_id,
            self.secret_key,
            self.settings.client_image_presign_expires_seconds,
            headers=upload_headers,
        )
        gateway = TencentCosImageGateway(self.settings)
        try:
            self._open(
                upload_url,
                "PUT",
                body=_PNG_1X1,
                headers=upload_headers,
                accepted=(200,),
            )
            metadata = gateway.head(source_key)
            image_info = gateway.image_info(source_key)
            file_hash = None
            for attempt in range(5):
                try:
                    file_hash = gateway.sha256(source_key)
                    break
                except CosImageGatewayError:
                    if attempt == 4:
                        raise
                    time.sleep(2**attempt)
            if metadata.byte_size != len(_PNG_1X1):
                raise ProvisioningError("COS HEAD size mismatch")
            if image_info.image_format != "png" or image_info.frame_count != 1:
                raise ProvisioningError("CI imageInfo did not recognize the static PNG")
            if file_hash is None or file_hash.sha256 != content_sha256:
                raise ProvisioningError("CI SHA-256 mismatch")

            gateway.promote(source_key, destination_key)
            preview_url, _ = presign_cos_get(
                self.bucket,
                self.region,
                destination_key,
                self.secret_id,
                self.secret_key,
                self.settings.client_image_shared_expires_seconds,
                query_parameters=(("imageMogr2/thumbnail/512x512>", None),),
                host=self.settings.client_image_preview_host,
            )
            _, preview = self._open(preview_url, "GET", accepted=(200,))
            if not preview.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ProvisioningError("signed thumbnail response is not a PNG")
            print(
                "smoke_test=ok "
                f"format={image_info.image_format} size={metadata.byte_size} sha256={content_sha256}"
            )
        finally:
            for key in (source_key, destination_key):
                try:
                    gateway.delete(key)
                except CosImageGatewayError as error:
                    print(f"cleanup_warning={key}:{error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="apply COS configuration")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--allowed-origin", action="append", required=True)
    parser.add_argument("--smoke-test", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.apply or args.confirm != _CONFIRMATION:
        raise SystemExit(f"refusing to mutate COS without --apply --confirm {_CONFIRMATION}")
    settings = Settings()
    if not settings.client_image_cos_bucket:
        raise SystemExit("RHYTHM_CLIENT_IMAGE_COS_BUCKET is required")
    if not settings.client_image_preview_host:
        raise SystemExit("RHYTHM_CLIENT_IMAGE_PREVIEW_HOST is required")
    if not settings.client_image_ci_enabled:
        raise SystemExit("RHYTHM_CLIENT_IMAGE_CI_ENABLED must be true")
    if not settings.cos_secret_id or not settings.cos_secret_key:
        raise SystemExit("COS credentials are required")

    provisioner = CosProvisioner(settings)
    provisioner.ensure_bucket()
    provisioner.configure_cors(args.allowed_origin)
    provisioner.configure_lifecycle()
    provisioner.enable_ci()
    if args.smoke_test:
        provisioner.smoke_test()


if __name__ == "__main__":
    main()
