from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from rhythm_metadata_api.domain.metadata import ResolutionPolicy, SourceType


class MatchTrackRequest(BaseModel):
    audio_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    file_size: int | None = Field(default=None, ge=1)
    duration_ms: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=1000)
    artist: str | None = Field(default=None, max_length=1000)
    album: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def require_strong_or_weak_identity(self) -> "MatchTrackRequest":
        if self.audio_sha256 is None and not (self.duration_ms and self.title and self.artist):
            raise ValueError("provide audio_sha256 or duration_ms + title + artist")
        return self


class TrackResponse(BaseModel):
    id: str
    revision: int
    matched_by: str


class OverrideValue(BaseModel):
    value: Any
    content_hash: str = Field(min_length=1, max_length=128)
    source: SourceType = SourceType.USER_OVERRIDE
    source_ref: str | None = Field(default=None, max_length=2000)
    pin: bool = True


class OverridePatchRequest(BaseModel):
    fields: dict[str, OverrideValue]
    policy: ResolutionPolicy = ResolutionPolicy.LOCAL_FIRST


class CandidateValue(BaseModel):
    value: Any
    content_hash: str = Field(min_length=1, max_length=128)
    source: SourceType
    source_ref: str | None = Field(default=None, max_length=2000)
    user_edited: bool = False
    pinned: bool = False

    @model_validator(mode="after")
    def reject_override_source(self) -> "CandidateValue":
        if self.source == SourceType.USER_OVERRIDE:
            raise ValueError("use the overrides endpoint for user_override")
        return self


class CandidateSyncRequest(BaseModel):
    fields: dict[str, list[CandidateValue]]
    policy: ResolutionPolicy = ResolutionPolicy.LOCAL_FIRST


class MetadataResponse(BaseModel):
    track_id: str
    revision: int
    resolved: dict[str, dict[str, Any]]
    conflicts: dict[str, list[dict[str, Any]]]
    candidates: dict[str, list[dict[str, Any]]]


class ArtifactKind(StrEnum):
    LYRICS = "lyrics"
    ARTWORK = "artwork"
    AUDIO = "audio"
    MUSICXML = "musicxml"
    MIDI = "midi"


class ArtifactResponse(BaseModel):
    track_id: str
    kind: ArtifactKind
    content_hash: str
    mime_type: str
    size: int
    revision: int
    updated_at: str


class HistoryEvent(BaseModel):
    id: int
    revision: int
    operation: str
    payload: dict[str, Any]
    created_at: str


class HistoryResponse(BaseModel):
    track_id: str
    events: list[HistoryEvent]
