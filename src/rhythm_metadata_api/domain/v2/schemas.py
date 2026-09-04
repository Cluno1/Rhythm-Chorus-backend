from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkAliasInput(ApiModel):
    namespace: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=500)


class CreditInput(ApiModel):
    contributor_id: str
    role: str = Field(min_length=1, max_length=100)
    position: int = Field(default=1, ge=1)


class ContributorCreate(ApiModel):
    display_name: str = Field(min_length=1, max_length=500)
    sort_name: str | None = Field(default=None, max_length=500)


class ContributorResponse(ContributorCreate):
    id: str
    revision: int


class WorkCreate(ApiModel):
    canonical_title: str = Field(min_length=1, max_length=500)
    language: str | None = Field(default=None, max_length=35)
    status: Literal["draft", "active", "archived"] = "active"
    aliases: list[WorkAliasInput] = Field(default_factory=list)
    credits: list[CreditInput] = Field(default_factory=list)


class WorkPatch(ApiModel):
    canonical_title: str | None = Field(default=None, min_length=1, max_length=500)
    language: str | None = Field(default=None, max_length=35)
    status: Literal["draft", "active", "archived"] | None = None


class WorkCreditResponse(ApiModel):
    id: str
    contributor_id: str
    display_name: str
    role: str
    position: int


class WorkResponse(ApiModel):
    id: str
    canonical_title: str
    language: str | None
    status: str
    revision: int
    aliases: list[WorkAliasInput]
    credits: list[WorkCreditResponse]
    created_at: datetime
    updated_at: datetime


class WorkResolveMetadata(ApiModel):
    title: str | None = Field(default=None, max_length=500)
    composer: str | None = Field(default=None, max_length=500)
    voicing: str | None = Field(default=None, max_length=100)


class WorkResolveRequest(ApiModel):
    work_id: str | None = None
    aliases: list[WorkAliasInput] = Field(default_factory=list)
    asset_sha256: list[str] = Field(default_factory=list)
    metadata: WorkResolveMetadata = Field(default_factory=WorkResolveMetadata)

    @field_validator("asset_sha256")
    @classmethod
    def validate_hashes(cls, hashes: list[str]) -> list[str]:
        for value in hashes:
            if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
                raise ValueError("asset_sha256 values must be 64 hexadecimal characters")
        return [value.lower() for value in hashes]


class WorkResolveCandidate(ApiModel):
    work_id: str
    canonical_title: str
    score: float
    reasons: list[str]


class WorkResolveResponse(ApiModel):
    result: Literal["exact", "candidates", "none"]
    matched_by: str | None = None
    work: WorkResponse | None = None
    candidates: list[WorkResolveCandidate] = Field(default_factory=list)


class PartInput(ApiModel):
    code: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    display_order: int = Field(default=1, ge=1)
    midi_channel: int | None = Field(default=None, ge=1, le=16)


class PartResponse(PartInput):
    id: str


class ArrangementCreate(ApiModel):
    name: str = Field(min_length=1, max_length=500)
    voicing: str | None = Field(default=None, max_length=100)
    key_signature: str | None = Field(default=None, max_length=100)
    based_on_id: str | None = None
    parts: list[PartInput] = Field(default_factory=list)


class ArrangementPatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    voicing: str | None = Field(default=None, max_length=100)
    key_signature: str | None = Field(default=None, max_length=100)
    preferred_score_id: str | None = None


class ArrangementResponse(ApiModel):
    id: str
    work_id: str
    name: str
    voicing: str | None
    key_signature: str | None
    based_on_id: str | None
    preferred_score_id: str | None
    revision: int
    parts: list[PartResponse]


class AssetResponse(ApiModel):
    id: str
    sha256: str
    byte_size: int
    media_type: str
    state: str


class UploadCreate(ApiModel):
    sha256: str
    byte_size: int = Field(gt=0)
    media_type: str = Field(min_length=1, max_length=255)
    original_filename: str | None = Field(default=None, max_length=1000)
    source: str = Field(default="upload", min_length=1, max_length=100)
    source_ref: str | None = Field(default=None, max_length=1000)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("sha256 must be 64 hexadecimal characters")
        return normalized


class UploadTarget(ApiModel):
    id: str
    method: Literal["PUT"] = "PUT"
    url: str
    expires_at: datetime


class UploadCreateResponse(ApiModel):
    status: Literal["reused", "upload_required"]
    asset: AssetResponse | None = None
    upload: UploadTarget | None = None


class UploadStatusResponse(ApiModel):
    id: str
    state: str
    expected_sha256: str
    expected_size: int
    actual_sha256: str | None
    actual_size: int | None
    expires_at: datetime
    asset: AssetResponse | None = None


class ScoreCreate(ApiModel):
    label: str = Field(min_length=1, max_length=500)
    origin: Literal["ocr", "midi_transcription", "manual", "external_import"]
    derived_from_revision_id: str | None = None


class ScorePatch(ApiModel):
    label: str | None = Field(default=None, min_length=1, max_length=500)
    published_revision_id: str | None = None


class ScoreAssetInput(ApiModel):
    asset_id: str
    role: Literal["primary_musicxml", "source_midi", "scan", "pdf"]


class ScoreAssetResponse(ScoreAssetInput):
    sha256: str
    byte_size: int
    media_type: str


class ScoreRevisionCreate(ApiModel):
    based_on_revision_id: str | None = None
    edit_message: str | None = Field(default=None, max_length=2000)
    assets: list[ScoreAssetInput] = Field(min_length=1)

    @model_validator(mode="after")
    def exactly_one_primary(self) -> ScoreRevisionCreate:
        if sum(item.role == "primary_musicxml" for item in self.assets) != 1:
            raise ValueError("exactly one primary_musicxml asset is required")
        return self


class ScoreRevisionResponse(ApiModel):
    id: str
    score_id: str
    revision_no: int
    based_on_revision_id: str | None
    edit_message: str | None
    assets: list[ScoreAssetResponse]
    created_at: datetime


class ScoreResponse(ApiModel):
    id: str
    arrangement_id: str
    label: str
    origin: str
    derived_from_revision_id: str | None
    head_revision_id: str | None
    published_revision_id: str | None
    revision: int


class RenditionAssetInput(ApiModel):
    asset_id: str
    role: Literal["master", "stream", "mix", "stem", "midi"]
    part_id: str | None = None
    codec_profile: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_part(self) -> RenditionAssetInput:
        if self.role == "stem" and self.part_id is None:
            raise ValueError("stem assets require part_id")
        if self.role != "stem" and self.part_id is not None:
            raise ValueError("only stem assets may specify part_id")
        return self


class RenditionAssetResponse(RenditionAssetInput):
    id: str
    sha256: str
    byte_size: int
    media_type: str


class RenditionCreate(ApiModel):
    label: str = Field(min_length=1, max_length=500)
    kind: str = Field(min_length=1, max_length=50)
    ensemble: str | None = Field(default=None, max_length=500)
    recorded_at: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    location: str | None = Field(default=None, max_length=500)
    duration_ms: int | None = Field(default=None, ge=0)
    assets: list[RenditionAssetInput] = Field(default_factory=list)


class RenditionPatch(ApiModel):
    label: str | None = Field(default=None, min_length=1, max_length=500)
    kind: str | None = Field(default=None, min_length=1, max_length=50)
    ensemble: str | None = Field(default=None, max_length=500)
    recorded_at: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    location: str | None = Field(default=None, max_length=500)
    duration_ms: int | None = Field(default=None, ge=0)


class RenditionResponse(ApiModel):
    id: str
    arrangement_id: str
    label: str
    kind: str
    ensemble: str | None
    recorded_at: str | None
    location: str | None
    duration_ms: int | None
    revision: int
    assets: list[RenditionAssetResponse]


class PlaybackResponse(ApiModel):
    rendition_id: str
    asset_id: str
    media_type: str
    byte_size: int
    delivery: Literal["authenticated_url", "signed_url"]
    url: str
    cache_key: str
    etag: str
    supports_range: bool
    expires_at: datetime | None = None


class AssetDeliveryResponse(ApiModel):
    asset_id: str
    media_type: str
    byte_size: int
    sha256: str
    delivery: Literal["authenticated_url", "signed_url"]
    url: str
    cache_key: str
    etag: str
    supports_range: bool
    expires_at: datetime | None = None


class LibrarySongResponse(ApiModel):
    work_id: str
    arrangement_id: str
    rendition_id: str
    album_id: str
    title: str
    artist: str | None = None
    album_title: str
    duration_ms: int | None = None
    track_no: int | None = None
    cover_url: str | None = None
    lyrics: str | None = None


class LibraryAlbumResponse(ApiModel):
    id: str
    key: str
    title: str
    artist: str | None = None
    cover_url: str | None = None
    song_count: int


class LibraryAlbumDetailResponse(ApiModel):
    album: LibraryAlbumResponse
    songs: list[LibrarySongResponse]


class ArrangementBundle(ArrangementResponse):
    scores: list[ScoreResponse]
    renditions: list[RenditionResponse]


class WorkBundleResponse(ApiModel):
    work: WorkResponse
    arrangements: list[ArrangementBundle]
    bundle_version: int


class ChangeResponse(ApiModel):
    sequence: int
    entity_type: str
    entity_id: str
    entity_revision: int
    operation: str
    work_ids: list[str]
    tombstone: bool
    created_at: datetime


class ChangesResponse(ApiModel):
    changes: list[ChangeResponse]
    next_cursor: int
    has_more: bool


class IdempotentResponse(ApiModel):
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str]
