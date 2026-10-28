"""issue 127: Chinese lyrics from public music platforms, via the internal Meting provider.

Invariants
----------
* Read-only and never persisted: results live only in a bounded in-process cache. Third-party
  lyrics are copyrighted and must not be copied into Works (issue 127 §5).
* Matching follows issue 107's lyrics search: a hit whose title contains the query (same
  ``library_search.normalize``) is a ``title`` hit and sorts before ``lyrics`` hits.
* One failing platform never fails the request; it is listed in ``unavailable_sources`` and
  logged once per call. Only "provider not configured" or "every source failed" is a 503.

Why this way
------------
Platform endpoints and encryption change often (NetEase now encrypts its web search, QQ moved
to ``search_for_qq_cp`` in Meting 1.6.2). Keeping them in ``lyrics_provider`` (Node,
``@meting/core``) lets an upgrade be a version bump instead of backend code.

What breaks silently if changed
-------------------------------
* Dropping the ``(title, first artist)`` de-duplication shows the same song three times, once
  per platform, pushing real alternatives off the first screen.
* Caching failed searches would hide a platform for the cache lifetime after one blip; only
  successful source answers are cached.

Rejected
--------
* Calling the platforms from Python: duplicates Meting's eapi encryption and its fixes.
* Calling them from the Android client: an app release per platform change, and no shared cache.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from rhythm_metadata_api.application.library_search import normalize
from rhythm_metadata_api.domain.v2.errors import V2DomainError, V2NotFound, V2Unavailable

log = logging.getLogger(__name__)

SUPPORTED_SOURCES = ("netease", "tencent", "kugou")
SEARCH_TTL_SECONDS = 10 * 60
LYRICS_TTL_SECONDS = 24 * 60 * 60
CACHE_MAX_ENTRIES = 512
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_TIMESTAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_TAG_LINE = re.compile(
    r"^\[(?:ti|ar|al|by|offset|re|ve|length|au|kana|language|#)[:：].*\]$", re.IGNORECASE
)
# Credit lines the platforms prepend: "作词 : 刘凤瑶", "词：王铮亮", "混音/母带：蒋柽".
_CREDIT = re.compile(
    r"^\s*(?:作词|作曲|编曲|词|曲|制作人|监制|和声|和声编写|录音|录音师|录音棚|混音|母带|混音/母带|吉他|贝斯|Bass|"
    r"鼓|键盘|弦乐|配唱制作人|出品|出品人|发行|版权|OP|SP|策划|统筹|宣传|封面|原唱|演唱|歌手|专辑|"
    r"[^:：]{0,12}(?:录音师|工程师|制作|监制|编写))\s*[:：]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SearchHit:
    source: str
    external_id: str
    title: str
    artists: tuple[str, ...]
    album: str | None
    duration_ms: int | None
    match_field: str
    snippet: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "external_id": self.external_id,
            "title": self.title,
            "artists": list(self.artists),
            "album": self.album,
            "duration_ms": self.duration_ms,
            "match_field": self.match_field,
            "snippet": self.snippet,
        }


class _TtlCache:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._items: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> Any | None:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires <= self._clock():
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: tuple, value: Any, ttl: float) -> None:
        with self._lock:
            self._items[key] = (self._clock() + ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > CACHE_MAX_ENTRIES:
                self._items.popitem(last=False)


def _lrc_time(match: re.Match[str]) -> float:
    fraction = match.group(3) or "0"
    return int(match.group(1)) * 60 + int(match.group(2)) + int(fraction) / (10 ** len(fraction))


def clean_lrc(lrc: str, title: str | None = None) -> tuple[str, str]:
    """Return (synced LRC, plain text) without ID tags, credit lines or a leading title line."""
    timed: list[tuple[float, str]] = []
    title_norm = normalize(title or "")
    for raw_line in (lrc or "").splitlines():
        line = raw_line.strip()
        if not line or _TAG_LINE.match(line):
            continue
        stamps = list(_TIMESTAMP.finditer(line))
        text = _TIMESTAMP.sub("", line).strip()
        if not stamps:
            continue
        if _CREDIT.match(text):
            continue
        # QQ/Kugou start with "旅人 - 王铮亮" at 00:00; NetEase with "作词 : …" (handled above).
        if (
            not timed
            and " - " in text
            and title_norm
            and normalize(text.split(" - ")[0]).startswith(title_norm)
        ):
            continue
        for stamp in stamps:
            timed.append((_lrc_time(stamp), text))
    timed.sort(key=lambda item: item[0])
    synced = "\n".join(f"[{int(t // 60):02d}:{t % 60:05.2f}]{text}" for t, text in timed)
    plain_lines: list[str] = []
    for _, text in timed:
        if text or (plain_lines and plain_lines[-1]):
            plain_lines.append(text)
    plain = "\n".join(plain_lines).strip()
    return synced, plain


class ExternalLyricsService:
    def __init__(
        self,
        base_url: str,
        *,
        sources: tuple[str, ...] = ("netease", "tencent"),
        timeout_seconds: float = 4.0,
        fetch: Callable[[str, float], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.sources = sources
        self.timeout = timeout_seconds
        self._fetch = fetch or self._http_get
        self._cache = _TtlCache(clock)
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="external-lyrics")

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    @staticmethod
    def _http_get(url: str, timeout: float) -> Any:
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _call(self, path: str, params: dict[str, Any]) -> Any:
        return self._fetch(f"{self.base_url}{path}?{urlencode(params)}", self.timeout)

    def _source_search(
        self, source: str, query: str, kind: str, limit: int
    ) -> list[dict[str, Any]]:
        key = ("search", source, kind, query, limit)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        data = self._call("/search", {"server": source, "q": query, "type": kind, "limit": limit})
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise TypeError("malformed provider search response")
        self._cache.put(key, items, SEARCH_TTL_SECONDS)
        return items

    def search(self, query: str, limit: int) -> dict[str, Any]:
        if not self.enabled:
            raise V2Unavailable("external lyrics provider is not configured")
        query = query.strip()
        needle = normalize(query)
        # NetEase has a lyric full-text index; the other platforms answer title and lyrics alike.
        jobs: list[tuple[str, str]] = []
        for source in self.sources:
            jobs.append((source, "title"))
            if source == "netease":
                jobs.append((source, "lyrics"))
        per_source = min(20, max(5, limit))
        futures = {
            job: self._pool.submit(self._source_search, job[0], query, job[1], per_source)
            for job in jobs
        }
        answered: dict[tuple[str, str], list[dict[str, Any]]] = {}
        failed: set[str] = set()
        for job, future in futures.items():
            try:
                answered[job] = future.result()
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as error:
                failed.add(job[0])
                log.warning(
                    "external lyrics search failed source=%s kind=%s error=%s",
                    job[0],
                    job[1],
                    type(error).__name__,
                )
        if not answered:
            raise V2Unavailable(
                "no external lyrics source answered", unavailable_sources=sorted(failed)
            )

        title_hits: list[SearchHit] = []
        lyric_hits: list[SearchHit] = []
        seen: set[tuple[str, str]] = set()
        for job in jobs:
            for raw in answered.get(job, []):
                hit = _to_hit(raw, needle)
                if hit is None:
                    continue
                identity = (normalize(hit.title), normalize(hit.artists[0]) if hit.artists else "")
                if identity in seen:
                    continue
                seen.add(identity)
                (title_hits if hit.match_field == "title" else lyric_hits).append(hit)
        items = (title_hits + lyric_hits)[:limit]
        unavailable = sorted(
            source for source in failed if not any(job[0] == source for job in answered)
        )
        return {
            "query": query,
            "type": "external_lyrics",
            "fallback": None,
            "items": [item.to_json() for item in items],
            "unavailable_sources": unavailable,
        }

    def lyrics(self, source: str, external_id: str, title: str | None = None) -> dict[str, Any]:
        if source not in SUPPORTED_SOURCES:
            raise V2NotFound(f"unknown lyrics source {source}")
        if not _EXTERNAL_ID.match(external_id):
            raise V2DomainError("invalid external lyrics id")
        if not self.enabled:
            raise V2Unavailable("external lyrics provider is not configured")
        key = ("lyrics", source, external_id)
        data = self._cache.get(key)
        if data is None:
            try:
                data = self._call("/lyric", {"server": source, "id": external_id})
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
                log.warning(
                    "external lyrics fetch failed source=%s error=%s", source, type(error).__name__
                )
                raise V2Unavailable(
                    "external lyrics source did not answer", source=source
                ) from error
            if not isinstance(data, dict):
                raise V2Unavailable("malformed provider lyrics response", source=source)
            self._cache.put(key, data, LYRICS_TTL_SECONDS)
        synced, plain = clean_lrc(str(data.get("lyric") or ""), title)
        if not plain:
            raise V2NotFound("this song has no lyrics on the source platform")
        translation_synced, translation = clean_lrc(str(data.get("tlyric") or ""), title)
        return {
            "source": source,
            "external_id": external_id,
            "lyrics": plain,
            "synced_lrc": synced,
            "translation": translation or None,
            "synced_translation": translation_synced or None,
        }


def _to_hit(raw: Any, needle: str) -> SearchHit | None:
    if not isinstance(raw, dict):
        return None
    source, external_id, title = raw.get("source"), raw.get("id"), raw.get("title")
    if (
        source not in SUPPORTED_SOURCES
        or not isinstance(external_id, str)
        or not _EXTERNAL_ID.match(external_id)
    ):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    artists = tuple(str(name).strip() for name in raw.get("artists") or [] if str(name).strip())
    duration = raw.get("duration_ms")
    snippet = (
        raw.get("snippet")
        if isinstance(raw.get("snippet"), str) and raw.get("snippet").strip()
        else None
    )
    album = str(raw.get("album") or "").strip() or None
    match_field = "title" if needle and needle in normalize(title) else "lyrics"
    return SearchHit(
        source=source,
        external_id=external_id,
        title=title.strip(),
        artists=artists,
        album=album,
        duration_ms=duration if isinstance(duration, int) and duration > 0 else None,
        match_field=match_field,
        snippet=snippet if match_field == "lyrics" else None,
    )
