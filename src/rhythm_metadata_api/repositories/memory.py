from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from rhythm_metadata_api.domain.commands import OverrideCommand, TrackIdentity, TrackMatch


class RevisionConflict(Exception):
    def __init__(self, current_revision: int) -> None:
        super().__init__(f"stale revision; current={current_revision}")
        self.current_revision = current_revision


@dataclass(slots=True)
class StoredTrack:
    id: str
    strong_key: str | None
    weak_key: str
    revision: int = 1
    candidates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def weak_identity(request: TrackIdentity) -> str:
    normalized = "|".join(
        [
            (request.title or "").strip().casefold(),
            (request.artist or "").strip().casefold(),
            (request.album or "").strip().casefold(),
            str(request.duration_ms or 0),
            str(request.file_size or 0),
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class InMemoryTrackRepository:
    """Fast test repository with the same public operations as SQLite."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tracks: dict[str, StoredTrack] = {}

    def match(self, request: TrackIdentity) -> TrackMatch:
        weak_key = weak_identity(request)
        with self._lock:
            for track in self._tracks.values():
                if request.audio_sha256 and track.strong_key == request.audio_sha256:
                    return TrackMatch(
                        id=track.id, revision=track.revision, matched_by="audio_sha256"
                    )
                if not request.audio_sha256 and track.weak_key == weak_key:
                    return TrackMatch(
                        id=track.id, revision=track.revision, matched_by="weak_identity"
                    )

            track = StoredTrack(
                id=str(uuid.uuid4()),
                strong_key=request.audio_sha256,
                weak_key=weak_key,
            )
            self._tracks[track.id] = track
            return TrackMatch(id=track.id, revision=track.revision, matched_by="created")

    def get(self, track_id: str) -> StoredTrack | None:
        with self._lock:
            return self._tracks.get(track_id)

    def apply_overrides(
        self,
        track_id: str,
        expected_revision: int,
        request: OverrideCommand,
    ) -> StoredTrack:
        with self._lock:
            track = self._tracks[track_id]
            if track.revision != expected_revision:
                raise RevisionConflict(track.revision)

            next_revision = track.revision + 1
            for field_name, override in request.fields.items():
                candidates = track.candidates.setdefault(field_name, [])
                candidates[:] = [item for item in candidates if item["source"] != "user_override"]
                candidates.append(
                    {
                        "value": override.value,
                        "source": override.source.value,
                        "content_hash": override.content_hash,
                        "revision": next_revision,
                        "base_revision": expected_revision,
                        "user_edited": True,
                        "pinned": override.pin,
                        "source_ref": override.source_ref,
                    }
                )
            track.revision = next_revision
            return track
