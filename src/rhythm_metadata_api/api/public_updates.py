from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from email.utils import formatdate
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from rhythm_metadata_api.application.device_auth import DevicePrincipal
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.storage.cos_presign import presign_cos_get

router = APIRouter(prefix="/v2/app-updates", tags=["Sonorus updates"])
_FILE_NAME = re.compile(r"^[A-Za-z0-9._-]+\.apk$")
_CHANNEL_APPLICATION = {
    "debug": "io.github.cluno1.sonorus.debug",
    "stable": "io.github.cluno1.sonorus",
}
_ALLOWED_ABIS = {"arm64-v8a", "armeabi-v7a", "x86_64", "x86", "universal"}


def _normalize_digest(value: str) -> str:
    return value.replace(":", "").strip().lower()


def _principal(request: Request) -> DevicePrincipal:
    principal = getattr(request.state, "device_principal", None)
    if not isinstance(principal, DevicePrincipal):
        raise HTTPException(401, "device proof is required")
    return principal


def _settings(request: Request) -> Settings:
    return request.app.state.update_settings


def _configured_channel_identity(request: Request, channel: str) -> tuple[str, str]:
    application_id = _CHANNEL_APPLICATION.get(channel)
    if application_id is None:
        raise HTTPException(404, "update track not found")
    settings = _settings(request)
    certificate = _normalize_digest(
        settings.sonorus_debug_certificate_sha256
        if channel == "debug"
        else settings.sonorus_stable_certificate_sha256
    )
    if not settings.sonorus_updates_root or len(certificate) != 64:
        raise HTTPException(503, "Sonorus update track is not configured")
    return application_id, certificate


def _channel(request: Request) -> tuple[str, str, str]:
    principal = _principal(request)
    application_id = request.headers.get("X-Sonorus-Application-ID", "")
    channel = request.headers.get("X-Sonorus-Update-Channel", "")
    certificate = _normalize_digest(
        request.headers.get("X-Sonorus-Signing-Certificate-SHA256", "")
    )
    try:
        current_version = int(request.headers.get("X-Sonorus-Version-Code", ""))
    except ValueError:
        raise HTTPException(422, "invalid Sonorus version identity") from None
    if current_version <= 0:
        raise HTTPException(422, "invalid Sonorus version identity")
    expected_application_id, expected_certificate = _configured_channel_identity(request, channel)
    if (
        expected_application_id != application_id
        or principal.application_id != application_id
        or principal.signing_certificate_sha256 != certificate
    ):
        raise HTTPException(403, "registered application identity does not match update track")
    if certificate != expected_certificate:
        raise HTTPException(403, "APK certificate is not authorized for update track")
    return channel, application_id, certificate


class UpdateRepository:
    def __init__(self, root: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self._verified: set[tuple[str, int, int, str]] = set()

    def manifest(self, channel: str, version_code: int | None = None) -> tuple[bytes, dict[str, Any], Path]:
        path = (
            self.root / channel / "latest.json"
            if version_code is None
            else self.root / channel / "releases" / str(version_code) / "manifest.json"
        )
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raise HTTPException(404, "update manifest not found") from None
        if len(raw) > 1_000_000:
            raise HTTPException(500, "update manifest is too large")
        try:
            manifest = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(500, "update manifest is invalid") from None
        if not isinstance(manifest, dict):
            raise HTTPException(500, "update manifest is invalid")
        return raw, manifest, path

    def validate_manifest(
        self,
        manifest: dict[str, Any],
        channel: str,
        application_id: str,
        certificate: str,
        version_code: int | None = None,
    ) -> None:
        manifest_version = manifest.get("versionCode")
        assets = manifest.get("assets")
        if (
            manifest.get("schemaVersion") != 1
            or manifest.get("channel") != channel
            or manifest.get("applicationId") != application_id
            or _normalize_digest(str(manifest.get("signingCertificateSha256", "")))
            != certificate
            or manifest.get("signatureAlgorithm") != "Ed25519"
            or not isinstance(manifest.get("manifestSignature"), str)
            or not isinstance(manifest_version, int)
            or isinstance(manifest_version, bool)
            or manifest_version <= 0
            or not isinstance(manifest.get("versionName"), str)
            or not manifest["versionName"]
            or not isinstance(manifest.get("publishedAt"), str)
            or not manifest["publishedAt"]
            or not isinstance(manifest.get("minimumAndroidSdk"), int)
            or isinstance(manifest.get("minimumAndroidSdk"), bool)
            or manifest["minimumAndroidSdk"] <= 0
            or manifest.get("mandatory") is not False
            or not isinstance(manifest.get("releaseNotes"), list)
            or any(not isinstance(note, str) for note in manifest["releaseNotes"])
            or not isinstance(assets, list)
            or not assets
        ):
            raise HTTPException(500, "update manifest identity is invalid")
        if version_code is not None and manifest_version != version_code:
            raise HTTPException(500, "update manifest version directory is invalid")
        try:
            signature = base64.b64decode(manifest["manifestSignature"], validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(500, "update manifest signature is invalid") from None
        if len(signature) != 64:
            raise HTTPException(500, "update manifest signature is invalid")
        names: set[str] = set()
        for asset in assets:
            if not isinstance(asset, dict):
                raise HTTPException(500, "update asset metadata is invalid")
            name = asset.get("fileName")
            digest = str(asset.get("sha256", ""))
            size = asset.get("sizeBytes")
            expected_url = f"/v2/app-updates/files/{manifest_version}/{name}"
            if (
                asset.get("abi") not in _ALLOWED_ABIS
                or not isinstance(name, str)
                or not _FILE_NAME.fullmatch(name)
                or name in names
                or asset.get("url") != expected_url
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise HTTPException(500, "update asset metadata is invalid")
            names.add(name)

    def asset(self, channel: str, version_code: int, file_name: str, manifest: dict[str, Any]) -> tuple[Path, str]:
        if not _FILE_NAME.fullmatch(file_name):
            raise HTTPException(404, "update asset not found")
        expected = next(
            (
                item
                for item in manifest["assets"]
                if isinstance(item, dict) and item.get("fileName") == file_name
            ),
            None,
        )
        if expected is None:
            raise HTTPException(404, "update asset not found")
        release_dir = (self.root / channel / "releases" / str(version_code)).resolve()
        path = (release_dir / file_name).resolve()
        if path.parent != release_dir or not path.is_file():
            raise HTTPException(404, "update asset not found")
        digest = str(expected.get("sha256", "")).lower()
        stat = path.stat()
        if expected.get("sizeBytes") != stat.st_size or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(500, "update asset metadata is invalid")
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size, digest)
        if cache_key not in self._verified:
            actual = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    actual.update(chunk)
            if actual.hexdigest() != digest:
                raise HTTPException(500, "update asset digest mismatch")
            self._verified.add(cache_key)
        return path, digest


def _repository(request: Request) -> UpdateRepository:
    return request.app.state.update_repository


def _cos_download_url(
    request: Request,
    channel: str,
    version_code: int,
    digest: str,
) -> str | None:
    settings = _settings(request)
    if not settings.sonorus_updates_cos_bucket:
        return None
    try:
        url, _ = presign_cos_get(
            bucket=settings.sonorus_updates_cos_bucket,
            region=settings.cos_region,
            key=f"{channel}/releases/{version_code}/{digest}",
            secret_id=settings.cos_secret_id,
            secret_key=settings.cos_secret_key,
            expires_seconds=settings.cos_presign_expires_seconds,
        )
    except ValueError as error:
        raise HTTPException(503, "Sonorus update delivery is not configured") from error
    return url


def _head_response(path: Path, headers: dict[str, str]) -> Response:
    return Response(
        status_code=200,
        media_type="application/vnd.android.package-archive",
        headers={**headers, "Content-Length": str(path.stat().st_size)},
    )


def _redirect_response(location: str, headers: dict[str, str]) -> Response:
    return Response(
        status_code=307,
        headers={
            **headers,
            "Location": location,
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/latest")
def latest(request: Request) -> Response:
    channel, application_id, certificate = _channel(request)
    raw, manifest, path = _repository(request).manifest(channel)
    _repository(request).validate_manifest(manifest, channel, application_id, certificate)
    version_raw, _, _ = _repository(request).manifest(channel, manifest["versionCode"])
    if version_raw != raw:
        raise HTTPException(500, "latest manifest does not match immutable release")
    etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(
        raw,
        media_type="application/json",
        headers={
            "ETag": etag,
            "Last-Modified": formatdate(path.stat().st_mtime, usegmt=True),
            "Cache-Control": "private, no-cache",
        },
    )


def _file_response(version_code: int, file_name: str, request: Request) -> Response:
    channel, application_id, certificate = _channel(request)
    _, manifest, _ = _repository(request).manifest(channel, version_code)
    _repository(request).validate_manifest(
        manifest, channel, application_id, certificate, version_code
    )
    path, digest = _repository(request).asset(channel, version_code, file_name, manifest)
    etag = f'"{digest}"'
    if_match = request.headers.get("If-Match")
    if if_match is not None and if_match != etag:
        raise HTTPException(412, "update asset changed")
    headers = {
        "ETag": etag,
        "X-Checksum-SHA256": digest,
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    if request.method == "HEAD":
        return _head_response(path, headers)
    if request.headers.get("X-Sonorus-COS-Redirect") == "1":
        cos_url = _cos_download_url(request, channel, version_code, digest)
        if cos_url is not None:
            return _redirect_response(cos_url, headers)
    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        headers=headers,
    )


def _public_latest_file_response(channel: str, request: Request) -> Response:
    application_id, certificate = _configured_channel_identity(request, channel)
    raw, manifest, _ = _repository(request).manifest(channel)
    _repository(request).validate_manifest(manifest, channel, application_id, certificate)
    version_code = manifest["versionCode"]
    version_raw, _, _ = _repository(request).manifest(channel, version_code)
    if version_raw != raw:
        raise HTTPException(500, "latest manifest does not match immutable release")

    assets = manifest["assets"]
    selected = next((asset for asset in assets if asset["abi"] == "universal"), None)
    if selected is None:
        selected = next((asset for asset in assets if asset["abi"] == "arm64-v8a"), None)
    if selected is None:
        raise HTTPException(404, "browser-compatible update asset not found")

    path, digest = _repository(request).asset(
        channel, version_code, selected["fileName"], manifest
    )
    etag = f'"{digest}"'
    headers = {
        "ETag": etag,
        "X-Checksum-SHA256": digest,
        "X-Sonorus-Version-Code": str(version_code),
        "X-Sonorus-Version-Name": manifest["versionName"],
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "public, max-age=0, must-revalidate",
    }
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename=path.name,
        headers=headers,
    )


@router.get("/files/{version_code}/{file_name}")
def download(version_code: int, file_name: str, request: Request) -> Response:
    return _file_response(version_code, file_name, request)


@router.head("/files/{version_code}/{file_name}")
def head(version_code: int, file_name: str, request: Request) -> Response:
    return _file_response(version_code, file_name, request)


@router.get("/{channel}/latest.apk")
def public_latest_download(channel: str, request: Request) -> Response:
    return _public_latest_file_response(channel, request)


@router.head("/{channel}/latest.apk")
def public_latest_head(channel: str, request: Request) -> Response:
    return _public_latest_file_response(channel, request)
