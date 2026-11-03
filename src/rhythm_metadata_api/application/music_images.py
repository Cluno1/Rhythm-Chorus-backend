"""Archive bounded public music images in private COS and issue stable delivery URLs."""

from __future__ import annotations

import hashlib
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from rhythm_metadata_api.application.music_adapters import (
    _ARTWORK_HOSTS,
    _opener,
    validate_public_url,
)
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.storage.cos_presign import presign_cos_get, presign_cos_put

_IMAGE_NAME = re.compile(r"^([a-f0-9]{64})\.(jpg|png|webp|gif)$")
_PREFIX = "/v2/music/images/"
_MAX_BYTES = 5_000_000


class MusicImageArchiveError(RuntimeError):
    pass


def _detected_type(body: bytes) -> tuple[str, str]:
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise MusicImageArchiveError("upstream response is not a supported image")


class MusicImageArchive:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _original_url(self, value: str) -> str:
        if value.startswith("/v2/music/artwork?url="):
            parsed = urlsplit(value)
            urls = parse_qs(parsed.query).get("url", [])
            if parsed.path != "/v2/music/artwork" or len(urls) != 1:
                raise MusicImageArchiveError("invalid artwork proxy URL")
            value = urls[0]
        try:
            validate_public_url(value, hosts=_ARTWORK_HOSTS)
        except Exception as error:
            raise MusicImageArchiveError("artwork host is not supported for COS archival") from error
        return value

    def normalize(self, value: str | None) -> str | None:
        if not value:
            return None
        base = self.settings.music_image_public_base_url.rstrip("/")
        if base and value.startswith(base + _PREFIX):
            value = value[len(base):]
        if value.startswith(_PREFIX):
            if _IMAGE_NAME.fullmatch(value.removeprefix(_PREFIX)):
                return value
            raise MusicImageArchiveError("invalid COS music image URL")
        return value

    def archive(self, value: str | None) -> str | None:
        value = self.normalize(value)
        if not value or value.startswith(_PREFIX) or not self.settings.music_image_cos_bucket:
            return value
        source_url = self._original_url(value)
        try:
            with _opener.open(Request(source_url, headers={"Accept": "image/*"}), timeout=8) as response:
                body = response.read(_MAX_BYTES + 1)
                declared_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        except (HTTPError, URLError, TimeoutError) as error:
            raise MusicImageArchiveError("upstream image could not be fetched") from error
        if not body or len(body) > _MAX_BYTES:
            raise MusicImageArchiveError("upstream image is empty or too large")
        content_type, suffix = _detected_type(body)
        if declared_type != content_type:
            raise MusicImageArchiveError("upstream image content type does not match its bytes")
        digest = hashlib.sha256(body).hexdigest()
        name = f"{digest}.{suffix}"
        key = f"music-images/{digest[:2]}/{name}"
        upload_url, _ = presign_cos_put(
            self.settings.music_image_cos_bucket, self.settings.cos_region, key,
            self.settings.cos_secret_id, self.settings.cos_secret_key,
            headers={"Content-Type": content_type},
        )
        try:
            with _opener.open(Request(
                upload_url, data=body, method="PUT", headers={"Content-Type": content_type},
            ), timeout=15) as response:
                if response.status not in {200, 201, 204}:
                    raise MusicImageArchiveError("COS rejected the image upload")
        except (HTTPError, URLError, TimeoutError) as error:
            raise MusicImageArchiveError("COS image upload failed") from error
        return _PREFIX + name

    def delivery_url(self, value: str | None) -> str | None:
        if value and value.startswith(_PREFIX):
            return self.settings.music_image_public_base_url.rstrip("/") + value
        return value

    @staticmethod
    def storage(value: str | None) -> str:
        return "cos" if value and value.startswith(_PREFIX) else "external"

    def signed_url(self, name: str) -> str:
        match = _IMAGE_NAME.fullmatch(name)
        if not match or not self.settings.music_image_cos_bucket:
            raise MusicImageArchiveError("music image is unavailable")
        digest = match.group(1)
        key = f"music-images/{digest[:2]}/{name}"
        url, _ = presign_cos_get(
            self.settings.music_image_cos_bucket, self.settings.cos_region, key,
            self.settings.cos_secret_id, self.settings.cos_secret_key,
            expires_seconds=600,
        )
        return url
