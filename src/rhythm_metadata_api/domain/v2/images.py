from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ImageApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientImageCapabilities(ImageApiModel):
    enabled: bool
    unavailable_reason: str | None = None
    supported_media_types: list[str]
    max_image_bytes: int
    max_image_pixels: int
    max_batch_items: int
    max_thumbnail_deliveries: int
    max_parallel_uploads: int
    upload_url_ttl_seconds: int
    shared_preview_ttl_seconds: int


class ClientImageBatchCreate(ImageApiModel):
    client_batch_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    total_count: int = Field(ge=1, le=5000)
    total_bytes: int = Field(ge=0)


class ClientImageBatchResponse(ImageApiModel):
    id: str
    client_batch_id: str
    total_count: int
    total_bytes: int
    succeeded_count: int
    failed_count: int
    state: str
    created_at: datetime
    updated_at: datetime


class ClientImageUploadCreate(ImageApiModel):
    batch_id: str
    client_item_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    display_name: str = Field(min_length=1, max_length=500)
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    byte_size: int = Field(gt=0, le=32 * 1024 * 1024)
    content_md5: str = Field(min_length=24, max_length=24)
    client_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(gt=0, le=50_000)
    height: int = Field(gt=0, le=50_000)
    metadata_sanitized: bool

    @field_validator("content_md5")
    @classmethod
    def valid_content_md5(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("content_md5 must be base64 encoded") from error
        if len(decoded) != 16:
            raise ValueError("content_md5 must encode exactly 16 bytes")
        return value

    @field_validator("display_name")
    @classmethod
    def safe_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("display_name contains invalid characters")
        return normalized


class ClientImageUploadTarget(ImageApiModel):
    method: Literal["PUT"] = "PUT"
    url: str
    expires_at: datetime
    required_headers: dict[str, str]


class ClientImageUploadResponse(ImageApiModel):
    upload_id: str
    image_id: str
    state: Literal["upload_required", "completed", "cancelled", "rejected"]
    upload: ClientImageUploadTarget | None = None
    asset_id: str | None = None


class ClientImageRecord(ImageApiModel):
    id: str
    owner_user_id: str
    batch_id: str
    client_item_id: str
    asset_id: str
    asset_sha256: str
    uploader_device_id: str | None
    display_name: str
    media_type: str
    image_format: str
    width: int
    height: int
    byte_size: int
    state: Literal["ready"]
    revision: int
    created_at: datetime
    ready_at: datetime


class ClientImageListResponse(ImageApiModel):
    items: list[ClientImageRecord]
    next_cursor: str | None = None


ImageDeliveryPurpose = Literal["thumbnail", "preview", "download"]
ImageDeliveryVariant = Literal["thumbnail_512", "preview_2048", "original"]


class ClientImageDelivery(ImageApiModel):
    image_id: str
    asset_id: str
    variant: ImageDeliveryVariant
    signed_url: str
    expires_at: datetime
    stable_cache_key: str
    media_type: str
    byte_size: int | None = None
    suggested_filename: str | None = None


class ThumbnailDeliveryRequest(ImageApiModel):
    image_ids: list[str] = Field(min_length=1, max_length=100)
    variant: Literal["thumbnail_512", "preview_2048"] = "thumbnail_512"

    @field_validator("image_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class ThumbnailDeliveryItem(ImageApiModel):
    image_id: str
    status: Literal["ready", "not_found", "failed"]
    delivery: ClientImageDelivery | None = None


class ThumbnailDeliveryResponse(ImageApiModel):
    items: list[ThumbnailDeliveryItem]


class ClientImageDeleteItem(ImageApiModel):
    image_id: str
    result: Literal["deleted", "already_deleted", "not_found", "conflict"]


class ClientImageBulkDeleteRequest(ImageApiModel):
    image_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("image_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))


class ClientImageBulkDeleteResponse(ImageApiModel):
    items: list[ClientImageDeleteItem]


class UserImageVisibilityPatch(ImageApiModel):
    enabled: bool


class UserImageVisibilityResponse(ImageApiModel):
    enabled: bool
    revision: int
    enabled_at: datetime | None
    updated_at: datetime


class AdminClientImageDetail(ImageApiModel):
    image: ClientImageRecord
    asset_location: dict[str, str]
    asset_source: dict[str, str | datetime | None]
    upload_session: dict[str, str | int | datetime | None]
    audit_events: list[dict[str, str | datetime | dict[str, object] | None]]


def is_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))
