from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PendingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PendingScore(PendingModel):
    id: str
    label: str
    work_id: str
    work_title: str
    arrangement_name: str
    head_revision_id: str | None
    updated_at: datetime


class PendingUpload(PendingModel):
    id: str
    state: str
    original_filename: str | None
    media_type: str
    source: str
    expected_size: int
    expires_at: datetime
    updated_at: datetime


class PendingAsset(PendingModel):
    id: str
    state: str
    media_type: str
    byte_size: int
    sha256: str
    created_at: datetime


class PendingScoreSection(PendingModel):
    total: int
    items: list[PendingScore]


class PendingUploadSection(PendingModel):
    total: int
    items: list[PendingUpload]


class PendingAssetSection(PendingModel):
    total: int
    items: list[PendingAsset]


class PendingSummary(PendingModel):
    unpublished_scores: PendingScoreSection
    failed_uploads: PendingUploadSection
    failed_assets: PendingAssetSection
    pending_assets: PendingAssetSection
