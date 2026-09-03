from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rhythm_metadata_api.domain.commands import (
    CandidateSyncCommand,
    OverrideCommand,
    TrackIdentity,
    TrackMatch,
)
from rhythm_metadata_api.repositories.memory import RevisionConflict, StoredTrack, weak_identity


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqliteTrackRepository:
    """Single-user persistent repository with transactional optimistic locking."""

    def __init__(self, database_path: str) -> None:
        self._lock = threading.RLock()
        if database_path != ":memory:":
            path = Path(database_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            database_path = str(path)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if database_path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ping(self) -> bool:
        with self._lock:
            return self._connection.execute("SELECT 1").fetchone()[0] == 1

    def _migrate(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    id TEXT PRIMARY KEY,
                    strong_key TEXT UNIQUE,
                    weak_key TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tracks_weak_key_idx ON tracks(weak_key);

                CREATE TABLE IF NOT EXISTS metadata_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    field_name TEXT NOT NULL,
                    value_json TEXT,
                    source TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    base_revision INTEGER NOT NULL,
                    user_edited INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    source_ref TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS metadata_track_idx
                    ON metadata_candidates(track_id, field_name);

                CREATE TABLE IF NOT EXISTS artifacts (
                    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    storage_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(track_id, kind)
                );

                CREATE TABLE IF NOT EXISTS change_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_track_idx
                    ON change_events(track_id, id DESC);
                """
            )

    def match(self, request: TrackIdentity) -> TrackMatch:
        weak_key = weak_identity(request)
        with self._lock, self._connection:
            if request.audio_sha256:
                row = self._connection.execute(
                    "SELECT id, revision FROM tracks WHERE strong_key = ?",
                    (request.audio_sha256,),
                ).fetchone()
                matched_by = "audio_sha256"
            else:
                row = self._connection.execute(
                    "SELECT id, revision FROM tracks WHERE weak_key = ? ORDER BY created_at LIMIT 1",
                    (weak_key,),
                ).fetchone()
                matched_by = "weak_identity"
            if row is not None:
                return TrackMatch(id=row["id"], revision=row["revision"], matched_by=matched_by)

            track_id = str(uuid.uuid4())
            created_at = _now()
            self._connection.execute(
                """INSERT INTO tracks(id, strong_key, weak_key, revision, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?)""",
                (track_id, request.audio_sha256, weak_key, created_at, created_at),
            )
            self._append_event(track_id, 1, "track.created", {"matched_by": "created"})
            return TrackMatch(id=track_id, revision=1, matched_by="created")

    def get(self, track_id: str) -> StoredTrack | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, strong_key, weak_key, revision FROM tracks WHERE id = ?",
                (track_id,),
            ).fetchone()
            if row is None:
                return None
            candidates: dict[str, list[dict[str, Any]]] = {}
            candidate_rows = self._connection.execute(
                """SELECT field_name, value_json, source, content_hash, revision,
                          base_revision, user_edited, pinned, source_ref
                   FROM metadata_candidates WHERE track_id = ? ORDER BY id""",
                (track_id,),
            ).fetchall()
            for candidate in candidate_rows:
                candidates.setdefault(candidate["field_name"], []).append(
                    {
                        "value": json.loads(candidate["value_json"]),
                        "source": candidate["source"],
                        "content_hash": candidate["content_hash"],
                        "revision": candidate["revision"],
                        "base_revision": candidate["base_revision"],
                        "user_edited": bool(candidate["user_edited"]),
                        "pinned": bool(candidate["pinned"]),
                        "source_ref": candidate["source_ref"],
                    }
                )
            return StoredTrack(
                id=row["id"],
                strong_key=row["strong_key"],
                weak_key=row["weak_key"],
                revision=row["revision"],
                candidates=candidates,
            )

    def apply_overrides(
        self,
        track_id: str,
        expected_revision: int,
        request: OverrideCommand,
    ) -> StoredTrack:
        with self._lock, self._connection:
            current_revision = self._current_revision(track_id)
            if current_revision != expected_revision:
                raise RevisionConflict(current_revision)
            next_revision = current_revision + 1
            changed_fields: list[str] = []
            for field_name, override in request.fields.items():
                self._connection.execute(
                    """DELETE FROM metadata_candidates
                       WHERE track_id = ? AND field_name = ? AND source = 'user_override'""",
                    (track_id, field_name),
                )
                self._connection.execute(
                    """INSERT INTO metadata_candidates(
                           track_id, field_name, value_json, source, content_hash, revision,
                           base_revision, user_edited, pinned, source_ref, created_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (
                        track_id,
                        field_name,
                        json.dumps(override.value, ensure_ascii=False),
                        override.source.value,
                        override.content_hash,
                        next_revision,
                        expected_revision,
                        int(override.pin),
                        override.source_ref,
                        _now(),
                    ),
                )
                changed_fields.append(field_name)
            self._bump_revision(track_id, next_revision)
            self._append_event(
                track_id,
                next_revision,
                "metadata.overridden",
                {"fields": changed_fields, "base_revision": expected_revision},
            )
        track = self.get(track_id)
        assert track is not None
        return track

    def sync_candidates(
        self,
        track_id: str,
        expected_revision: int,
        request: CandidateSyncCommand,
    ) -> StoredTrack:
        with self._lock, self._connection:
            current_revision = self._current_revision(track_id)
            if current_revision != expected_revision:
                raise RevisionConflict(current_revision)
            next_revision = current_revision + 1
            synced: dict[str, int] = {}
            for field_name, incoming in request.fields.items():
                for candidate in incoming:
                    self._connection.execute(
                        """DELETE FROM metadata_candidates
                           WHERE track_id = ? AND field_name = ? AND source = ?
                             AND ((source_ref IS NULL AND ? IS NULL) OR source_ref = ?)""",
                        (
                            track_id,
                            field_name,
                            candidate.source.value,
                            candidate.source_ref,
                            candidate.source_ref,
                        ),
                    )
                    self._connection.execute(
                        """INSERT INTO metadata_candidates(
                               track_id, field_name, value_json, source, content_hash, revision,
                               base_revision, user_edited, pinned, source_ref, created_at
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            track_id,
                            field_name,
                            json.dumps(candidate.value, ensure_ascii=False),
                            candidate.source.value,
                            candidate.content_hash,
                            next_revision,
                            expected_revision,
                            int(candidate.user_edited),
                            int(candidate.pinned),
                            candidate.source_ref,
                            _now(),
                        ),
                    )
                synced[field_name] = len(incoming)
            self._bump_revision(track_id, next_revision)
            self._append_event(
                track_id,
                next_revision,
                "metadata.candidates_synced",
                {"fields": synced, "base_revision": expected_revision},
            )
        track = self.get(track_id)
        assert track is not None
        return track

    def record_artifact(
        self,
        track_id: str,
        expected_revision: int,
        *,
        kind: str,
        storage_key: str,
        content_hash: str,
        mime_type: str,
        size: int,
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            current_revision = self._current_revision(track_id)
            if current_revision != expected_revision:
                raise RevisionConflict(current_revision)
            next_revision = current_revision + 1
            updated_at = _now()
            self._connection.execute(
                """INSERT INTO artifacts(
                       track_id, kind, storage_key, content_hash, mime_type, size, revision, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(track_id, kind) DO UPDATE SET
                       storage_key=excluded.storage_key,
                       content_hash=excluded.content_hash,
                       mime_type=excluded.mime_type,
                       size=excluded.size,
                       revision=excluded.revision,
                       updated_at=excluded.updated_at""",
                (
                    track_id,
                    kind,
                    storage_key,
                    content_hash,
                    mime_type,
                    size,
                    next_revision,
                    updated_at,
                ),
            )
            self._bump_revision(track_id, next_revision)
            self._append_event(
                track_id,
                next_revision,
                "artifact.stored",
                {"kind": kind, "content_hash": content_hash, "size": size},
            )
            return {
                "track_id": track_id,
                "kind": kind,
                "content_hash": content_hash,
                "mime_type": mime_type,
                "size": size,
                "revision": next_revision,
                "updated_at": updated_at,
                "storage_key": storage_key,
            }

    def get_artifact(self, track_id: str, kind: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """SELECT track_id, kind, storage_key, content_hash, mime_type,
                          size, revision, updated_at
                   FROM artifacts WHERE track_id = ? AND kind = ?""",
                (track_id, kind),
            ).fetchone()
            return dict(row) if row is not None else None

    def history(self, track_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            if self.get(track_id) is None:
                raise KeyError(track_id)
            rows = self._connection.execute(
                """SELECT id, revision, operation, payload_json, created_at
                   FROM change_events WHERE track_id = ? ORDER BY id DESC LIMIT ?""",
                (track_id, limit),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "revision": row["revision"],
                    "operation": row["operation"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    def _current_revision(self, track_id: str) -> int:
        row = self._connection.execute(
            "SELECT revision FROM tracks WHERE id = ?",
            (track_id,),
        ).fetchone()
        if row is None:
            raise KeyError(track_id)
        return int(row["revision"])

    def _bump_revision(self, track_id: str, revision: int) -> None:
        self._connection.execute(
            "UPDATE tracks SET revision = ?, updated_at = ? WHERE id = ?",
            (revision, _now(), track_id),
        )

    def _append_event(
        self,
        track_id: str,
        revision: int,
        operation: str,
        payload: dict[str, Any],
    ) -> None:
        self._connection.execute(
            """INSERT INTO change_events(track_id, revision, operation, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (track_id, revision, operation, json.dumps(payload, ensure_ascii=False), _now()),
        )
