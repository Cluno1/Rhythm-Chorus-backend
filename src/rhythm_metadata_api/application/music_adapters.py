"""Read-only upstream adapters behind the stable Sonorus music-source contract.

Adding a different upstream protocol means registering another adapter here. Administrators
manage instances, URLs, ordering and availability; client response shapes do not change.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from rhythm_metadata_api.domain.v2.errors import V2DomainError, V2NotFound, V2Unavailable

MAX_JSON_BYTES = 2_000_000
_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_AUDIO_HOSTS = frozenset({"car-er.kuwo.cn", "car-bj.kuwo.cn", "car-lv.kuwo.cn"})
_ARTWORK_HOSTS = frozenset({"img1.kuwo.cn", "img2.kuwo.cn", "img3.kuwo.cn", "img4.kuwo.cn"})
_LRC_TIME = re.compile(r"^\[\d{1,3}:\d{2}(?:\.\d{1,3})?\]")
_PROXY_FAKE_IPS = ipaddress.ip_network("198.18.0.0/15")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(NoRedirect)


def clean_feiniao_lrc(value: str) -> str:
    """Remove the site's fixed promotional header while retaining song credits and verses."""
    kept = []
    for line in value.splitlines():
        stripped = line.strip()
        if stripped.startswith("[by:") and ("飞鸟·轻音" in stripped or "飞鸟.site" in stripped):
            continue
        lyric = _LRC_TIME.sub("", stripped).strip()
        if lyric in {
            "该音源由 飞鸟·轻音 提供服务",
            "官网:飞鸟.site",
            "官网：飞鸟.site",
            "————下面是正歌部分————",
        }:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def validate_public_url(url: str, *, hosts: frozenset[str] | None = None) -> str:
    """Reject local targets before a request; callers supply only fixed adapter-generated URLs."""
    parts = urlsplit(url)
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as error:
        raise V2DomainError("unsupported upstream URL") from error
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 80, 443}
        or (hosts is not None and host not in hosts)
    ):
        raise V2DomainError("unsupported upstream URL")
    try:
        addresses = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80))
        # Mihomo TUN returns RFC 2544 benchmark addresses for public DNS names.
        # The literal URL host is still a DNS name; local/other private targets remain blocked.
        if not addresses or any(
            not (address.is_global or address in _PROXY_FAKE_IPS)
            for item in addresses
            for address in (ipaddress.ip_address(item[4][0]),)
        ):
            raise V2DomainError("upstream must resolve only to public addresses")
    except (OSError, ValueError) as error:
        raise V2Unavailable("upstream DNS resolution failed") from error
    return url


def fetch_json(url: str) -> dict[str, Any]:
    validate_public_url(url)
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with _opener.open(request, timeout=8) as response:
            if not response.headers.get("Content-Type", "").lower().startswith("application/json"):
                raise V2Unavailable("upstream returned non-JSON content")
            body = response.read(MAX_JSON_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as error:
        raise V2Unavailable("music upstream is unavailable") from error
    if len(body) > MAX_JSON_BYTES:
        raise V2Unavailable("music upstream response is too large")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError) as error:
        raise V2Unavailable("music upstream returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise V2Unavailable("music upstream returned an invalid object")
    return payload


class MusicAdapter(Protocol):
    kind: str

    def search(self, base_url: str, source_id: str, query: str, page: int) -> dict: ...
    def recommend(self, base_url: str, source_id: str, page: int) -> dict: ...
    def playlists(self, base_url: str, source_id: str, page: int) -> dict: ...
    def playlist(self, base_url: str, source_id: str, playlist_id: str, page: int) -> dict: ...
    def song(self, base_url: str, source_id: str, song_id: str) -> dict: ...
    def lyrics(self, base_url: str, source_id: str, song_id: str) -> dict: ...
    def audio_url(self, base_url: str, song_id: str, quality: str) -> str: ...


class FeiniaoAdapter:
    kind = "feiniao"
    upstream_source = "酷我音乐"

    def __init__(self, fetch=fetch_json) -> None:
        self.fetch = fetch

    def _get(self, base_url: str, api: str, **params: object) -> dict:
        # No arbitrary path/query is taken from clients. Every supported operation is a fixed GET.
        url = base_url + "?" + urlencode({"api": api, **params})
        payload = self.fetch(url)
        code = payload.get("code")
        if code == 401:
            raise V2Unavailable("configured music source unexpectedly requires login")
        if code not in (None, 0, 200):
            raise V2Unavailable("music upstream rejected the request")
        return payload

    @staticmethod
    def _song(source_id: str, item: dict) -> dict | None:
        rid = str(item.get("rid") or "")
        if not _ID.fullmatch(rid):
            return None
        duration = item.get("duration")
        duration_ms = int(duration) * 1000 if isinstance(duration, (int, float)) else None
        artwork = FeiniaoAdapter._artwork(item.get("pic"))
        return {
            "id": f"{source_id}:{rid}",
            "source_id": source_id,
            "external_id": rid,
            "title": str(item.get("name") or ""),
            "artists": [str(item.get("artist") or "")],
            "album": item.get("album") if isinstance(item.get("album"), str) else None,
            "duration_ms": duration_ms,
            "artwork_url": artwork,
        }

    @staticmethod
    def _artwork(value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 2000:
            return None
        try:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or parts.hostname not in _ARTWORK_HOSTS:
                return None
        except ValueError:
            return None
        return f"/v2/music/artwork?url={quote(value, safe='')}"

    @staticmethod
    def _page(source_id: str, items: object, page: int, more: bool) -> dict:
        songs = [FeiniaoAdapter._song(source_id, item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        return {"items": [song for song in songs if song], "page": page, "has_more": more}

    def search(self, base_url: str, source_id: str, query: str, page: int) -> dict:
        payload = self._get(base_url, "search_with_source", key=query, pn=page, source=self.upstream_source)
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise V2Unavailable("music search response changed")
        return self._page(source_id, data.get("songs"), page, data.get("hasMore") is True)

    def recommend(self, base_url: str, source_id: str, page: int) -> dict:
        payload = self._get(base_url, "recommend", pn=page)
        pagination = payload.get("pagination") or {}
        return self._page(
            source_id, payload.get("data"), page,
            page < int(pagination.get("total_pages") or page) if isinstance(pagination, dict) else False,
        )

    def playlists(self, base_url: str, source_id: str, page: int) -> dict:
        payload = self._get(base_url, "playlist_home", pn=page)
        pagination = payload.get("pagination") or {}
        items = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "")
            if _ID.fullmatch(pid):
                items.append({
                    "id": f"{source_id}:{pid}", "source_id": source_id,
                    "external_id": pid, "name": str(item.get("name") or ""),
                    "artwork_url": self._artwork(item.get("img")),
                    "song_count": int(item.get("total_songs") or 0),
                })
        return {
            "items": items, "page": page,
            "has_more": page < int(pagination.get("total_pages") or page)
            if isinstance(pagination, dict) else False,
        }

    def playlist(self, base_url: str, source_id: str, playlist_id: str, page: int) -> dict:
        payload = self._get(base_url, "playlist_detail", pid=playlist_id, pn=page)
        pagination = payload.get("pagination") or {}
        result = self._page(
            source_id, payload.get("data"), page,
            page < int(pagination.get("total_pages") or page) if isinstance(pagination, dict) else False,
        )
        return {"id": f"{source_id}:{playlist_id}", **result}

    def song(self, base_url: str, source_id: str, song_id: str) -> dict:
        payload = self._get(base_url, "get_song_with_source", id=song_id, source=self.upstream_source)
        qualities = [str(item.get("type")) for item in payload.get("data") or []
                     if isinstance(item, dict) and item.get("url") and item.get("type") in {"mp3", "m4a", "ogg", "flac"}]
        return {
            "id": f"{source_id}:{song_id}", "source_id": source_id,
            "external_id": song_id, "qualities": qualities,
            "artwork_url": self._artwork(payload.get("pic")),
        }

    def lyrics(self, base_url: str, source_id: str, song_id: str) -> dict:
        payload = self._get(base_url, "get_lyrics_with_source", id=song_id, source=self.upstream_source)
        lyrics = payload.get("gc")
        if not isinstance(lyrics, str):
            raise V2NotFound("lyrics are unavailable")
        return {"id": f"{source_id}:{song_id}", "source_id": source_id, "lrc": clean_feiniao_lrc(lyrics)}

    def audio_url(self, base_url: str, song_id: str, quality: str) -> str:
        payload = self._get(base_url, "get_song_with_source", id=song_id, source=self.upstream_source)
        for item in payload.get("data") or []:
            if isinstance(item, dict) and item.get("type") == quality:
                url = item.get("url")
                if isinstance(url, str):
                    return validate_public_url(url, hosts=_AUDIO_HOSTS)
        raise V2NotFound("requested audio quality is unavailable")


ADAPTERS: dict[str, MusicAdapter] = {"feiniao": FeiniaoAdapter()}


def adapter_kinds() -> list[dict[str, str]]:
    return [{"id": key, "name": "飞鸟轻音" if key == "feiniao" else key} for key in ADAPTERS]
