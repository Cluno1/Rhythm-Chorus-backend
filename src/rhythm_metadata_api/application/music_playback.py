"""Immutable, short-lived audio tickets for safe Range playback across source failover."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
import unicodedata
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field
from sqlalchemy import delete

from rhythm_metadata_api.application.catalog_service import ActorContext
from rhythm_metadata_api.application.music_adapters import _opener, validate_public_url
from rhythm_metadata_api.application.music_sources import (
    AUDIO_QUALITIES,
    AUTO_QUALITY,
    MusicSourceService,
)
from rhythm_metadata_api.domain.v2.errors import (
    V2Conflict,
    V2DomainError,
    V2NotFound,
    V2Unavailable,
)
from rhythm_metadata_api.infrastructure.db.models import MusicPlaybackTicket, MusicSource, utc_now

_TICKET_ID = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_TICKET_LIFETIME = timedelta(minutes=10)


class PlaybackPrepare(BaseModel):
    source_id: str = Field(min_length=2, max_length=40)
    song_id: str = Field(min_length=1, max_length=80)
    quality: str = "mp3"
    previous_ticket: str | None = Field(default=None, max_length=64)
    # Search hits may not yet have been written to shared metadata. Hints are used only after
    # exact artist/title/duration matching, and never as a target platform song ID.
    title: str | None = Field(default=None, max_length=500)
    artists: list[str] = Field(default_factory=list, max_length=10)
    album: str | None = Field(default=None, max_length=500)
    duration_ms: int | None = Field(default=None, ge=1000, le=7_200_000)


def _fold(value: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", value).casefold() if char.isalnum())


def _same_recording(original: dict, candidate: dict) -> bool:
    if not original.get("title") or not original.get("artists"):
        return False
    if _fold(original["title"]) != _fold(candidate.get("title") or ""):
        return False
    artists = candidate.get("artists") or []
    if not artists or _fold(original["artists"][0]) != _fold(artists[0]):
        return False
    first, second = original.get("duration_ms"), candidate.get("duration_ms")
    if not isinstance(first, int) or not isinstance(second, int) or abs(first - second) > 4000:
        return False
    first_album, second_album = original.get("album"), candidate.get("album")
    return not (first_album and second_album and _fold(first_album) != _fold(second_album))


class MusicPlaybackService:
    def __init__(self, sources: MusicSourceService):
        self.sources = sources
        settings = sources.settings_config
        secret = settings.public_token_secret or settings.bootstrap_token
        if not secret:
            raise RuntimeError("playback ticket encryption secret is not configured")
        key = base64.urlsafe_b64encode(hashlib.sha256(("music-ticket:" + secret).encode()).digest())
        self.cipher = Fernet(key)

    @staticmethod
    def _owner(actor: ActorContext) -> tuple[str, str | None]:
        return actor.actor_id, actor.device_id

    def _ticket(self, ticket_id: str, actor: ActorContext, *, allow_expired: bool = False) -> MusicPlaybackTicket:
        if not _TICKET_ID.fullmatch(ticket_id):
            raise V2NotFound("playback ticket not found")
        with self.sources.sessions() as session:
            ticket = session.get(MusicPlaybackTicket, ticket_id)
            if ticket is None or (ticket.actor_id, ticket.device_id) != self._owner(actor):
                raise V2NotFound("playback ticket not found")
            if not allow_expired and ticket.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC):
                raise V2Conflict("playback ticket expired; prepare a new audio entity")
            return ticket

    @staticmethod
    def _probe(url: str, source: MusicSource) -> tuple[str, int, str | None]:
        from rhythm_metadata_api.application.music_sources import MusicSourceConfig
        hosts = frozenset(MusicSourceConfig.model_validate(source.config).audio_hosts)
        validate_public_url(url, hosts=hosts)
        try:
            with _opener.open(Request(url, headers={"Range": "bytes=0-0"}), timeout=7) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                match = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if response.status != 206 or not match or (match.group(1), match.group(2)) != ("0", "0"):
                    raise V2Unavailable("audio upstream does not support stable byte ranges")
                if not content_type.startswith(("audio/", "application/octet-stream")):
                    raise V2Unavailable("audio upstream returned non-audio content")
                length = int(match.group(3))
                if length <= 0 or len(response.read(1)) != 1:
                    raise V2Unavailable("audio upstream returned an empty file")
                etag = response.headers.get("ETag")
                if not etag or etag.startswith("W/"):
                    raise V2Unavailable("audio upstream has no strong entity validator")
                return content_type, length, etag
        except (HTTPError, URLError, TimeoutError) as error:
            raise V2Unavailable("audio upstream is unavailable") from error

    def _resolve(self, source: MusicSource, song_id: str, quality: str):
        url = self.sources._adapter(source).audio_url(source.base_url, song_id, quality)
        content_type, length, etag = self._probe(url, source)
        return url, content_type, length, etag

    def _resolve_bounded(self, source: MusicSource, song_id: str, quality: str, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self.sources._search_slots.acquire(blocking=False):
            raise V2Unavailable("audio resolution deadline exceeded")
        try:
            future = self.sources._search_pool.submit(self._resolve, source, song_id, quality)
        except RuntimeError:
            self.sources._search_slots.release()
            raise V2Unavailable("audio resolution worker is unavailable") from None
        future.add_done_callback(lambda _: self.sources._search_slots.release())
        timeout = min(remaining, self.sources._capability(source, "audio_resolve")["timeout_ms"] / 1000)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as error:
            future.cancel()
            raise V2Unavailable("audio resolution timed out") from error

    def _candidate_sources(self, catalog: str, origin: MusicSource, excluded: set[str]):
        candidates = [item for item in self.sources._operation_sources("audio_resolve")
                      if item.id not in excluded and self.sources._catalog(item) == catalog]
        return sorted(candidates, key=lambda item: (
            item.id != origin.id,
            self.sources._capability(item, "audio_resolve")["priority"], item.id,
        ))

    def _metadata(self, request: PlaybackPrepare) -> dict:
        try:
            return self.sources.metadata(request.source_id, request.song_id)
        except V2NotFound:
            return request.model_dump(include={"title", "artists", "album", "duration_ms"})

    def prepare(self, request: PlaybackPrepare, actor: ActorContext) -> dict:
        if request.quality != AUTO_QUALITY and request.quality not in AUDIO_QUALITIES:
            raise V2DomainError("unsupported audio quality")
        origin = self.sources._known(request.source_id)
        song_id = self.sources._id(request.song_id)
        catalog = self.sources._catalog(origin)
        excluded: set[str] = set()
        deadline = time.monotonic() + 12
        if request.previous_ticket:
            previous = self._ticket(request.previous_ticket, actor, allow_expired=True)
            if (previous.origin_source_id, previous.origin_song_id) != (origin.id, song_id):
                raise V2DomainError("previous ticket belongs to a different song")
            excluded.add(previous.serving_source_id)

        selected = None
        for source in self._candidate_sources(catalog, origin, excluded)[:4]:
            try:
                selected = (source, song_id, *self._resolve_bounded(source, song_id, request.quality, deadline))
                break
            except (V2Unavailable, V2NotFound):
                continue

        if selected is None and self.sources.settings().get("cross_platform_playback_enabled", False):
            original = self._metadata(request)
            if original.get("title") and original.get("artists") and original.get("duration_ms"):
                try:
                    results = self.sources.search(f"{original['title']} {original['artists'][0]}", 1)
                except V2Unavailable:
                    results = {"sources": []}
                for group in results["sources"]:
                    searched = self.sources._known(group["source_id"])
                    target_catalog = self.sources._catalog(searched)
                    if target_catalog == catalog:
                        continue
                    for hit in group["items"]:
                        if not _same_recording(original, hit):
                            continue
                        target_id = hit["external_id"]
                        for source in self._candidate_sources(target_catalog, searched, excluded)[:3]:
                            try:
                                candidate = (source, target_id, *self._resolve_bounded(source, target_id, request.quality, deadline))
                            except (V2Unavailable, V2NotFound):
                                continue
                            # A short preview is too small to be a full recording. This is a
                            # conservative filter, not proof that larger files are complete.
                            if candidate[4] < original["duration_ms"] / 1000 * 6000:
                                continue
                            selected = candidate
                            break
                        if selected is not None:
                            break
                    if selected is not None:
                        break

        if selected is None:
            raise V2Unavailable("no enabled source could provide a verified playable audio file")
        serving, serving_id, url, content_type, length, etag = selected
        if etag:
            material = f"{serving.id}:{serving_id}:{request.quality}:{urlsplit(url).path}:{etag}:{length}"
            entity = hashlib.sha256(material.encode()).hexdigest()
        else:
            entity = secrets.token_hex(32)  # no cross-ticket cache reuse without a strong validator
        ticket_id = secrets.token_urlsafe(32)
        expires = utc_now() + _TICKET_LIFETIME
        with self.sources.sessions() as session:
            session.execute(delete(MusicPlaybackTicket).where(MusicPlaybackTicket.expires_at < utc_now()))
            session.add(MusicPlaybackTicket(
                id=ticket_id, actor_id=actor.actor_id, device_id=actor.device_id,
                origin_source_id=origin.id, origin_song_id=song_id,
                serving_source_id=serving.id, serving_song_id=serving_id,
                quality=request.quality, audio_entity_id=entity,
                encrypted_url=self.cipher.encrypt(url.encode()).decode(),
                content_type=content_type, content_length=length, etag=etag,
                expires_at=expires, created_at=utc_now(),
            ))
            session.commit()
        return {
            "playback_ticket": ticket_id,
            "audio_entity_id": entity,
            "stream_url": f"/v2/music/playback/{ticket_id}/stream",
            "origin_id": f"{origin.id}:{song_id}",
            "serving_source_id": serving.id,
            "serving_song_id": serving_id,
            "content_type": content_type,
            "content_length": length,
            "expires_at": expires.isoformat(),
        }

    def resolve(self, ticket_id: str, actor: ActorContext) -> tuple[MusicPlaybackTicket, str]:
        ticket = self._ticket(ticket_id, actor)
        source = self.sources._known(ticket.serving_source_id)
        if not self.sources._allows(source, "audio_resolve"):
            raise V2Conflict("audio source changed; prepare a new audio entity")
        try:
            url = self.cipher.decrypt(ticket.encrypted_url.encode()).decode()
        except (InvalidToken, UnicodeError) as error:
            raise V2Unavailable("playback ticket is invalid") from error
        from rhythm_metadata_api.application.music_sources import MusicSourceConfig
        validate_public_url(url, hosts=frozenset(MusicSourceConfig.model_validate(source.config).audio_hosts))
        return ticket, url
