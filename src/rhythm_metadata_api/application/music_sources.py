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
"""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
from datetime import UTC
from time import monotonic
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator
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
from rhythm_metadata_api.infrastructure.db.models import MusicSongMetadata, MusicSource, utc_now

log = logging.getLogger(__name__)

_SOURCE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
_AUDIO_URL_TTL_SECONDS = 150
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


class MusicSourceConfig(BaseModel):
    audio_hosts: list[str] = Field(default_factory=lambda: sorted(_AUDIO_HOSTS), max_length=20)
    artwork_hosts: list[str] = Field(default_factory=lambda: sorted(_ARTWORK_HOSTS), max_length=20)
    upstream_source: str = Field(default="酷我音乐", min_length=1, max_length=80)
    audio_url_ttl_seconds: int = Field(default=_AUDIO_URL_TTL_SECONDS, ge=0, le=600)
    lyric_filters: LyricFilters = Field(default_factory=LyricFilters)
    audio_quality_order: list[AudioQuality] = Field(
        default_factory=lambda: list(DEFAULT_AUDIO_QUALITY_ORDER), min_length=1, max_length=4,
    )

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
            if item.lyrics_lrc and filters and item.mode != "manual"
            else item.lyrics_lrc or None
        ),
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
        config = data.config.model_dump()
        if "lyric_filters" not in data.config.model_fields_set:
            # Filter text belongs to the adapter; other protocols start without any rules.
            defaults = getattr(ADAPTERS[data.adapter], "default_config", {})
            config["lyric_filters"] = defaults.get("lyric_filters", config["lyric_filters"])
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
        def image_value(value: str | None) -> str | None:
            try:
                normalized = self.images.normalize(value)
            except MusicImageArchiveError as error:
                raise V2DomainError("invalid music image URL") from error
            checked = _image_url(normalized)
            try:
                return self.images.archive(checked, hosts=self.artwork_hosts(source.id))
            except MusicImageArchiveError:
                # Keep the display URL; administrators can retry archival later.
                return checked

        changes = {
            "title": title,
            "artists": artists,
            "album": data.album.strip() or None if data.album is not None else None,
            "duration_ms": data.duration_ms,
            "artwork_url": image_value(data.artwork_url),
            "artist_avatar_url": image_value(data.artist_avatar_url),
            "lyrics_lrc": (
                clean_lyrics(data.lyrics_lrc, source.config.get("lyric_filters", {}))
                if data.lyrics_lrc and data.mode != "manual"
                else data.lyrics_lrc or None
            ),
            "qualities": list(dict.fromkeys(data.qualities)),
            "mode": data.mode,
        }
        with self.sessions() as session:
            item = session.get(MusicSongMetadata, (source_id, song_id))
            if item is None:
                item = MusicSongMetadata(source_id=source_id, song_id=song_id, **changes)
                session.add(item)
            elif item.mode != "manual" or data.mode == "manual":
                for key, value in changes.items():
                    setattr(item, key, value)
                item.updated_at = utc_now()
            session.commit()
            return serialize_metadata(item, self.images, source.config.get("lyric_filters", {}))

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
        sources = (
            [self._active(source_id)] if source_id else
            self._enabled_sources()
        )
        results = []
        unavailable = []
        for source in sources:
            try:
                result = self._overlay(
                    source.id,
                    self._adapter(source).search(source.base_url, source.id, query.strip(), page),
                )
                results.append({"source_id": source.id, **result})
            except (V2Unavailable, V2DomainError) as error:
                if source_id:
                    raise V2Unavailable("configured music source is unavailable") from error
                unavailable.append(source.id)
        if sources and not results:
            raise V2Unavailable("all configured music sources are unavailable")
        return {"sources": results, "unavailable_sources": unavailable}

    def _enabled_sources(self) -> list[MusicSource]:
        with self.sessions() as session:
            return list(session.scalars(
                select(MusicSource).where(MusicSource.enabled.is_(True))
                .order_by(MusicSource.priority, MusicSource.id)
            ))

    def recommend(self, source_id: str, page: int) -> dict:
        source = self._active(source_id)
        return self._overlay(source.id, self._adapter(source).recommend(source.base_url, source.id, page))

    def playlists(self, source_id: str, page: int) -> dict:
        source = self._active(source_id)
        return self._adapter(source).playlists(source.base_url, source.id, page)

    def playlist(self, source_id: str, playlist_id: str, page: int) -> dict:
        source = self._active(source_id)
        result = self._adapter(source).playlist(source.base_url, source.id, self._id(playlist_id), page)
        return self._overlay(source.id, result)

    def song(self, source_id: str, song_id: str, refresh: bool = False) -> dict:
        source = self._active(source_id)
        if not refresh:
            try:
                return self.metadata(source_id, song_id)
            except V2NotFound:
                pass
        return self._adapter(source).song(source.base_url, source.id, self._id(song_id))

    def lyrics(self, source_id: str, song_id: str, refresh: bool = False) -> dict:
        source = self._active(source_id)
        if not refresh:
            try:
                cached = self.metadata(source_id, song_id)
                if cached["lyrics_lrc"]:
                    return {"id": cached["id"], "source_id": source_id, "lrc": cached["lyrics_lrc"], "mode": cached["mode"]}
            except V2NotFound:
                pass
        result = self._adapter(source).lyrics(source.base_url, source.id, self._id(song_id))
        return {**result, "lrc": clean_lyrics(result["lrc"], source.config.get("lyric_filters", {}))}

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
            url = self._adapter(source).audio_url(source.base_url, song_id, quality)
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
