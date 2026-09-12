from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rhythm_metadata_api.domain.v2.schemas import AssetDeliveryResponse, UploadTarget


class ChorusApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChorusProjectCreate(ChorusApiModel):
    arrangement_id: str
    alignment_score_revision_id: str
    timeline_hash: str
    title: str = Field(min_length=1, max_length=500)
    status: Literal["draft", "open"] = "open"

    @field_validator("timeline_hash")
    @classmethod
    def validate_timeline_hash(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("timeline_hash must be 64 hexadecimal characters")
        return normalized


class ChorusPartResponse(ChorusApiModel):
    id: str
    code: str
    name: str
    display_order: int


class ChorusSyncAnchor(ChorusApiModel):
    anchor_order: int = Field(ge=0, le=100)
    score_tick: int = Field(ge=0)
    media_ms: int = Field(ge=0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: Literal["in_app_clock", "automatic", "manual"] = "in_app_clock"


class ChorusTrackResponse(ChorusApiModel):
    id: str
    chorus_project_id: str
    rendition_id: str
    uploader_display_name: str
    owned_by_requester: bool
    part_id: str | None
    contribution_kind: str
    display_label: str
    take_no: int
    alignment_state: str
    alignment_offset_ms: int
    status: str
    gain_db: float
    pan: float
    duration_ms: int | None
    waveform_peaks: list[float]
    rejection_reason: str | None
    revision: int
    anchors: list[ChorusSyncAnchor]
    created_at: datetime
    updated_at: datetime


class ChorusProjectResponse(ChorusApiModel):
    id: str
    work_id: str
    arrangement_id: str
    alignment_score_revision_id: str
    timeline_hash: str
    title: str
    status: str
    revision: int
    parts: list[ChorusPartResponse]
    tracks: list[ChorusTrackResponse]
    created_at: datetime
    updated_at: datetime


class WorkChorusResponse(ChorusApiModel):
    work_id: str
    projects: list[ChorusProjectResponse]


class ChorusTrackCreate(ChorusApiModel):
    part_id: str | None = None
    contribution_kind: Literal["vocal_part", "harmony", "guitar", "piano", "percussion", "other"]
    display_label: str = Field(min_length=1, max_length=300)
    take_no: int = Field(default=1, ge=1, le=999)
    sha256: str
    byte_size: int = Field(gt=0, le=100 * 1024 * 1024)
    media_type: Literal[
        "audio/wav",
        "audio/x-wav",
        "audio/mp4",
        "audio/aac",
        "audio/mpeg",
        "audio/flac",
        "audio/ogg",
        "audio/opus",
    ]
    original_filename: str = Field(min_length=1, max_length=300)
    duration_ms: int = Field(gt=0, le=15 * 60 * 1000)
    rights_confirmed: Literal[True]
    initial_anchors: list[ChorusSyncAnchor] = Field(default_factory=list, max_length=16)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("sha256 must be 64 hexadecimal characters")
        return normalized

    @model_validator(mode="after")
    def validate_part_kind_and_anchors(self) -> ChorusTrackCreate:
        if self.contribution_kind == "vocal_part" and self.part_id is None:
            raise ValueError("vocal_part contributions require part_id")
        orders = [anchor.anchor_order for anchor in self.initial_anchors]
        if orders != sorted(set(orders)):
            raise ValueError("anchor_order values must be unique and increasing")
        ticks = [anchor.score_tick for anchor in self.initial_anchors]
        media = [anchor.media_ms for anchor in self.initial_anchors]
        if ticks != sorted(ticks) or media != sorted(media):
            raise ValueError("anchor score_tick and media_ms values must not move backwards")
        return self


class ChorusTrackCreateResponse(ChorusApiModel):
    track: ChorusTrackResponse
    upload_status: Literal["reused", "upload_required"]
    upload: UploadTarget | None = None


class ChorusTrackAlignmentPatch(ChorusApiModel):
    offset_ms: int = Field(ge=-15 * 60 * 1000, le=15 * 60 * 1000)
    anchors: list[ChorusSyncAnchor] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_anchors(self) -> ChorusTrackAlignmentPatch:
        orders = [anchor.anchor_order for anchor in self.anchors]
        ticks = [anchor.score_tick for anchor in self.anchors]
        media = [anchor.media_ms for anchor in self.anchors]
        if orders != sorted(set(orders)):
            raise ValueError("anchor_order values must be unique and increasing")
        if ticks != sorted(ticks) or media != sorted(media):
            raise ValueError("anchor score_tick and media_ms values must not move backwards")
        return self


class ChorusModerationRequest(ChorusApiModel):
    status: Literal["published", "rejected"]
    reason: str | None = Field(default=None, max_length=2000)
    gain_db: float = Field(default=0.0, ge=-24.0, le=12.0)
    pan: float = Field(default=0.0, ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def require_rejection_reason(self) -> ChorusModerationRequest:
        if self.status == "rejected" and not (self.reason or "").strip():
            raise ValueError("rejected tracks require a reason")
        return self


class ChorusModerationSettingsPatch(ChorusApiModel):
    automatic_approval: bool


class ChorusModerationSettingsResponse(ChorusApiModel):
    automatic_approval: bool
    updated_by: str
    updated_at: datetime | None


class ChorusModerationQueueItem(ChorusApiModel):
    work_id: str
    project_title: str
    track: ChorusTrackResponse


class ChorusModerationQueueResponse(ChorusApiModel):
    items: list[ChorusModerationQueueItem]


class ChorusMixResolveRequest(ChorusApiModel):
    track_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("track_ids")
    @classmethod
    def unique_tracks(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("track_ids must be unique")
        return value


class ChorusMixResponse(ChorusApiModel):
    id: str
    chorus_project_id: str
    selection_hash: str
    selected_track_ids: list[str]
    selected_track_count: int
    mix_profile: str
    state: str
    duration_ms: int | None
    error_summary: str | None
    delivery: AssetDeliveryResponse | None
    created_at: datetime
    ready_at: datetime | None
