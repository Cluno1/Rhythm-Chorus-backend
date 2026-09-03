from dataclasses import dataclass
from typing import Any

from rhythm_metadata_api.domain.metadata import SourceType


@dataclass(frozen=True, slots=True)
class TrackIdentity:
    audio_sha256: str | None = None
    file_size: int | None = None
    duration_ms: int | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None


@dataclass(frozen=True, slots=True)
class TrackMatch:
    id: str
    revision: int
    matched_by: str


@dataclass(frozen=True, slots=True)
class OverrideField:
    value: Any
    content_hash: str
    source: SourceType = SourceType.USER_OVERRIDE
    source_ref: str | None = None
    pin: bool = True


@dataclass(frozen=True, slots=True)
class OverrideCommand:
    fields: dict[str, OverrideField]


@dataclass(frozen=True, slots=True)
class CandidateField:
    value: Any
    content_hash: str
    source: SourceType
    source_ref: str | None = None
    user_edited: bool = False
    pinned: bool = False


@dataclass(frozen=True, slots=True)
class CandidateSyncCommand:
    fields: dict[str, list[CandidateField]]
