"""Read-only upstream adapters behind the stable Sonorus music-source contract.

Adding a different upstream protocol means registering another adapter here. Administrators
manage instances, URLs, ordering and availability; client response shapes do not change.

Feiniao catalog invariant (issue 172):
- Only search, song detail, audio URL and lyrics take Feiniao's ``source`` parameter. Its
  recommend, hot playlist, playlist search and playlist detail APIs ignore it and always answer
  with Kuwo IDs. An instance whose ``upstream_source`` is not Kuwo (for example NetEase) therefore
  browses nothing: those calls return empty pages, or not found for a playlist, without contacting
  upstream. Otherwise its playlists would carry Kuwo song IDs under the NetEase instance, and
  playing them would resolve unrelated NetEase songs or fail.
- Rejected: a separate admin switch for browsing. It would let the same wrong IDs come back by
  misconfiguration, and nothing besides Kuwo has a playlist API at Feiniao today.

Kuwo direct adapter invariants (issue 173):
- Search, song info and lyrics call Kuwo's own anonymous APIs, the ones LX Music uses. Kuwo has no
  anonymous full-length audio API, so audio comes from administrator-configured resolvers, tried
  in order. A resolver either answers JSON holding the URL or redirects to it; either way the final
  URL must be on the instance's audio host list, so a resolver cannot point the gateway anywhere.
- A failing resolver, including one whose URL is off the host list, is logged and the next one is
  tried. Unlike Feiniao's single answer, falling through here is the point: third-party resolvers
  rate-limit and disappear without notice. Only when all fail is the source unavailable.
- Kuwo's song info answers "音乐查询失败" (status 301) for about half of identical requests, and its
  search sometimes reports a total with nothing shown. Both are retried at once, at most three
  calls in all, as LX does for search; a song that fails three times reads as having no lyrics.
- issue 174: Kuwo direct also serves the hot playlist list, playlist search and playlist detail.
  Feiniao's playlist IDs are Kuwo ``pid``s with the same songs in the same order, and both page
  details by 20, so a ``feiniao:<pid>`` page can be served by Kuwo with the same page number.
  Playlist search uses the ``client=kt`` parameters: LX's PC parameters return nothing for common
  words such as 流行 and answer with single-quoted pseudo-JSON.
- ``catalog`` names the platform whose IDs an adapter speaks. Feiniao on Kuwo and Kuwo direct both
  speak Kuwo ``rid``s, so one can stand in for the other with the same song ID (see the service).
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import math
import re
import socket
import unicodedata
from copy import copy
from typing import Any, ClassVar, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from rhythm_metadata_api.domain.v2.errors import V2DomainError, V2NotFound, V2Unavailable

log = logging.getLogger(__name__)

MAX_JSON_BYTES = 2_000_000
_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_AUDIO_HOSTS = frozenset({"car-er.kuwo.cn", "car-bj.kuwo.cn", "car-lv.kuwo.cn"})
_ARTWORK_HOSTS = frozenset({"img1.kuwo.cn", "img2.kuwo.cn", "img3.kuwo.cn", "img4.kuwo.cn"})
_LRC_TIME = re.compile(r"^(?:\[\d{1,3}:\d{2}(?:\.\d{1,3})?\])+")
_BY_TAG = re.compile(r"^\[by:", re.IGNORECASE)
AUDIO_QUALITIES = ("mp3", "m4a", "ogg", "flac")
AUTO_QUALITY = "auto"
# FLAC is left out on purpose: audio is relayed, so it would multiply gateway traffic.
DEFAULT_AUDIO_QUALITY_ORDER = ("mp3", "m4a", "ogg")
FEINIAO_LYRIC_FILTERS = {
    "exact_lines": ["该音源由 飞鸟·轻音 提供服务", "官网:飞鸟.site", "————下面是正歌部分————"],
    "by_tag_contains": ["飞鸟·轻音", "飞鸟.site"],
}
_PROXY_FAKE_IPS = ipaddress.ip_network("198.18.0.0/15")
_PLAYLIST_PAGE_SIZE = 20
# Feiniao's playlist and recommend APIs only serve this upstream; see the module docstring.
_CATALOG_UPSTREAM = "酷我音乐"
_FEINIAO_CATALOGS = {"酷我音乐": "kuwo", "网易云音乐": "netease"}
# issue 173: Kuwo's anonymous APIs, as used by LX Music (lyswhut/lx-music-desktop, Apache-2.0).
_KUWO_SONG_INFO = "http://m.kuwo.cn/newh5/singles/songinfoandlrc"
_KUWO_PAGE_SIZE = 30
_KUWO_ATTEMPTS = 3
_KUWO_COVER_PREFIX = "http://img1.kwcdn.kuwo.cn/star/albumcover/500/"
_KUWO_ARTIST_PREFIX = "http://img1.kwcdn.kuwo.cn/star/starheads/500/"
_KUWO_HOT_PLAYLISTS = "http://wapi.kuwo.cn/api/pc/classify/playlist/getRcmPlayList"
_KUWO_PLAYLIST_DETAIL = "http://nplserver.kuwo.cn/pl.svc"
_KUWO_HOT_PAGE_SIZE = 36
_KUWO_SONGLIST_DIGEST = "8"  # ordinary user playlists; other digests need different detail APIs
KUWO_AUDIO_HOSTS = (
    "bd-er.kuwo.cn", "bd-lw.kuwo.cn", "car-bj.kuwo.cn", "car-er.kuwo.cn", "car-lv.kuwo.cn", "car-lw.kuwo.cn",
)
KUWO_ARTWORK_HOSTS = (
    "img1.kwcdn.kuwo.cn", "img2.kwcdn.kuwo.cn", "img3.kwcdn.kuwo.cn", "img4.kwcdn.kuwo.cn", "star.kuwo.cn",
    "img1.kuwo.cn", "img2.kuwo.cn", "img3.kuwo.cn", "img4.kuwo.cn",
)
KUWO_DEFAULT_RESOLVERS = (
    {"name": "haitangw", "kind": "redirect",
     "url_template": "https://musicapi.haitangw.net/music/kw.php?type=mp3&id={id}&level=exhigh"},
    {"name": "nxinxz", "kind": "redirect",
     "url_template": "http://music.nxinxz.com/kw.php?id={id}&level=exhigh&type=mp3"},
    # Shared public key published in the Huibq LX script; it rate-limits bursts, so it goes last.
    {"name": "huibq", "kind": "json", "url_template": "https://lxmusicapi.onrender.com/url/kw/{id}/320k",
     "headers": {"X-Request-Key": "share-v3", "User-Agent": "lx-music-mobile/2.0.0"}},
)
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


def _count(value: object) -> int:
    """Upstream counts arrive as numbers or digit strings; anything else counts as zero."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


_opener = build_opener(NoRedirect)


def _normalized(value: str) -> str:
    """Compare text regardless of full-width punctuation and repeated whitespace."""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _line_is_filtered(line: str, exact_lines: set[str], by_tag_contains: list[str]) -> bool:
    stripped = line.strip()
    if _BY_TAG.match(stripped):
        normalized = _normalized(stripped)
        return any(text in normalized for text in by_tag_contains)
    return _normalized(_LRC_TIME.sub("", stripped)) in exact_lines


def split_lyrics(value: str, filters: dict) -> tuple[list[str], list[str]]:
    """Return (kept, removed) lines. Rules are literal text, never patterns, so they cannot hang."""
    exact_lines = {_normalized(text) for text in filters.get("exact_lines", [])}
    by_tag_contains = [_normalized(text) for text in filters.get("by_tag_contains", [])]
    kept: list[str] = []
    removed: list[str] = []
    for line in value.splitlines():
        (removed if _line_is_filtered(line, exact_lines, by_tag_contains) else kept).append(line)
    return kept, removed


def clean_lyrics(value: str, filters: dict) -> str:
    """Remove administrator-configured promotional lines; other lines keep their order and timing."""
    return "\n".join(split_lyrics(value, filters)[0]).strip()


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


def fetch_json(
    url: str, headers: dict[str, str] | None = None, *, strict_type: bool = True,
) -> dict[str, Any]:
    """Fetch one JSON object. ``strict_type=False`` accepts Kuwo's text/plain and text/html labels."""
    validate_public_url(url)
    request = Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with _opener.open(request, timeout=8) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if strict_type and not content_type.startswith("application/json"):
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


def fetch_redirect(url: str, headers: dict[str, str] | None = None) -> str:
    """Return where a resolver redirects to, without following it (the caller checks the host)."""
    validate_public_url(url)
    try:
        with _opener.open(Request(url, headers=headers or {}), timeout=8):
            pass
    except HTTPError as error:
        location = error.headers.get("Location") if error.code in _REDIRECT_CODES else None
        error.close()
        if location:
            return urljoin(url, location)
        raise V2Unavailable("audio resolver returned no audio location") from error
    except (URLError, TimeoutError) as error:
        raise V2Unavailable("audio resolver is unavailable") from error
    raise V2Unavailable("audio resolver did not redirect")


def artwork_proxy(value: object, source_id: str, hosts: frozenset[str]) -> str | None:
    """Gateway URL for an upstream image, or None when it is not on the instance's host list."""
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or parts.hostname not in hosts:
            return None
    except ValueError:
        return None
    return f"/v2/music/artwork?url={quote(value, safe='')}&source={quote(source_id, safe='')}"


def _lrc_time(value: object) -> str | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    centiseconds = round(seconds * 100)
    return f"{centiseconds // 6000:02d}:{centiseconds % 6000 / 100:05.2f}"


class MusicAdapter(Protocol):
    kind: str

    def search(self, base_url: str, source_id: str, query: str, page: int) -> dict: ...
    def recommend(self, base_url: str, source_id: str, page: int) -> dict: ...
    def playlists(self, base_url: str, source_id: str, page: int) -> dict: ...
    def search_playlists(self, base_url: str, source_id: str, query: str, page: int) -> dict: ...
    def playlist(self, base_url: str, source_id: str, playlist_id: str, page: int) -> dict: ...
    def song(self, base_url: str, source_id: str, song_id: str) -> dict: ...
    def lyrics(self, base_url: str, source_id: str, song_id: str) -> dict: ...
    def audio_url(self, base_url: str, song_id: str, quality: str) -> str: ...


class MetingSearchAdapter:
    """Search-only connector for the trusted local lyrics_provider service."""

    kind = "meting_search"
    browses_catalog = False
    supported_operations = frozenset({"song_search"})
    default_config: ClassVar[dict] = {"upstream_source": "netease"}
    page_size = 20

    def __init__(self, fetch=None) -> None:
        self.fetch = fetch or self._fetch
        self.upstream_source = "netease"

    def configured(self, config: dict) -> MetingSearchAdapter:
        adapter = copy(self)
        adapter.upstream_source = config["upstream_source"]
        if adapter.upstream_source not in {"netease", "tencent", "kugou"}:
            raise V2DomainError("unsupported Meting platform")
        return adapter

    @property
    def catalog(self) -> str:
        return self.upstream_source

    @staticmethod
    def _fetch(url: str) -> dict:
        # Only the administrator-configured internal provider URL is passed here. Redirects are
        # disabled so an internal service cannot turn this into a request to another host.
        try:
            with _opener.open(Request(url, headers={"Accept": "application/json"}), timeout=5) as response:
                if not response.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise V2Unavailable("Meting search returned non-JSON content")
                body = response.read(MAX_JSON_BYTES + 1)
        except (HTTPError, URLError, TimeoutError) as error:
            raise V2Unavailable("Meting search is unavailable") from error
        if len(body) > MAX_JSON_BYTES:
            raise V2Unavailable("Meting search response is too large")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError) as error:
            raise V2Unavailable("Meting search returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise V2Unavailable("Meting search returned an invalid object")
        return payload

    def search(self, base_url: str, source_id: str, query: str, page: int) -> dict:
        url = base_url.rstrip("/") + "/search?" + urlencode({
            "server": self.upstream_source, "q": query, "type": "title",
            "limit": self.page_size, "page": page,
        })
        payload = self.fetch(url)
        rows = payload.get("items")
        if not isinstance(rows, list):
            raise V2Unavailable("Meting search response changed")
        items = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            external_id = str(row.get("id") or "")
            title = row.get("title")
            artists = row.get("artists")
            if not _ID.fullmatch(external_id) or not isinstance(title, str) or not title.strip():
                continue
            if not isinstance(artists, list) or not all(isinstance(item, str) for item in artists):
                continue
            duration = row.get("duration_ms")
            items.append({
                "id": f"{source_id}:{external_id}", "source_id": source_id,
                "external_id": external_id, "title": title.strip(),
                "artists": artists, "album": row.get("album") if isinstance(row.get("album"), str) else None,
                "duration_ms": duration if isinstance(duration, int) and duration > 0 else None,
                "artwork_url": None,
            })
        return {"items": items, "page": page, "has_more": len(rows) >= self.page_size}


class FeiniaoAdapter:
    kind = "feiniao"
    upstream_source = "酷我音乐"
    default_config: ClassVar[dict] = {"lyric_filters": FEINIAO_LYRIC_FILTERS}

    def __init__(self, fetch=fetch_json) -> None:
        self.fetch = fetch
        self.audio_hosts = _AUDIO_HOSTS
        self.artwork_hosts = _ARTWORK_HOSTS
        self.audio_quality_order = DEFAULT_AUDIO_QUALITY_ORDER

    def configured(self, config: dict) -> FeiniaoAdapter:
        adapter = copy(self)
        adapter.audio_hosts = frozenset(config["audio_hosts"])
        adapter.artwork_hosts = frozenset(config["artwork_hosts"])
        adapter.upstream_source = config["upstream_source"]
        adapter.audio_quality_order = tuple(
            config.get("audio_quality_order") or DEFAULT_AUDIO_QUALITY_ORDER
        )
        return adapter

    @property
    def browses_catalog(self) -> bool:
        return self.upstream_source == _CATALOG_UPSTREAM

    @property
    def catalog(self) -> str:
        return _FEINIAO_CATALOGS.get(self.upstream_source, f"feiniao:{self.upstream_source}")

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

    def _song(self, source_id: str, item: dict) -> dict | None:
        rid = str(item.get("rid") or "")
        if not _ID.fullmatch(rid):
            return None
        duration = item.get("duration")
        duration_ms = int(duration) * 1000 if isinstance(duration, (int, float)) else None
        artwork = self._artwork(item.get("pic"), source_id)
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

    def _artwork(self, value: object, source_id: str) -> str | None:
        return artwork_proxy(value, source_id, self.artwork_hosts)

    def _page(self, source_id: str, items: object, page: int, more: bool) -> dict:
        songs = [self._song(source_id, item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        return {"items": [song for song in songs if song], "page": page, "has_more": more}

    def search(self, base_url: str, source_id: str, query: str, page: int) -> dict:
        payload = self._get(base_url, "search_with_source", key=query, pn=page, source=self.upstream_source)
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise V2Unavailable("music search response changed")
        return self._page(source_id, data.get("songs"), page, data.get("hasMore") is True)

    def recommend(self, base_url: str, source_id: str, page: int) -> dict:
        if not self.browses_catalog:
            return self._page(source_id, [], page, False)
        payload = self._get(base_url, "recommend", pn=page)
        pagination = payload.get("pagination") or {}
        return self._page(
            source_id, payload.get("data"), page,
            page < int(pagination.get("total_pages") or page) if isinstance(pagination, dict) else False,
        )

    def playlists(self, base_url: str, source_id: str, page: int) -> dict:
        if not self.browses_catalog:
            return {**self._page(source_id, [], page, False), "total_pages": page}
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
                    "artwork_url": self._artwork(item.get("img"), source_id),
                    "song_count": int(item.get("total_songs") or 0),
                })
        total_pages = int(pagination.get("total_pages") or page) if isinstance(pagination, dict) else page
        return {
            "items": items, "page": page,
            "has_more": page < total_pages,
            # issue 171: the recommended pool samples pages across the whole hot list.
            "total_pages": total_pages,
        }

    def search_playlists(self, base_url: str, source_id: str, query: str, page: int) -> dict:
        """issue 167: keyword playlist search; the upstream pages by 20 and reports a string total."""
        if not self.browses_catalog:
            return self._page(source_id, [], page, False)
        payload = self._get(base_url, "search_playlist", key=query, pn=page)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise V2Unavailable("playlist search response changed")
        items = []
        for item in data["list"]:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "")
            name = str(item.get("name") or "").strip()
            if not _ID.fullmatch(pid) or not name:
                continue
            items.append({
                "id": f"{source_id}:{pid}", "source_id": source_id, "external_id": pid, "name": name,
                "artwork_url": self._artwork(item.get("img"), source_id),
                "song_count": _count(item.get("total")),
            })
        total = _count(data.get("total"))
        # An empty page ends the search even if the reported total is larger.
        return {"items": items, "page": page, "has_more": bool(data["list"]) and page * _PLAYLIST_PAGE_SIZE < total}

    def playlist(self, base_url: str, source_id: str, playlist_id: str, page: int) -> dict:
        if not self.browses_catalog:
            raise V2NotFound("this music source has no playlists")
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
                     if isinstance(item, dict) and item.get("url") and item.get("type") in AUDIO_QUALITIES]
        return {
            "id": f"{source_id}:{song_id}", "source_id": source_id,
            "external_id": song_id, "qualities": qualities,
            "artwork_url": self._artwork(payload.get("pic"), source_id),
        }

    def lyrics(self, base_url: str, source_id: str, song_id: str) -> dict:
        payload = self._get(base_url, "get_lyrics_with_source", id=song_id, source=self.upstream_source)
        lyrics = payload.get("gc")
        if not isinstance(lyrics, str):
            raise V2NotFound("lyrics are unavailable")
        return {"id": f"{source_id}:{song_id}", "source_id": source_id, "lrc": lyrics}

    def audio_url(self, base_url: str, song_id: str, quality: str) -> str:
        payload = self._get(base_url, "get_song_with_source", id=song_id, source=self.upstream_source)
        candidates: dict[str, str] = {}
        for item in payload.get("data") or []:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                candidates.setdefault(str(item.get("type")), item["url"])
        # The Media3 cache key has no format, so "auto" must stay deterministic: the same upstream
        # formats always select the same one. MP3 first keeps every existing cached song valid.
        # A rejected host fails loudly instead of falling through, which would hide a bad host rule.
        for candidate in self.audio_quality_order if quality == AUTO_QUALITY else (quality,):
            if candidate in candidates:
                return validate_public_url(candidates[candidate], hosts=self.audio_hosts)
        raise V2NotFound("requested audio quality is unavailable")


class KuwoAdapter:
    """issue 173: Kuwo's own APIs as LX Music calls them; audio through configured resolvers."""

    kind = "kuwo"
    catalog = "kuwo"
    browses_catalog = True
    default_config: ClassVar[dict] = {
        "audio_hosts": list(KUWO_AUDIO_HOSTS),
        "artwork_hosts": list(KUWO_ARTWORK_HOSTS),
        "audio_quality_order": ["mp3"],
        "lyric_filters": {"exact_lines": [], "by_tag_contains": []},
        "url_resolvers": [dict(item) for item in KUWO_DEFAULT_RESOLVERS],
    }

    def __init__(self, fetch=fetch_json, locate=fetch_redirect) -> None:
        self.fetch = fetch
        self.locate = locate
        self.audio_hosts = frozenset(KUWO_AUDIO_HOSTS)
        self.artwork_hosts = frozenset(KUWO_ARTWORK_HOSTS)
        self.resolvers: list[dict] = [dict(item) for item in KUWO_DEFAULT_RESOLVERS]

    def configured(self, config: dict) -> KuwoAdapter:
        adapter = copy(self)
        adapter.audio_hosts = frozenset(config.get("audio_hosts") or KUWO_AUDIO_HOSTS)
        adapter.artwork_hosts = frozenset(config.get("artwork_hosts") or KUWO_ARTWORK_HOSTS)
        adapter.resolvers = [
            item for item in config.get("url_resolvers") or [] if item.get("enabled", True)
        ]
        return adapter

    @staticmethod
    def _text(value: object) -> str:
        return html.unescape(str(value or "")).strip()

    def _song(self, source_id: str, item: dict) -> dict | None:
        rid = str(item.get("MUSICRID") or "").removeprefix("MUSIC_")
        if not _ID.fullmatch(rid):
            return None
        duration = str(item.get("DURATION") or "")
        # About a third of results carry no album cover; the artist photo is the next best picture.
        album_cover = str(item.get("web_albumpic_short") or "")
        artist_cover = str(item.get("web_artistpic_short") or "")
        cover = (
            _KUWO_COVER_PREFIX + album_cover.split("/", 1)[1] if "/" in album_cover
            else _KUWO_ARTIST_PREFIX + artist_cover.split("/", 1)[1] if "/" in artist_cover
            else None
        )
        return {
            "id": f"{source_id}:{rid}",
            "source_id": source_id,
            "external_id": rid,
            "title": self._text(item.get("SONGNAME")),
            "artists": [self._text(item.get("ARTIST")).replace("&", "、")],
            "album": self._text(item.get("ALBUM")) or None,
            "duration_ms": int(duration) * 1000 if duration.isdigit() else None,
            "artwork_url": artwork_proxy(cover, source_id, self.artwork_hosts),
        }

    def search(self, base_url: str, source_id: str, query: str, page: int) -> dict:
        params = {
            "client": "kt", "all": query, "pn": page - 1, "rn": _KUWO_PAGE_SIZE, "uid": "794762570",
            "ver": "kwplayer_ar_9.2.2.1", "vipver": 1, "show_copyright_off": 1, "newver": 1,
            "ft": "music", "cluster": 0, "strategy": 2012, "encoding": "utf8", "rformat": "json",
            "vermerge": 1, "mobi": 1, "issubtitle": 1,
        }
        url = base_url + "r.s?" + urlencode(params)
        for _ in range(_KUWO_ATTEMPTS):
            payload = self.fetch(url, strict_type=False)
            items = payload.get("abslist")
            glitch = str(payload.get("TOTAL")) != "0" and str(payload.get("SHOW")) == "0"
            if isinstance(items, list) and not glitch:
                break
        else:
            raise V2Unavailable("kuwo search response changed")
        songs = [self._song(source_id, item) for item in items if isinstance(item, dict)]
        return {
            "items": [song for song in songs if song], "page": page,
            "has_more": page * _KUWO_PAGE_SIZE < _count(payload.get("TOTAL")),
        }

    def _song_info(self, song_id: str) -> dict:
        url = _KUWO_SONG_INFO + "?" + urlencode({"musicId": song_id})
        for _ in range(_KUWO_ATTEMPTS):
            payload = self.fetch(url, strict_type=False)
            data = payload.get("data")
            # issue 176: "301 音乐查询失败" sometimes still carries a data object without songinfo.
            if isinstance(data, dict) and payload.get("status", 200) == 200:
                return data
        raise V2NotFound("kuwo song is unavailable")

    def song(self, base_url: str, source_id: str, song_id: str) -> dict:
        try:
            info = self._song_info(song_id).get("songinfo") or {}
        except V2NotFound:
            # issue 176: the song itself still plays through the resolvers; only its cover is missing.
            log.warning("Kuwo song info unavailable, detail has no cover: song=%s", song_id)
            info = {}
        cover = str(info.get("pic") or "").replace("/albumcover/240/", "/albumcover/500/")
        return {
            "id": f"{source_id}:{song_id}", "source_id": source_id,
            "external_id": song_id, "qualities": ["mp3"] if self.resolvers else [],
            "artwork_url": artwork_proxy(cover, source_id, self.artwork_hosts),
        }

    def lyrics(self, base_url: str, source_id: str, song_id: str) -> dict:
        lines = []
        for item in self._song_info(song_id).get("lrclist") or []:
            stamp = _lrc_time(item.get("time")) if isinstance(item, dict) else None
            if stamp is not None:
                lines.append(f"[{stamp}]{self._text(item.get('lineLyric'))}")
        if not lines:
            raise V2NotFound("lyrics are unavailable")
        return {"id": f"{source_id}:{song_id}", "source_id": source_id, "lrc": "\n".join(lines)}

    def _resolve(self, resolver: dict, song_id: str) -> str:
        url = resolver["url_template"].replace("{id}", quote(song_id, safe=""))
        headers = resolver.get("headers") or {}
        if resolver.get("kind") == "json":
            target = self.fetch(url, headers, strict_type=False).get(resolver.get("json_url_field") or "url")
            if not isinstance(target, str):
                raise V2Unavailable("audio resolver returned no URL")
        else:
            target = self.locate(url, headers)
        return validate_public_url(target, hosts=self.audio_hosts)

    def audio_url(self, base_url: str, song_id: str, quality: str) -> str:
        if quality not in {AUTO_QUALITY, "mp3"}:
            raise V2NotFound("requested audio quality is unavailable")
        last_error: Exception | None = None
        for resolver in self.resolvers:
            try:
                return self._resolve(resolver, song_id)
            except (V2Unavailable, V2DomainError, V2NotFound) as error:
                log.warning(
                    "Kuwo audio resolver failed: resolver=%s song=%s error=%s",
                    resolver.get("name"), song_id, error,
                )
                last_error = error
        raise V2Unavailable("every configured audio resolver failed") from last_error

    def recommend(self, base_url: str, source_id: str, page: int) -> dict:
        return {"items": [], "page": page, "has_more": False}

    def _summary(self, source_id: str, pid: object, name: object, img: object, count: object) -> dict | None:
        pid, title = str(pid or ""), self._text(name)
        if not _ID.fullmatch(pid) or not title:
            return None
        return {
            "id": f"{source_id}:{pid}", "source_id": source_id, "external_id": pid, "name": title,
            "artwork_url": artwork_proxy(str(img or ""), source_id, self.artwork_hosts),
            "song_count": _count(count),
        }

    def playlists(self, base_url: str, source_id: str, page: int) -> dict:
        params = {
            "loginUid": 0, "loginSid": 0, "appUid": "76039576", "pn": page,
            "rn": _KUWO_HOT_PAGE_SIZE, "order": "hot",
        }
        payload = self.fetch(_KUWO_HOT_PLAYLISTS + "?" + urlencode(params), strict_type=False)
        data = payload.get("data")
        if payload.get("code") != 200 or not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise V2Unavailable("kuwo playlist list response changed")
        items = [
            self._summary(source_id, item.get("id"), item.get("name"), item.get("img"), item.get("total"))
            for item in data["data"]
            if isinstance(item, dict) and str(item.get("digest")) == _KUWO_SONGLIST_DIGEST
        ]
        total_pages = max(1, math.ceil(_count(data.get("total")) / _KUWO_HOT_PAGE_SIZE))
        return {
            "items": [item for item in items if item], "page": page,
            "has_more": page < total_pages, "total_pages": total_pages,
        }

    def search_playlists(self, base_url: str, source_id: str, query: str, page: int) -> dict:
        params = {
            "client": "kt", "all": query, "pn": page - 1, "rn": _PLAYLIST_PAGE_SIZE, "uid": "794762570",
            "ver": "kwplayer_ar_9.2.2.1", "vipver": 1, "ft": "playlist", "encoding": "utf8",
            "rformat": "json", "mobi": 1,
        }
        payload = self.fetch(base_url + "r.s?" + urlencode(params), strict_type=False)
        rows = payload.get("abslist")
        if not isinstance(rows, list):
            raise V2Unavailable("kuwo playlist search response changed")
        items = [
            self._summary(source_id, row.get("playlistid"), row.get("name"), row.get("pic"), row.get("songnum"))
            for row in rows if isinstance(row, dict)
        ]
        return {
            "items": [item for item in items if item], "page": page,
            "has_more": bool(rows) and page * _PLAYLIST_PAGE_SIZE < _count(payload.get("TOTAL")),
        }

    def _playlist_song(self, source_id: str, item: dict) -> dict | None:
        rid = str(item.get("id") or "")
        if not _ID.fullmatch(rid):
            return None
        duration = str(item.get("duration") or "")
        cover = str(item.get("albumpic") or "").replace("/albumcover/120/", "/albumcover/500/")
        return {
            "id": f"{source_id}:{rid}", "source_id": source_id, "external_id": rid,
            "title": self._text(item.get("name")),
            "artists": [self._text(item.get("artist")).replace("&", "、")],
            "album": self._text(item.get("album")) or None,
            "duration_ms": int(duration) * 1000 if duration.isdigit() else None,
            "artwork_url": artwork_proxy(cover, source_id, self.artwork_hosts),
        }

    def playlist(self, base_url: str, source_id: str, playlist_id: str, page: int) -> dict:
        params = {
            "op": "getlistinfo", "pid": playlist_id, "pn": page - 1, "rn": _PLAYLIST_PAGE_SIZE,
            "encode": "utf8", "keyset": "pl2012", "identity": "kuwo", "pcmp4": 1,
            "vipver": "MUSIC_9.0.5.0_W1", "newver": 1,
        }
        url = _KUWO_PLAYLIST_DETAIL + "?" + urlencode(params)
        for _ in range(_KUWO_ATTEMPTS):
            payload = self.fetch(url, strict_type=False)
            rows = payload.get("musiclist")
            if payload.get("result") == "ok" and isinstance(rows, list):
                break
        else:
            raise V2Unavailable("kuwo playlist detail failed")
        songs = [self._playlist_song(source_id, row) for row in rows if isinstance(row, dict)]
        return {
            "id": f"{source_id}:{playlist_id}", "items": [song for song in songs if song], "page": page,
            "has_more": page * _PLAYLIST_PAGE_SIZE < _count(payload.get("total")),
        }


ADAPTERS: dict[str, MusicAdapter] = {
    "feiniao": FeiniaoAdapter(), "kuwo": KuwoAdapter(), "meting_search": MetingSearchAdapter(),
}
_ADAPTER_NAMES = {
    "feiniao": "飞鸟轻音", "kuwo": "酷我直连（LX 接口）",
    "meting_search": "Meting 搜索（网易/QQ/酷狗）",
}


def adapter_kinds() -> list[dict]:
    return [
        {
            "id": key,
            "name": _ADAPTER_NAMES.get(key, key),
            "default_config": getattr(adapter, "default_config", {}),
            "supported_operations": sorted(
                getattr(adapter, "supported_operations", (
                    {"song_search", "song_detail", "audio_resolve", "lyrics"}
                    | ({"playlist_search", "playlist_detail", "recommend"}
                       if getattr(adapter, "browses_catalog", True) else set())
                ))
            ),
        }
        for key, adapter in ADAPTERS.items()
    ]
