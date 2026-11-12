"""Persisted source registry and unified client-facing music operations.

Lyric filter invariants:
- Filters run on upstream reads, automatic writes and every read, so a new rule applies at once,
  including to old rows and restored backups. Manually edited lyrics are never filtered.
- Accepted residual: removing a rule does not restore lines already filtered from stored automatic
  lyrics, because clients submit filtered text. Refreshing the song from upstream restores them;
  this is why administrators can preview a rule before saving it.
- Saving rules bumps the source revision, so each song re-resolves its audio URL once.
- config PATCH merges only submitted fields. A whole-object replace would let an older Web build,
  whose schema strips unknown fields, silently reset rules it does not know about.

Public song lyrics invariants (issue 166):
- ``lyrics_mode`` records who chose the lyrics, apart from the record-wide ``mode``. ``PUT .../lyrics``
  with ``auto`` writes only while no lyrics are stored, as one conditional UPDATE, so two clients
  that both fell back to LRCLIB cannot replace each other. ``manual`` always replaces them; the user
  decided that the latest manual choice wins for everyone.
- An automatic whole-record ``PUT .../metadata`` never blanks stored lyrics and never replaces
  manually chosen lyrics. Otherwise "refresh from the song's source", which finds no lyrics for
  exactly these songs, would erase the fallback lyrics again.
- Rejected: reusing the whole-record ``PUT`` for fallback lyrics. It also rewrites title, artwork and
  the record-wide mode, and a manual lyric would block automatic tag refreshes for the whole song.

Recommended album invariants:
- Requests never wait on upstream while a pool is fresh, and the pool refreshes once per hour per
  worker. Filters run per request, so a new song count range applies at once.
- A failed refresh keeps serving the old pool and waits five minutes before retrying, otherwise a
  broken upstream would add seconds to every home screen request.
- Accepted residual: each worker holds its own pool, so workers may return different playlists.
  This is harmless for a random recommendation and each pool expires by itself.
- issue 171, rotation: the hot list has hundreds of pages but its first pages change slowly, so
  the pool samples pages across the whole list (``total_pages`` from upstream, half of them from
  the newer front pages, never the short last page; page 1 is not pinned) and adds one keyword
  playlist search, the keyword rotating every hour. Playlists returned in the last 72 hours are
  only used to fill up when too few unseen ones remain.
- Accepted residual: the "recently returned" record lives in process memory and is shared by all
  users. A restart or a second worker starts with an empty record, which only falls back to plain
  random picks for a while; it is capped so it cannot grow without bound. Rejected: a table or
  Redis for this, which adds a write to every home screen request for a best-effort preference.
- issue 181: the keyword list is one setting for all sources (``v2_music_settings``, edited on the
  Web music-sources page), not part of each source's config: every source used the same words, and
  the per-source field had no editor. A missing row (in-memory databases) means the default words.
  An old per-source ``recommended_keywords`` key is ignored and dropped on that source's next save.
- issue 172: sources whose adapter does not browse playlists (a Feiniao instance on NetEase) are
  skipped when building pools, so they never trigger an empty refresh and its five-minute retry.

Standby source invariants (issue 173):
- Sources whose adapters share a ``catalog`` (Feiniao on Kuwo and Kuwo direct both use Kuwo rids)
  stand in for each other. Search asks only the first healthy one per catalog in priority order,
  so the same songs are not listed twice; detail, lyrics and audio for ``a:<rid>`` fall back to
  ``b:<rid>`` and keep the ``a:`` ID, so songs already saved on phones keep playing.
- A source that answered "unavailable" is skipped for 60 seconds while a standby exists, so an
  outage does not add an upstream timeout to every request. The record lives in process memory:
  a restart forgets it and the source is simply tried again (self-healing, no table).
- A fallback audio URL is cached under the requested source's key, so seeks reuse it and the
  existing invalidate-and-retry path re-resolves through the same fallback.
- issue 174, playlists: playlist search and the home pool follow the same rule as song search,
  one source per catalog. The home pool keeps using the first source whose pool is non-empty, so
  Kuwo direct builds no pool while Feiniao has one; while Feiniao is skipped as down, its last pool
  is still served (opened through the playlist standby) instead of refreshing it. A refresh that
  hits "unavailable" marks the source down and stops, rather than timing out on every page.
  ``feiniao:<pid>`` playlist pages fall back to Kuwo with the same pid and page and keep the
  ``feiniao:`` playlist ID; their songs carry the serving source's IDs, which play either way.
- issue 176: Kuwo direct details may carry no cover, so clients fill in the cover they display,
  which is the raw upstream ``http://`` URL (or the gateway proxy URL with this server's host).
  ``put_metadata`` turns any whitelisted upstream image into the proxy path and strips the host
  from an absolute proxy URL. Any other unusable image only drops that field (warning logged) and
  keeps the stored value: rejecting the whole write used to lose the lyrics with it (HTTP 422).
- Not covered: a URL that resolves but whose CDN then fails mid-stream; the stream route's single
  retry handles that as before. Configuration errors (a host rule rejection, V2DomainError) do not
  fall back, so a bad rule stays visible instead of being masked by the standby.
"""

from __future__ import annotations

import ipaddress
import logging
import random
import re
import threading
import time
from collections import OrderedDict
from datetime import UTC
from time import monotonic
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from rhythm_metadata_api.application.music_adapters import (
    _ARTWORK_HOSTS,
    _AUDIO_HOSTS,
    _ID,
    ADAPTERS,
    AUDIO_QUALITIES,
    AUTO_QUALITY,
    DEFAULT_AUDIO_QUALITY_ORDER,
    adapter_kinds,
    artwork_proxy,
    clean_lyrics,
    split_lyrics,
)
from rhythm_metadata_api.application.music_images import MusicImageArchive, MusicImageArchiveError
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.domain.v2.errors import (
    V2Conflict,
    V2DomainError,
    V2NotFound,
    V2Unavailable,
)
from rhythm_metadata_api.infrastructure.db.models import (
    MusicSettings,
    MusicSongMetadata,
    MusicSource,
    utc_now,
)

log = logging.getLogger(__name__)
_KEEP = object()  # issue 176: marker for "leave the stored image as it is"

_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
_AUDIO_URL_TTL_SECONDS = 150
_RECOMMENDED_POOL_TTL_SECONDS = 3600
_RECOMMENDED_RETRY_SECONDS = 300
_RECOMMENDED_FRONT_PAGES = 2
_RECOMMENDED_ANY_PAGES = 2
_RECOMMENDED_FRONT_LAST_PAGE = 50
_RECOMMENDED_SEEN_SECONDS = 72 * 3600
_RECOMMENDED_SEEN_MAX = 5000
_RECOMMENDED_KEYWORDS = (
    "流行", "民谣", "摇滚", "古典", "治愈", "轻音乐", "爵士", "粤语", "经典老歌", "华语",
    "欧美", "日语", "说唱", "电子", "学习", "运动", "睡前", "咖啡馆", "钢琴", "怀旧",
)
_SOURCE_DOWN_SECONDS = 60
_RECOMMENDED_MIN_SONGS = 10
_RECOMMENDED_MAX_SONGS = 200
_HOST = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


AudioQuality = Literal["mp3", "m4a", "ogg", "flac"]
_LYRIC_RULE_MIN = 2
_LYRIC_RULE_MAX = 200
_LYRIC_PREVIEW_ROWS = 5000
_LYRIC_PREVIEW_SAMPLES = 20


class LyricFilters(BaseModel):
    exact_lines: list[str] = Field(default_factory=list, max_length=50)
    by_tag_contains: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("exact_lines", "by_tag_contains")
    @classmethod
    def valid_rules(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not _LYRIC_RULE_MIN <= len(value) <= _LYRIC_RULE_MAX for value in normalized):
            raise ValueError(
                f"each lyric rule must contain {_LYRIC_RULE_MIN} to {_LYRIC_RULE_MAX} characters"
            )
        return list(dict.fromkeys(normalized))


_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,40}$")


class UrlResolver(BaseModel):
    """issue 173: one third-party audio URL resolver; ``{id}`` is replaced by the song ID."""

    name: str = Field(min_length=1, max_length=40)
    kind: Literal["json", "redirect"]
    url_template: str = Field(min_length=10, max_length=500)
    headers: dict[str, str] = Field(default_factory=dict, max_length=5)
    json_url_field: str = Field(default="url", min_length=1, max_length=40)
    enabled: bool = True

    @field_validator("url_template")
    @classmethod
    def valid_template(cls, value: str) -> str:
        value = value.strip()
        parts = urlsplit(value.replace("{id}", "0"))
        host = (parts.hostname or "").lower()
        if value.count("{id}") != 1 or "{" in value.replace("{id}", ""):
            raise ValueError("url_template must contain {id} exactly once and no other placeholder")
        if parts.scheme not in {"http", "https"} or parts.username or parts.password or not _HOST.fullmatch(host):
            raise ValueError("url_template must be an http(s) URL on a public DNS name")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return value
        raise ValueError("IP addresses are not allowed in resolver URLs")

    @field_validator("headers")
    @classmethod
    def valid_headers(cls, values: dict[str, str]) -> dict[str, str]:
        for name, value in values.items():
            if not _HEADER_NAME.fullmatch(name) or len(value) > 200 or any(c in value for c in "\r\n"):
                raise ValueError("resolver headers need simple names and single-line values")
        return values


class MusicSourceConfig(BaseModel):
    audio_hosts: list[str] = Field(default_factory=lambda: sorted(_AUDIO_HOSTS), max_length=20)
    artwork_hosts: list[str] = Field(default_factory=lambda: sorted(_ARTWORK_HOSTS), max_length=20)
    upstream_source: str = Field(default="酷我音乐", min_length=1, max_length=80)
    audio_url_ttl_seconds: int = Field(default=_AUDIO_URL_TTL_SECONDS, ge=0, le=600)
    lyric_filters: LyricFilters = Field(default_factory=LyricFilters)
    audio_quality_order: list[AudioQuality] = Field(
        default_factory=lambda: list(DEFAULT_AUDIO_QUALITY_ORDER), min_length=1, max_length=4,
    )
    recommended_min_songs: int = Field(default=_RECOMMENDED_MIN_SONGS, ge=1, le=100000)
    recommended_max_songs: int = Field(default=_RECOMMENDED_MAX_SONGS, ge=1, le=100000)
    # issue 173: only adapters that resolve audio through third parties (Kuwo direct) read this.
    url_resolvers: list[UrlResolver] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def valid_recommended_range(self) -> Self:
        if self.recommended_min_songs > self.recommended_max_songs:
            raise ValueError("recommended_min_songs must not exceed recommended_max_songs")
        return self

    @field_validator("audio_quality_order")
    @classmethod
    def valid_quality_order(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @field_validator("audio_hosts", "artwork_hosts")
    @classmethod
    def valid_hosts(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if any(len(value) > 253 or not _HOST.fullmatch(value) for value in normalized):
            raise ValueError("enter exact public DNS hostnames without a scheme, port or wildcard")
        for value in normalized:
            try:
                ipaddress.ip_address(value)
            except ValueError:
                continue
            raise ValueError("IP addresses are not allowed in upstream host rules")
        return list(dict.fromkeys(normalized))

    @field_validator("upstream_source")
    @classmethod
    def valid_upstream_source(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("upstream source is required")
        return value


class MusicSourceCreate(BaseModel):
    id: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=100)
    adapter: str = Field(min_length=1, max_length=40)
    base_url: str = Field(min_length=1, max_length=500)
    enabled: bool = False
    priority: int = Field(default=100, ge=0, le=10000)
    config: MusicSourceConfig = Field(default_factory=MusicSourceConfig)


class MusicSourcePatch(BaseModel):
    revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    config: MusicSourceConfig | None = None


def _valid_keywords(values: list[str]) -> list[str]:
    normalized = [value.strip() for value in values if value.strip()]
    if any(len(value) > 40 for value in normalized):
        raise ValueError("each recommended keyword must contain at most 40 characters")
    return list(dict.fromkeys(normalized))


class MusicSettingsPatch(BaseModel):
    """issue 181: one of these keywords is searched per hourly pool refresh; empty turns it off."""

    recommended_keywords: list[str] = Field(max_length=100)
    revision: int = Field(ge=1)
    health_enabled: bool | None = None
    health_interval_minutes: int | None = Field(default=None, ge=1, le=10080)

    @field_validator("recommended_keywords")
    @classmethod
    def valid_recommended_keywords(cls, values: list[str]) -> list[str]:
        return _valid_keywords(values)


class MusicSongMetadataWrite(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    artists: list[str] = Field(min_length=1, max_length=10)
    album: str | None = Field(default=None, max_length=500)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    artwork_url: str | None = Field(default=None, max_length=2000)
    artist_avatar_url: str | None = Field(default=None, max_length=2000)
    lyrics_lrc: str | None = Field(default=None, max_length=200_000)
    qualities: list[Literal["mp3", "m4a", "ogg", "flac"]] = Field(default_factory=list, max_length=4)
    mode: Literal["auto", "manual"] = "auto"


class MusicSongLyricsWrite(BaseModel):
    lyrics_lrc: str = Field(min_length=1, max_length=200_000)
    mode: Literal["auto", "manual"] = "auto"
    provider: str | None = Field(default=None, max_length=40)


def _image_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    parts = urlsplit(value)
    if value.startswith("/v2/music/artwork?url=") and parts.path == "/v2/music/artwork":
        return value
    if value.startswith("/v2/music/images/") and parts.path == value:
        return value
    if parts.scheme == "https" and parts.hostname and not parts.username and not parts.password:
        try:
            ipaddress.ip_address(parts.hostname)
        except ValueError:
            return value
    raise V2DomainError("image URL must be HTTPS or a music artwork proxy path")


def serialize_metadata(
    item: MusicSongMetadata, images: MusicImageArchive | None = None, filters: dict | None = None,
) -> dict:
    ingested_at = item.ingested_at.replace(tzinfo=item.ingested_at.tzinfo or UTC)
    updated_at = item.updated_at.replace(tzinfo=item.updated_at.tzinfo or UTC)
    return {
        "id": f"{item.source_id}:{item.song_id}",
        "source_id": item.source_id,
        "external_id": item.song_id,
        "title": item.title,
        "artists": item.artists,
        "album": item.album,
        "duration_ms": item.duration_ms,
        "artwork_url": images.delivery_url(item.artwork_url) if images else item.artwork_url,
        "artist_avatar_url": images.delivery_url(item.artist_avatar_url) if images else item.artist_avatar_url,
        "artwork_storage": images.storage(item.artwork_url) if images else "external",
        "artist_avatar_storage": images.storage(item.artist_avatar_url) if images else "external",
        "lyrics_lrc": (
            clean_lyrics(item.lyrics_lrc, filters)
            if item.lyrics_lrc and filters and item.mode != "manual" and item.lyrics_mode != "manual"
            else item.lyrics_lrc or None
        ),
        "lyrics_mode": item.lyrics_mode,
        "lyrics_provider": item.lyrics_provider,
        "qualities": item.qualities,
        "mode": item.mode,
        "ingested_at": ingested_at.isoformat(),
        "updated_at": updated_at.isoformat(),
    }


def validate_base_url(value: str) -> str:
    parts = urlsplit(value)
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as error:
        raise V2DomainError("base_url must be a public HTTP(S) origin") from error
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 80, 443}
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or host == "localhost"
        or not re.fullmatch(r"[a-z0-9.-]+", host)
    ):
        raise V2DomainError("base_url must be a public HTTP(S) origin")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise V2DomainError("base_url must use a DNS name, not an IP address")
    return f"{parts.scheme}://{host}{':' + str(port) if port else ''}/"


def serialize_source(source: MusicSource) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "adapter": source.adapter,
        "base_url": source.base_url,
        "config": source.config,
        "enabled": source.enabled,
        "health_failed": source.health_failed,
        "health_checked_at": source.health_checked_at.isoformat() if source.health_checked_at else None,
        "health_error": source.health_error,
        "health_latency_ms": source.health_latency_ms,
        "priority": source.priority,
        "revision": source.revision,
    }


class MusicSourceService:
    def __init__(self, sessions: sessionmaker[Session], settings: Settings) -> None:
        self.sessions = sessions
        self.images = MusicImageArchive(settings)
        self._audio_urls: dict[tuple[str, int, str, str], tuple[float, str]] = {}
        self._audio_urls_guard = threading.Lock()
        self._audio_url_locks = [threading.Lock() for _ in range(32)]
        self._recommended_pools: dict[str, tuple[float, list[dict]]] = {}
        self._recommended_lock = threading.Lock()
        self._recommended_total_pages: dict[str, int] = {}
        # issue 171: playlist id -> wall-clock time it was last returned, oldest first.
        self._recommended_seen: OrderedDict[str, float] = OrderedDict()
        self._recommended_seen_lock = threading.Lock()
        # issue 173: source id -> monotonic time until which it is skipped in favour of a standby.
        self._down_until: dict[str, float] = {}

    def adapters(self) -> dict:
        return {"items": adapter_kinds()}

    def list_sources(self, *, enabled_only: bool = False) -> dict:
        with self.sessions() as session:
            query = select(MusicSource).order_by(MusicSource.priority, MusicSource.id)
            if enabled_only:
                query = query.where(MusicSource.enabled.is_(True))
            return {"items": [serialize_source(item) for item in session.scalars(query)]}

    def list_metadata(
        self, *, page: int = 1, page_size: int = 25,
        source_id: str | None = None, query: str | None = None,
    ) -> dict:
        filters = []
        if source_id:
            filters.append(MusicSongMetadata.source_id == source_id)
        if query:
            pattern = f"%{query.strip()}%"
            artist_names = func.json_each(MusicSongMetadata.artists).table_valued("value")
            filters.append(or_(
                MusicSongMetadata.title.ilike(pattern),
                MusicSongMetadata.album.ilike(pattern),
                MusicSongMetadata.song_id.ilike(pattern),
                select(1).select_from(artist_names)
                .where(artist_names.c.value.ilike(pattern)).exists(),
            ))
        with self.sessions() as session:
            total = session.scalar(
                select(func.count()).select_from(MusicSongMetadata).where(*filters)
            ) or 0
            rows = session.scalars(
                select(MusicSongMetadata).where(*filters)
                .order_by(MusicSongMetadata.ingested_at.desc(), MusicSongMetadata.source_id,
                          MusicSongMetadata.song_id)
                .offset((page - 1) * page_size).limit(page_size)
            )
            filters = {
                source.id: source.config.get("lyric_filters", {})
                for source in session.scalars(select(MusicSource))
            }
            items = []
            for row in rows:
                item = serialize_metadata(row, self.images, filters.get(row.source_id))
                item["has_lyrics"] = bool(item.pop("lyrics_lrc"))
                items.append(item)
            return {"items": items, "total": total, "page": page, "page_size": page_size}

    def admin_metadata(self, source_id: str, song_id: str) -> dict:
        with self.sessions() as session:
            item = session.get(MusicSongMetadata, (source_id, self._id(song_id)))
            if item is None:
                raise V2NotFound("music metadata has not been collected")
            return serialize_metadata(item, self.images, self._lyric_filters(session, source_id))

    def preview_lyric_filters(self, source_id: str, filters: LyricFilters) -> dict:
        """Report what draft rules would remove from stored automatic lyrics; writes nothing."""
        rules = filters.model_dump()
        with self.sessions() as session:
            if session.get(MusicSource, source_id) is None:
                raise V2NotFound("music source not found")
            rows = session.scalars(
                select(MusicSongMetadata).where(
                    MusicSongMetadata.source_id == source_id,
                    MusicSongMetadata.mode != "manual",
                    MusicSongMetadata.lyrics_lrc.is_not(None),
                ).limit(_LYRIC_PREVIEW_ROWS)
            )
            scanned = affected = removed_lines = 0
            samples: list[dict] = []
            for row in rows:
                scanned += 1
                removed = split_lyrics(row.lyrics_lrc, rules)[1]
                if not removed:
                    continue
                affected += 1
                removed_lines += len(removed)
                for line in removed:
                    if len(samples) < _LYRIC_PREVIEW_SAMPLES:
                        samples.append({"song_id": row.song_id, "title": row.title, "line": line})
        return {
            "scanned_songs": scanned, "affected_songs": affected,
            "removed_lines": removed_lines, "samples": samples,
        }

    def create(self, data: MusicSourceCreate) -> dict:
        if not _SOURCE_ID.fullmatch(data.id):
            raise V2DomainError("source id must contain lowercase letters, digits, '-' or '_'")
        if data.adapter not in ADAPTERS:
            raise V2DomainError("unsupported music adapter")
        # Fields the administrator did not send come from the adapter (filter text, Kuwo hosts and
        # resolvers), then from the model defaults; sent fields always win.
        defaults = getattr(ADAPTERS[data.adapter], "default_config", {})
        explicit = data.config.model_dump(include=data.config.model_fields_set)
        config = MusicSourceConfig.model_validate({**defaults, **explicit}).model_dump()
        source = MusicSource(
            id=data.id, name=data.name.strip(), adapter=data.adapter,
            base_url=validate_base_url(data.base_url), enabled=data.enabled,
            priority=data.priority, config=config, revision=1,
        )
        if not source.name:
            raise V2DomainError("source name is required")
        with self.sessions() as session:
            session.add(source)
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise V2Conflict("music source id already exists") from error
            return serialize_source(source)

    def patch(self, source_id: str, data: MusicSourcePatch) -> dict:
        with self.sessions() as session:
            source = session.get(MusicSource, source_id)
            if source is None:
                raise V2NotFound("music source not found")
            if source.revision != data.revision:
                raise V2Conflict("music source was changed by another administrator")
            fields = data.model_fields_set
            changes: dict = {}
            if "name" in fields:
                name = (data.name or "").strip()
                if not name:
                    raise V2DomainError("source name is required")
                changes["name"] = name
            if "base_url" in fields:
                changes["base_url"] = validate_base_url(data.base_url or "")
            if "enabled" in fields:
                if data.enabled is None:
                    raise V2DomainError("enabled must be a boolean")
                changes["enabled"] = data.enabled
            if "priority" in fields:
                if data.priority is None:
                    raise V2DomainError("priority must be an integer")
                changes["priority"] = data.priority
            if "config" in fields:
                if data.config is None:
                    raise V2DomainError("source config is required")
                current = MusicSourceConfig.model_validate(source.config).model_dump()
                submitted = data.config.model_dump()
                changes["config"] = {
                    **current,
                    **{key: submitted[key] for key in data.config.model_fields_set},
                }
            if any(key in changes and changes[key] != getattr(source, key) for key in ("enabled", "config", "base_url")):
                changes.update(health_failed=False, health_checked_at=None, health_error=None, health_latency_ms=None)
            changes["revision"] = data.revision + 1
            changes["updated_at"] = utc_now()
            result = session.execute(
                update(MusicSource)
                .where(MusicSource.id == source_id, MusicSource.revision == data.revision)
                .values(**changes)
            )
            if result.rowcount != 1:
                session.rollback()
                raise V2Conflict("music source was changed by another administrator")
            session.commit()
            session.refresh(source)
            log.info(
                "music source %s updated to revision %s; fields: %s",
                source_id, source.revision, sorted(fields - {"revision"}),
            )
            return serialize_source(source)

    def settings(self) -> dict:
        with self.sessions() as session:
            row = session.get(MusicSettings, "global")
        if row is None:
            return {"recommended_keywords": list(_RECOMMENDED_KEYWORDS), "health_enabled": True, "health_interval_minutes": 60, "revision": 1, "updated_at": None}
        return {
            "recommended_keywords": list(row.recommended_keywords),
            "health_enabled": row.health_enabled,
            "health_interval_minutes": row.health_interval_minutes,
            "revision": row.revision,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def patch_settings(self, data: MusicSettingsPatch) -> dict:
        with self.sessions() as session:
            row = session.get(MusicSettings, "global")
            current = row.revision if row is not None else 1
            if data.revision != current:
                raise V2Conflict("music settings were changed by another administrator")
            if row is None:
                session.add(MusicSettings(
                    id="global", recommended_keywords=data.recommended_keywords, revision=current + 1,
                    health_enabled=data.health_enabled if data.health_enabled is not None else True,
                    health_interval_minutes=data.health_interval_minutes or 60,
                ))
            else:
                result = session.execute(
                    update(MusicSettings)
                    .where(MusicSettings.id == "global", MusicSettings.revision == data.revision)
                    .values(recommended_keywords=data.recommended_keywords, revision=current + 1,
                            health_enabled=data.health_enabled if data.health_enabled is not None else row.health_enabled,
                            health_interval_minutes=data.health_interval_minutes or row.health_interval_minutes,
                            updated_at=utc_now())
                )
                if result.rowcount != 1:
                    session.rollback()
                    raise V2Conflict("music settings were changed by another administrator")
            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                raise V2Conflict("music settings were changed by another administrator") from error
        log.info("music settings updated to revision %s; keywords=%s", current + 1, len(data.recommended_keywords))
        return self.settings()

    def _active(self, source_id: str) -> MusicSource:
        with self.sessions() as session:
            source = session.get(MusicSource, source_id)
            if source is None or not source.enabled:
                raise V2NotFound("music source is unavailable")
            return source

    def _adapter(self, source: MusicSource):
        adapter = ADAPTERS.get(source.adapter)
        if adapter is None:
            raise V2Unavailable("configured music adapter is not installed")
        return adapter.configured(source.config) if hasattr(adapter, "configured") else adapter

    def _catalog(self, source: MusicSource) -> str | None:
        return getattr(self._adapter(source), "catalog", None)

    def _is_down(self, source_id: str) -> bool:
        return self._down_until.get(source_id, 0.0) > monotonic()

    def _mark_down(self, source_id: str) -> None:
        self._down_until[source_id] = monotonic() + _SOURCE_DOWN_SECONDS

    def _yields_to_standby(self, source_id: str, catalog: str | None, catalogs: dict[str, str | None]) -> bool:
        """True while ``source_id`` is marked down and a healthy source of its catalog is enabled."""
        return catalog is not None and self._is_down(source_id) and any(
            other != source_id and other_catalog == catalog and not self._is_down(other)
            for other, other_catalog in catalogs.items()
        )

    def _with_standby(self, source: MusicSource, operation):
        """Run ``operation(source, adapter)``, then on same-catalog standbys (issue 173)."""
        catalog = self._catalog(source)
        standbys = [
            other for other in self._enabled_sources()
            if other.id != source.id and catalog is not None and self._catalog(other) == catalog
        ] if catalog is not None else []
        if not standbys:
            return source, operation(source, self._adapter(source))
        order = [*standbys, source] if self._is_down(source.id) or source.health_failed else [source, *standbys]
        last_error: Exception | None = None
        for candidate in order:
            try:
                value = operation(candidate, self._adapter(candidate))
            except (V2Unavailable, V2NotFound) as error:
                if isinstance(error, V2Unavailable):
                    self._mark_down(candidate.id)
                log.warning(
                    "Music source failed, trying standby: source=%s tried=%s error=%s",
                    source.id, candidate.id, error,
                )
                last_error = error
                continue
            if candidate.id != source.id:
                log.info("Served by standby source: source=%s standby=%s", source.id, candidate.id)
            return candidate, value
        raise last_error  # type: ignore[misc]

    def artwork_hosts(self, source_id: str | None = None) -> frozenset[str]:
        sources = [self._active(source_id)] if source_id else self._enabled_sources()
        return frozenset(
            host for source in sources
            for host in MusicSourceConfig.model_validate(source.config).artwork_hosts
        )

    @staticmethod
    def _lyric_filters(session: Session, source_id: str) -> dict:
        source = session.get(MusicSource, source_id)
        return source.config.get("lyric_filters", {}) if source else {}

    def _id(self, value: str) -> str:
        if not _ID.fullmatch(value):
            raise V2DomainError("invalid upstream item id")
        return value

    def metadata(self, source_id: str, song_id: str) -> dict:
        self._active(source_id)
        with self.sessions() as session:
            item = session.get(MusicSongMetadata, (source_id, self._id(song_id)))
            if item is None:
                raise V2NotFound("music metadata has not been collected")
            return serialize_metadata(item, self.images, self._lyric_filters(session, source_id))

    def put_metadata(self, source_id: str, song_id: str, data: MusicSongMetadataWrite) -> dict:
        source = self._active(source_id)
        song_id = self._id(song_id)
        title = data.title.strip()
        artists = [name.strip() for name in data.artists]
        if not title or any(not name or len(name) > 200 for name in artists):
            raise V2DomainError("title and artist names must be nonempty")
        hosts = self.artwork_hosts(source.id)

        def image_value(field: str, value: str | None) -> str | None | object:
            value = (value or "").strip() or None
            if value is not None:
                parts = urlsplit(value)
                if parts.scheme and parts.path == "/v2/music/artwork" and parts.query:
                    value = f"{parts.path}?{parts.query}"  # issue 176: our own proxy URL with a host
                else:
                    value = artwork_proxy(value, source.id, hosts) or value
            try:
                checked = _image_url(self.images.normalize(value))
            except (MusicImageArchiveError, V2DomainError) as error:
                log.warning(
                    "Music image dropped from metadata write: source=%s song=%s field=%s error=%s",
                    source.id, song_id, field, error,
                )
                return _KEEP
            try:
                return self.images.archive(checked, hosts=hosts)
            except MusicImageArchiveError:
                # Keep the display URL; administrators can retry archival later.
                return checked

        changes = {
            "title": title,
            "artists": artists,
            "album": data.album.strip() or None if data.album is not None else None,
            "duration_ms": data.duration_ms,
            "artwork_url": image_value("artwork_url", data.artwork_url),
            "artist_avatar_url": image_value("artist_avatar_url", data.artist_avatar_url),
            "lyrics_lrc": (
                clean_lyrics(data.lyrics_lrc, source.config.get("lyric_filters", {}))
                if data.lyrics_lrc and data.mode != "manual"
                else data.lyrics_lrc or None
            ),
            "qualities": list(dict.fromkeys(data.qualities)),
            "mode": data.mode,
        }
        if changes["lyrics_lrc"]:
            changes.update(
                lyrics_mode=data.mode, lyrics_provider="edit" if data.mode == "manual" else "source",
                lyrics_updated_at=utc_now(),
            )
        else:
            changes.update(lyrics_mode=None, lyrics_provider=None, lyrics_updated_at=None)
        for key in ("artwork_url", "artist_avatar_url"):
            if changes[key] is _KEEP:
                changes.pop(key)  # issue 176: an unusable image keeps what is stored (None for a new row)
        with self.sessions() as session:
            item = session.get(MusicSongMetadata, (source_id, song_id))
            if item is None:
                item = MusicSongMetadata(source_id=source_id, song_id=song_id, **changes)
                session.add(item)
            elif item.mode != "manual" or data.mode == "manual":
                if data.mode == "auto" and (not changes["lyrics_lrc"] or item.lyrics_mode == "manual"):
                    # issue 166: see the module invariants; keep the stored lyrics and their origin.
                    for key in ("lyrics_lrc", "lyrics_mode", "lyrics_provider", "lyrics_updated_at"):
                        changes.pop(key)
                for key, value in changes.items():
                    setattr(item, key, value)
                item.updated_at = utc_now()
            session.commit()
            return serialize_metadata(item, self.images, source.config.get("lyric_filters", {}))

    def put_lyrics(self, source_id: str, song_id: str, data: MusicSongLyricsWrite) -> dict:
        """issue 166: store fallback (auto) or user-picked (manual) lyrics without touching other fields."""
        source = self._active(source_id)
        song_id = self._id(song_id)
        lyrics = (
            clean_lyrics(data.lyrics_lrc, source.config.get("lyric_filters", {}))
            if data.mode == "auto" else data.lyrics_lrc.strip()
        )
        if not lyrics:
            raise V2DomainError("lyrics must not be blank")
        values = {
            "lyrics_lrc": lyrics, "lyrics_mode": data.mode,
            "lyrics_provider": (data.provider or "").strip() or None,
            "lyrics_updated_at": utc_now(), "updated_at": utc_now(),
        }
        with self.sessions() as session:
            if session.get(MusicSongMetadata, (source_id, song_id)) is None:
                raise V2NotFound("music metadata has not been collected")
            query = update(MusicSongMetadata).where(
                MusicSongMetadata.source_id == source_id, MusicSongMetadata.song_id == song_id,
            )
            if data.mode == "auto":
                query = query.where(or_(
                    MusicSongMetadata.lyrics_lrc.is_(None), MusicSongMetadata.lyrics_lrc == "",
                ))
            written = session.execute(query.values(**values)).rowcount == 1
            session.commit()
            log.info(
                "public song lyrics %s: source=%s song=%s mode=%s provider=%s",
                "stored" if written else "kept", source_id, song_id, data.mode, values["lyrics_provider"],
            )
            item = session.get(MusicSongMetadata, (source_id, song_id), populate_existing=True)
            return {
                "written": written,
                **serialize_metadata(item, self.images, source.config.get("lyric_filters", {})),
            }

    def archive_metadata_images(self, source_id: str, song_id: str) -> dict:
        with self.sessions() as session:
            item = session.get(MusicSongMetadata, (source_id, self._id(song_id)))
            if item is None:
                raise V2NotFound("music metadata has not been collected")
            for field in ("artwork_url", "artist_avatar_url"):
                original = getattr(item, field)
                try:
                    archived = self.images.archive(original, hosts=self.artwork_hosts(source_id))
                except MusicImageArchiveError:
                    continue
                if archived != original:
                    setattr(item, field, archived)
                    item.updated_at = utc_now()
            session.commit()
            return serialize_metadata(item, self.images, self._lyric_filters(session, source_id))

    def _overlay(self, source_id: str, page: dict) -> dict:
        ids = [item.get("external_id") for item in page.get("items", [])]
        if not ids:
            return page
        with self.sessions() as session:
            stored = {
                item.song_id: serialize_metadata(item, self.images)
                for item in session.scalars(
                    select(MusicSongMetadata).where(
                        MusicSongMetadata.source_id == source_id,
                        MusicSongMetadata.song_id.in_(ids),
                    )
                )
            }
        merged = []
        for item in page["items"]:
            overlay = stored.get(item.get("external_id"))
            if overlay:
                # Blank stored fields must not hide upstream values. A manually cleared album therefore
                # still lists the upstream album; the API cannot tell "cleared" from "not provided".
                overlay = {
                    key: value for key, value in overlay.items()
                    if key != "lyrics_lrc" and value not in (None, "", [])
                }
            merged.append({**item, **overlay} if overlay else item)
        return {**page, "items": merged}

    def search(self, query: str, page: int, source_id: str | None = None) -> dict:
        if not query.strip() or len(query) > 100:
            raise V2DomainError("search query must contain 1 to 100 characters")
        enabled = [] if source_id else self._enabled_sources()
        sources = [self._active(source_id)] if source_id else [s for s in enabled if not s.health_failed]
        results = []
        unavailable = [s.id for s in enabled if s.health_failed]
        served: set[str] = set()
        catalogs = {source.id: None if source_id else self._catalog(source) for source in sources}
        for source in sources:
            catalog = catalogs[source.id]
            if catalog is not None and catalog in served:
                continue  # issue 173: a healthier source of the same catalog already answered
            if self._yields_to_standby(source.id, catalog, catalogs):
                unavailable.append(source.id)
                continue
            try:
                result = self._overlay(
                    source.id,
                    self._adapter(source).search(source.base_url, source.id, query.strip(), page),
                )
                results.append({"source_id": source.id, **result})
                if catalog is not None:
                    served.add(catalog)
            except (V2Unavailable, V2DomainError) as error:
                if source_id:
                    raise V2Unavailable("configured music source is unavailable") from error
                if isinstance(error, V2Unavailable):
                    self._mark_down(source.id)
                log.warning("Music search failed: source=%s page=%s error=%s", source.id, page, error)
                unavailable.append(source.id)
        if (sources or unavailable) and not results:
            raise V2Unavailable("all configured music sources are unavailable")
        return {"sources": results, "unavailable_sources": unavailable}

    def search_playlists(self, query: str, page: int, source_id: str | None = None) -> dict:
        """issue 167: playlists by name, in the same multi-source shell as song search."""
        query = query.strip()
        if not query or len(query) > 100:
            raise V2DomainError("search query must contain 1 to 100 characters")
        enabled = [] if source_id else self._enabled_sources()
        sources = [self._active(source_id)] if source_id else [s for s in enabled if not s.health_failed]
        results = []
        unavailable = [s.id for s in enabled if s.health_failed]
        served: set[str] = set()
        catalogs = {source.id: None if source_id else self._catalog(source) for source in sources}
        for source in sources:
            adapter = self._adapter(source)
            if not hasattr(adapter, "search_playlists"):
                continue
            catalog = catalogs[source.id]
            if catalog is not None and catalog in served:
                continue  # issue 174: one source per catalog, as in song search
            if self._yields_to_standby(source.id, catalog, catalogs):
                unavailable.append(source.id)
                continue
            try:
                result = adapter.search_playlists(source.base_url, source.id, query, page)
                results.append({"source_id": source.id, **result})
                if catalog is not None:
                    served.add(catalog)
            except (V2Unavailable, V2DomainError) as error:
                if isinstance(error, V2Unavailable) and not source_id:
                    self._mark_down(source.id)
                log.warning("Playlist search failed: source=%s page=%s error=%s", source.id, page, error)
                if source_id:
                    raise V2Unavailable("configured music source is unavailable") from error
                unavailable.append(source.id)
        if not results and unavailable:
            raise V2Unavailable("all configured music sources are unavailable")
        return {"sources": results, "unavailable_sources": unavailable}

    def _enabled_sources(self) -> list[MusicSource]:
        with self.sessions() as session:
            return list(session.scalars(
                select(MusicSource).where(MusicSource.enabled.is_(True))
                .order_by(MusicSource.health_failed, func.coalesce(MusicSource.health_latency_ms, 2147483647),
                          MusicSource.priority, MusicSource.id)
            ))

    def recommended_albums(self, limit: int) -> dict:
        candidates: dict[str, dict] = {}
        browsing = [
            (source, self._adapter(source)) for source in self._enabled_sources()
            if getattr(self._adapter(source), "browses_catalog", True)
        ]
        catalogs = {source.id: getattr(adapter, "catalog", None) for source, adapter in browsing}
        served: set[str] = set()
        for source, _adapter in browsing:
            catalog = catalogs[source.id]
            if catalog is not None and catalog in served:
                continue  # issue 174: a source of this catalog already has a pool
            if self._yields_to_standby(source.id, catalog, catalogs):
                cached = self._recommended_pools.get(source.id)
                pool = cached[1] if cached else []  # keep the last pool, do not wait on a down source
            else:
                pool = self._recommended_pool(source)
            if pool and catalog is not None:
                served.add(catalog)
            low = source.config.get("recommended_min_songs", _RECOMMENDED_MIN_SONGS)
            high = source.config.get("recommended_max_songs", _RECOMMENDED_MAX_SONGS)
            for item in pool:
                if item["name"].strip() and item["artwork_url"] and low <= item["song_count"] <= high:
                    candidates.setdefault(item["id"], item)
        return {"items": self._pick_unseen(list(candidates.values()), limit)}

    def _pick_unseen(self, candidates: list[dict], limit: int) -> list[dict]:
        """issue 171: prefer playlists not returned in the last 72 hours, then the least recent."""
        count = min(limit, len(candidates))
        now = time.time()
        with self._recommended_seen_lock:
            seen = self._recommended_seen
            while seen and next(iter(seen.values())) < now - _RECOMMENDED_SEEN_SECONDS:
                seen.popitem(last=False)
            fresh = [item for item in candidates if item["id"] not in seen]
            picked = random.sample(fresh, min(count, len(fresh)))
            if len(picked) < count:
                stale = sorted((item for item in candidates if item["id"] in seen), key=lambda item: seen[item["id"]])
                picked += stale[:count - len(picked)]
            for item in picked:
                seen.pop(item["id"], None)
                seen[item["id"]] = now
            while len(seen) > _RECOMMENDED_SEEN_MAX:
                seen.popitem(last=False)
        return picked

    def _recommended_pages(self, source_id: str, exclude: tuple[int, ...] = ()) -> list[int]:
        """Pages of the hot list for one pool refresh; page 1 only while the page count is unknown."""
        total = self._recommended_total_pages.get(source_id)
        if total is None:
            return [1]
        last = max(1, total - 1) if total > 1 else 1
        front = [page for page in range(1, min(_RECOMMENDED_FRONT_LAST_PAGE, last) + 1) if page not in exclude]
        pages = random.sample(front, min(_RECOMMENDED_FRONT_PAGES, len(front)))
        rest = [page for page in range(1, last + 1) if page not in pages and page not in exclude]
        pages += random.sample(rest, min(_RECOMMENDED_ANY_PAGES, len(rest)))
        return pages

    def _recommended_pool(self, source: MusicSource) -> list[dict]:
        cached = self._recommended_pools.get(source.id)
        if cached and monotonic() < cached[0]:
            return cached[1]
        with self._recommended_lock:
            cached = self._recommended_pools.get(source.id)
            if cached and monotonic() < cached[0]:
                return cached[1]
            adapter = self._adapter(source)
            items: dict[str, dict] = {}
            pages = self._recommended_pages(source.id)
            first_build = pages == [1]
            for page in pages:
                try:
                    result = adapter.playlists(source.base_url, source.id, page)
                except (V2Unavailable, V2DomainError) as error:
                    log.warning("Recommended playlists failed: source=%s page=%s error=%s", source.id, page, error)
                    if isinstance(error, V2Unavailable):
                        self._mark_down(source.id)
                        break
                    continue
                items.update((item["id"], item) for item in result["items"])
                if result.get("total_pages"):
                    self._recommended_total_pages[source.id] = int(result["total_pages"])
            if first_build and source.id in self._recommended_total_pages:
                # Page 1 told us the size of the list; fill the first pool like every later one.
                for page in self._recommended_pages(source.id, exclude=(1,)):
                    try:
                        result = adapter.playlists(source.base_url, source.id, page)
                    except (V2Unavailable, V2DomainError) as error:
                        log.warning("Recommended playlists failed: source=%s page=%s error=%s", source.id, page, error)
                        if isinstance(error, V2Unavailable):
                            self._mark_down(source.id)
                            break
                        continue
                    items.update((item["id"], item) for item in result["items"])
            keywords = self.settings()["recommended_keywords"]
            if keywords and hasattr(adapter, "search_playlists") and not self._is_down(source.id):
                keyword = keywords[int(time.time() // 3600) % len(keywords)]
                try:
                    result = adapter.search_playlists(source.base_url, source.id, keyword, 1)
                    items.update((item["id"], item) for item in result["items"])
                except (V2Unavailable, V2DomainError) as error:
                    log.warning("Recommended keyword playlists failed: source=%s error=%s", source.id, error)
            log.info("Recommended pool refreshed: source=%s pages=%s playlists=%s", source.id, pages, len(items))
            if items:
                pool = (monotonic() + _RECOMMENDED_POOL_TTL_SECONDS, list(items.values()))
            else:
                pool = (monotonic() + _RECOMMENDED_RETRY_SECONDS, cached[1] if cached else [])
            self._recommended_pools[source.id] = pool
            return pool[1]

    def recommend(self, source_id: str, page: int) -> dict:
        source = self._active(source_id)
        return self._overlay(source.id, self._adapter(source).recommend(source.base_url, source.id, page))

    def playlists(self, source_id: str, page: int) -> dict:
        source = self._active(source_id)
        return self._adapter(source).playlists(source.base_url, source.id, page)

    def playlist(self, source_id: str, playlist_id: str, page: int) -> dict:
        source = self._active(source_id)
        playlist_id = self._id(playlist_id)
        serving, result = self._with_standby(
            source, lambda serving, adapter: adapter.playlist(serving.base_url, serving.id, playlist_id, page),
        )
        return self._overlay(serving.id, {**result, "id": f"{source.id}:{playlist_id}"})

    def song(self, source_id: str, song_id: str, refresh: bool = False) -> dict:
        source = self._active(source_id)
        if not refresh:
            try:
                return self.metadata(source_id, song_id)
            except V2NotFound:
                pass
        song_id = self._id(song_id)
        _, result = self._with_standby(
            source, lambda serving, adapter: adapter.song(serving.base_url, serving.id, song_id),
        )
        return {**result, "id": f"{source.id}:{song_id}", "source_id": source.id}

    def lyrics(self, source_id: str, song_id: str, refresh: bool = False) -> dict:
        source = self._active(source_id)
        if not refresh:
            try:
                cached = self.metadata(source_id, song_id)
                if cached["lyrics_lrc"]:
                    return {"id": cached["id"], "source_id": source_id, "lrc": cached["lyrics_lrc"], "mode": cached["mode"]}
            except V2NotFound:
                pass
        song_id = self._id(song_id)
        serving, result = self._with_standby(
            source, lambda serving, adapter: adapter.lyrics(serving.base_url, serving.id, song_id),
        )
        return {
            **result, "id": f"{source.id}:{song_id}", "source_id": source.id,
            "lrc": clean_lyrics(result["lrc"], serving.config.get("lyric_filters", {})),
        }

    def audio_url(self, source_id: str, song_id: str, quality: str) -> str:
        if quality != AUTO_QUALITY and quality not in AUDIO_QUALITIES:
            raise V2DomainError("unsupported audio quality")
        source = self._active(source_id)
        song_id = self._id(song_id)
        key = (source.id, source.revision, song_id, quality)
        with self._audio_urls_guard:
            cached = self._audio_urls.get(key)
        if cached and cached[0] > monotonic():
            return cached[1]
        # Per-key single flight prevents concurrent seeks from resolving the same song repeatedly.
        with self._audio_url_locks[hash(key) % len(self._audio_url_locks)]:
            with self._audio_urls_guard:
                cached = self._audio_urls.get(key)
            if cached and cached[0] > monotonic():
                return cached[1]
            _, url = self._with_standby(
                source, lambda serving, adapter: adapter.audio_url(serving.base_url, song_id, quality),
            )
            with self._audio_urls_guard:
                ttl = MusicSourceConfig.model_validate(source.config).audio_url_ttl_seconds
                self._audio_urls[key] = (monotonic() + ttl, url)
                if len(self._audio_urls) > 1000:
                    now = monotonic()
                    self._audio_urls = {
                        item_key: value for item_key, value in self._audio_urls.items()
                        if value[0] > now
                    }
            return url

    def invalidate_audio_url(
        self, source_id: str, song_id: str, quality: str, failed_url: str,
    ) -> None:
        source = self._active(source_id)
        key = (source.id, source.revision, self._id(song_id), quality)
        with self._audio_urls_guard:
            if (cached := self._audio_urls.get(key)) and cached[1] == failed_url:
                self._audio_urls.pop(key, None)
