"""issue 166: lyrics adopted for local (DEVICE) songs, shared across devices and users.

Invariants
----------
* Identity is the normalized title plus normalized artist (``library_search.normalize``: NFKC,
  casefold, letters and digits only) and a duration within ``DURATION_TOLERANCE_MS``. The user
  decided that two local files with the same title, artist and length are the same song, so a
  live or cover version with another artist or length gets its own record.
* ``auto`` never replaces stored lyrics: it only creates a record when no matching record exists.
  ``manual`` (the user pressed "use") always replaces the matching record, including another
  user's manual choice; the user decided that the latest manual choice wins for everyone.
* A request without an artist is rejected. Untagged files would otherwise share lyrics by title
  alone.

Why this way
------------
Local audio never leaves the phone and MediaStore IDs differ per device, so the only identity
that works across devices is the song's own tags.

What breaks silently if changed
-------------------------------
* Letting ``auto`` update an existing record lets a weaker automatic match replace a lyric a user
  picked by hand, on every phone at once.
* Matching without the duration check shares a 3-minute single's lyrics with a 7-minute live take.

Accepted residuals
------------------
* Two devices may insert automatic records for the same song at the same moment. Both rows match;
  lookups return the closest duration, then the newest, so the duplicate is harmless and a later
  manual choice updates the one that lookups return.
* Records with a missing duration match any duration of the same title and artist, after records
  with a known close duration.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from rhythm_metadata_api.application.library_search import normalize
from rhythm_metadata_api.domain.v2.errors import V2DomainError, V2NotFound
from rhythm_metadata_api.infrastructure.db.models import DeviceSongLyrics, utc_now

log = logging.getLogger(__name__)

DURATION_TOLERANCE_MS = 3000
_KEY_MAX = 200


class DeviceSongLyricsWrite(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    artist: str = Field(min_length=1, max_length=500)
    album: str | None = Field(default=None, max_length=500)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    lyrics_lrc: str = Field(min_length=1, max_length=200_000)
    mode: Literal["auto", "manual"] = "auto"
    provider: str | None = Field(default=None, max_length=40)


def song_keys(title: str, artist: str) -> tuple[str, str]:
    title_key = normalize(title)[:_KEY_MAX]
    artist_key = normalize(artist)[:_KEY_MAX]
    if not title_key or not artist_key:
        raise V2DomainError("title and artist must contain letters or digits")
    return title_key, artist_key


def serialize(row: DeviceSongLyrics) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "artist": row.artist,
        "album": row.album,
        "duration_ms": row.duration_ms,
        "lyrics_lrc": row.lyrics_lrc,
        "mode": row.mode,
        "provider": row.provider,
        "updated_at": row.updated_at.isoformat(),
    }


def _best(rows: list[DeviceSongLyrics], duration_ms: int | None) -> DeviceSongLyrics | None:
    def rank(row: DeviceSongLyrics) -> tuple[int, int, float]:
        if duration_ms is None or row.duration_ms is None:
            # Unknown lengths match, but after a record whose known length is close.
            return (1, 0, -row.updated_at.timestamp())
        return (0, abs(row.duration_ms - duration_ms), -row.updated_at.timestamp())

    candidates = [
        row for row in rows
        if duration_ms is None or row.duration_ms is None
        or abs(row.duration_ms - duration_ms) <= DURATION_TOLERANCE_MS
    ]
    return min(candidates, key=rank) if candidates else None


class DeviceSongLyricsService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    @staticmethod
    def _match(session: Session, title: str, artist: str, duration_ms: int | None) -> DeviceSongLyrics | None:
        title_key, artist_key = song_keys(title, artist)
        rows = list(session.scalars(
            select(DeviceSongLyrics).where(
                DeviceSongLyrics.title_key == title_key, DeviceSongLyrics.artist_key == artist_key,
            )
        ))
        return _best(rows, duration_ms)

    def find(self, title: str, artist: str, duration_ms: int | None) -> dict:
        with self.sessions() as session:
            row = self._match(session, title, artist, duration_ms)
            if row is None:
                raise V2NotFound("no shared lyrics for this song")
            return serialize(row)

    def save(self, data: DeviceSongLyricsWrite) -> dict:
        title = data.title.strip()
        artist = data.artist.strip()
        lyrics = data.lyrics_lrc.strip()
        if not lyrics:
            raise V2DomainError("lyrics must not be blank")
        provider = (data.provider or "").strip() or None
        album = (data.album or "").strip() or None
        with self.sessions() as session:
            row = self._match(session, title, artist, data.duration_ms)
            if row is not None and data.mode == "auto":
                log.info("device song lyrics kept: id=%s existing_mode=%s", row.id, row.mode)
                return {"written": False, "lyrics": serialize(row)}
            if row is None:
                title_key, artist_key = song_keys(title, artist)
                row = DeviceSongLyrics(
                    title_key=title_key, artist_key=artist_key, title=title, artist=artist,
                    album=album, duration_ms=data.duration_ms, lyrics_lrc=lyrics,
                    mode=data.mode, provider=provider, created_at=utc_now(), updated_at=utc_now(),
                )
                session.add(row)
            else:
                row.title, row.artist = title, artist
                row.album = album or row.album
                row.duration_ms = data.duration_ms if data.duration_ms is not None else row.duration_ms
                row.lyrics_lrc, row.mode, row.provider = lyrics, data.mode, provider
                row.updated_at = utc_now()
            session.commit()
            log.info("device song lyrics stored: id=%s mode=%s provider=%s", row.id, row.mode, row.provider)
            return {"written": True, "lyrics": serialize(row)}
