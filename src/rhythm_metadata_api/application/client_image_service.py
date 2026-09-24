from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.catalog_service import ActorContext, is_expired
from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.domain.v2.errors import (
    StaleRevision,
    V2Conflict,
    V2DomainError,
    V2NotFound,
    V2Unavailable,
)
from rhythm_metadata_api.domain.v2.images import (
    AdminClientImageDetail,
    ClientImageBatchCreate,
    ClientImageBatchResponse,
    ClientImageCapabilities,
    ClientImageDelivery,
    ClientImageRecord,
    ClientImageUploadCreate,
    ClientImageUploadResponse,
    ClientImageUploadTarget,
    ThumbnailDeliveryItem,
    ThumbnailDeliveryRequest,
    ThumbnailDeliveryResponse,
    UserImageVisibilityResponse,
)
from rhythm_metadata_api.infrastructure.db.models import (
    Asset,
    AssetLocation,
    AssetSource,
    AuthUser,
    ClientImage,
    ClientImageAuditEvent,
    ClientImageBatch,
    UploadSession,
    UserImageAdminVisibility,
    new_id,
    utc_now,
)
from rhythm_metadata_api.infrastructure.storage.base import UploadValidationError
from rhythm_metadata_api.infrastructure.storage.cos_images import (
    ClientImageObjectGateway,
    CosImageGatewayError,
)
from rhythm_metadata_api.infrastructure.storage.cos_presign import presign_cos_get, presign_cos_put

_MEDIA_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
}
_FORMAT_MEDIA_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg", "webp": "image/webp"}
_VARIANT_RECIPES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "thumbnail_512": (("imageMogr2/thumbnail/512x512>", None),),
    "preview_2048": (("imageMogr2/thumbnail/2048x2048>", None),),
    "original": (),
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ClientImageService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
        object_gateway: ClientImageObjectGateway,
    ) -> None:
        self.uow_factory = uow_factory
        self.settings = settings
        self.object_gateway = object_gateway

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.client_image_cos_bucket
            and self.settings.client_image_preview_host
            and self.settings.client_image_ci_enabled
            and self.settings.cos_secret_id
            and self.settings.cos_secret_key
        )

    def capabilities(self) -> ClientImageCapabilities:
        reason = None
        if not self.settings.client_image_cos_bucket:
            reason = "image_bucket_not_configured"
        elif not self.settings.client_image_preview_host:
            reason = "image_preview_host_not_configured"
        elif not self.settings.client_image_ci_enabled:
            reason = "image_validation_not_enabled"
        elif not (self.settings.cos_secret_id and self.settings.cos_secret_key):
            reason = "cos_credentials_not_configured"
        return ClientImageCapabilities(
            enabled=reason is None,
            unavailable_reason=reason,
            supported_media_types=list(_MEDIA_FORMATS),
            max_image_bytes=self.settings.client_image_max_bytes,
            max_image_pixels=self.settings.client_image_max_pixels,
            max_batch_items=self.settings.client_image_max_batch_items,
            max_thumbnail_deliveries=self.settings.client_image_max_thumbnail_deliveries,
            max_parallel_uploads=self.settings.client_image_max_parallel_uploads,
            upload_url_ttl_seconds=self.settings.client_image_presign_expires_seconds,
            shared_preview_ttl_seconds=self.settings.client_image_shared_expires_seconds,
        )

    def create_batch(
        self, request: ClientImageBatchCreate, actor: ActorContext
    ) -> ClientImageBatchResponse:
        self._require_enabled()
        if request.total_count > self.settings.client_image_max_batch_items:
            raise V2DomainError("image batch exceeds the configured item limit")
        if request.total_bytes > request.total_count * self.settings.client_image_max_bytes:
            raise V2DomainError("image batch exceeds the configured byte limit")
        with self.uow_factory() as uow:
            self._ensure_actor_user(uow.session, actor)
            existing = uow.session.scalar(
                select(ClientImageBatch).where(
                    ClientImageBatch.owner_user_id == actor.actor_id,
                    ClientImageBatch.client_batch_id == request.client_batch_id,
                )
            )
            if existing is not None:
                if (existing.total_count, existing.total_bytes) != (
                    request.total_count,
                    request.total_bytes,
                ):
                    raise V2Conflict("client_batch_id was reused with different totals")
                return self._batch_response(existing)
            batch = ClientImageBatch(
                owner_user_id=actor.actor_id,
                client_batch_id=request.client_batch_id,
                total_count=request.total_count,
                total_bytes=request.total_bytes,
                state="active",
            )
            uow.session.add(batch)
            uow.session.flush()
            self._audit(
                uow.session,
                actor,
                "image_batch.created",
                batch_id=batch.id,
                details={"total_count": batch.total_count, "total_bytes": batch.total_bytes},
            )
            return self._batch_response(batch)

    def get_batch(self, batch_id: str, actor: ActorContext) -> ClientImageBatchResponse:
        with self.uow_factory() as uow:
            batch = self._owned_batch(uow.session, batch_id, actor)
            self._refresh_batch(uow.session, batch)
            return self._batch_response(batch)

    def cancel_batch(self, batch_id: str, actor: ActorContext) -> ClientImageBatchResponse:
        keys: list[str] = []
        with self.uow_factory() as uow:
            batch = self._owned_batch(uow.session, batch_id, actor)
            if batch.state == "cancelled":
                return self._batch_response(batch)
            rows = list(
                uow.session.scalars(
                    select(ClientImage).where(
                        ClientImage.batch_id == batch.id,
                        ClientImage.owner_user_id == actor.actor_id,
                        ClientImage.state.in_(("upload_pending", "verifying")),
                    )
                )
            )
            for image in rows:
                upload = uow.session.get(UploadSession, image.upload_session_id)
                if upload is not None:
                    upload.state = "cancelled"
                    upload.updated_at = utc_now()
                    key = self._temporary_object_key(upload)
                    if key:
                        keys.append(key)
                image.state = "cancelled"
                image.revision += 1
                image.updated_at = utc_now()
            self._refresh_batch(uow.session, batch)
            batch.state = "cancelled"
            batch.cancelled_at = utc_now()
            batch.updated_at = utc_now()
            self._audit(uow.session, actor, "image_batch.cancelled", batch_id=batch.id)
            response = self._batch_response(batch)
        for key in keys:
            self._delete_best_effort(key)
        return response

    def create_upload(
        self, request: ClientImageUploadCreate, actor: ActorContext
    ) -> ClientImageUploadResponse:
        self._require_enabled()
        self._validate_upload_request(request)
        with self.uow_factory() as uow:
            self._ensure_actor_user(uow.session, actor)
            batch = self._owned_batch(uow.session, request.batch_id, actor)
            if batch.state == "cancelled":
                raise V2Conflict("image batch is cancelled")
            existing = uow.session.scalar(
                select(ClientImage).where(
                    ClientImage.owner_user_id == actor.actor_id,
                    ClientImage.batch_id == batch.id,
                    ClientImage.client_item_id == request.client_item_id,
                )
            )
            if existing is not None:
                self._assert_same_upload(existing, request)
                return self._upload_response(uow.session, existing)
            item_count = uow.session.scalar(
                select(func.count(ClientImage.id)).where(ClientImage.batch_id == batch.id)
            ) or 0
            if item_count >= batch.total_count:
                raise V2Conflict("image batch already contains its declared item count")
            used_bytes = uow.session.scalar(
                select(func.coalesce(func.sum(ClientImage.byte_size), 0)).where(
                    ClientImage.owner_user_id == actor.actor_id,
                    ClientImage.deleted_at.is_(None),
                    ClientImage.state.notin_(("rejected", "cancelled", "deleted")),
                )
            ) or 0
            if used_bytes + request.byte_size > self.settings.client_image_storage_quota_bytes:
                raise V2Conflict("client image storage quota would be exceeded")

            image_id = new_id()
            upload_id = new_id()
            owner_partition = hashlib.sha256(actor.actor_id.encode()).hexdigest()[:24]
            object_key = f"labs/images/tmp/{owner_partition}/{upload_id}/original"
            upload = UploadSession(
                id=upload_id,
                expected_sha256=request.client_sha256,
                expected_size=request.byte_size,
                media_type=request.media_type,
                original_filename=request.display_name,
                source="labs_image",
                source_ref=image_id,
                temporary_key=f"cos://{self.settings.client_image_cos_bucket}/{object_key}",
                expires_at=utc_now() + timedelta(seconds=self.settings.upload_session_ttl_seconds),
            )
            image = ClientImage(
                id=image_id,
                owner_user_id=actor.actor_id,
                batch_id=batch.id,
                client_item_id=request.client_item_id,
                upload_session_id=upload.id,
                uploader_device_id=actor.device_id,
                display_name=request.display_name,
                media_type=request.media_type,
                width=request.width,
                height=request.height,
                byte_size=request.byte_size,
                content_md5=request.content_md5,
                client_sha256=request.client_sha256,
                metadata_sanitized=request.metadata_sanitized,
                state="upload_pending",
            )
            # There is no ORM relationship between these aggregate records, so
            # explicitly persist the parent before the FK-constrained image row.
            uow.session.add(upload)
            uow.session.flush()
            uow.session.add(image)
            uow.session.flush()
            self._audit(
                uow.session,
                actor,
                "image_upload.created",
                image_id=image.id,
                batch_id=batch.id,
                details={"byte_size": image.byte_size, "media_type": image.media_type},
            )
            return self._upload_response(uow.session, image)

    def refresh_upload(self, upload_id: str, actor: ActorContext) -> ClientImageUploadResponse:
        self._require_enabled()
        with self.uow_factory() as uow:
            image = self._owned_image_by_upload(uow.session, upload_id, actor)
            upload = uow.session.get(UploadSession, upload_id)
            if upload is None:
                raise V2NotFound("image upload was not found")
            if image.state == "ready":
                return self._upload_response(uow.session, image)
            if image.state == "deleted":
                raise V2Conflict("image upload was deleted")
            if image.state == "verifying":
                raise V2Conflict("image upload verification is already in progress")
            if image.state in {"cancelled", "rejected"}:
                return self._upload_response(uow.session, image)
            if is_expired(upload.expires_at):
                upload.expires_at = utc_now() + timedelta(
                    seconds=self.settings.upload_session_ttl_seconds
                )
                upload.state = "created"
                upload.updated_at = utc_now()
            if upload.state == "failed":
                upload.state = "created"
            return self._upload_response(uow.session, image)

    def complete_upload(self, upload_id: str, actor: ActorContext) -> ClientImageRecord:
        self._require_enabled()
        with self.uow_factory() as uow:
            image = self._owned_image_by_upload(uow.session, upload_id, actor)
            if image.state == "ready":
                return self._record(uow.session, image)
            if image.state in {"cancelled", "rejected", "deleted"}:
                raise V2Conflict(f"image upload cannot complete while {image.state}")
            upload = uow.session.get(UploadSession, image.upload_session_id)
            if upload is None or is_expired(upload.expires_at):
                if upload is not None:
                    upload.state = "expired"
                raise V2Conflict("image upload session has expired")
            object_key = self._temporary_object_key(upload)
            if object_key is None:
                raise V2Conflict("image upload target is invalid")
            image.state = "verifying"
            image.failure_code = None
            image.updated_at = utc_now()
            upload.state = "uploaded"
            upload.updated_at = utc_now()

        try:
            metadata = self.object_gateway.head(object_key)
            image_info = self.object_gateway.image_info(object_key)
            file_hash = self.object_gateway.sha256(object_key)
        except CosImageGatewayError as error:
            with self.uow_factory() as uow:
                image = self._owned_image_by_upload(uow.session, upload_id, actor)
                if image.state == "verifying":
                    image.state = "upload_pending"
                    image.failure_code = "verification_unavailable"
                    image.updated_at = utc_now()
            raise V2Unavailable("COS image verification is temporarily unavailable") from error

        failure = self._inspection_failure(image, metadata, image_info, file_hash)
        if failure is not None:
            self._reject_upload(upload_id, actor, object_key, failure)
            raise UploadValidationError(failure)

        # HEAD/imageInfo/filehash are deliberately performed outside a database
        # transaction. Re-check the durable state before promoting bytes because
        # the owner may have cancelled or deleted the image while those network
        # calls were running.
        with self.uow_factory() as uow:
            current = self._owned_image_by_upload(uow.session, upload_id, actor)
            if current.state == "ready":
                return self._record(uow.session, current)
            if current.state != "verifying":
                raise V2Conflict(f"image upload cannot complete while {current.state}")

        detected_media_type = _FORMAT_MEDIA_TYPES[image_info.image_format]
        final_key = f"labs/images/assets/{file_hash.sha256[:2]}/{file_hash.sha256}"
        with self.uow_factory() as uow:
            existing_asset = uow.session.scalar(
                select(Asset).where(Asset.sha256 == file_hash.sha256)
            )
            existing_location = (
                uow.session.scalar(
                    select(AssetLocation).where(
                        AssetLocation.asset_id == existing_asset.id,
                        AssetLocation.provider == "cos",
                        AssetLocation.state == "available",
                    )
                )
                if existing_asset is not None
                else None
            )
        if existing_asset is not None and (
            existing_asset.byte_size != image_info.byte_size
            or existing_asset.detected_media_type != detected_media_type
        ):
            self._reject_upload(upload_id, actor, object_key, "verified asset identity conflicts")
            raise UploadValidationError("verified asset identity conflicts")
        if existing_location is None:
            try:
                self.object_gateway.promote(object_key, final_key)
            except CosImageGatewayError as error:
                with self.uow_factory() as uow:
                    image = self._owned_image_by_upload(uow.session, upload_id, actor)
                    image.state = "upload_pending"
                    image.failure_code = "promotion_unavailable"
                raise V2Unavailable("COS image promotion is temporarily unavailable") from error

        final_storage_key = (
            existing_location.storage_key
            if existing_location is not None
            else f"{self.settings.client_image_cos_bucket}/{final_key}"
        )

        with self.uow_factory() as uow:
            image = self._owned_image_by_upload(uow.session, upload_id, actor)
            if image.state == "ready":
                return self._record(uow.session, image)
            if image.state != "verifying":
                raise V2Conflict(f"image upload cannot complete while {image.state}")
            upload = uow.session.get(UploadSession, image.upload_session_id)
            if upload is None:
                raise V2Conflict("image upload session disappeared")
            asset = uow.session.scalar(select(Asset).where(Asset.sha256 == file_hash.sha256))
            if asset is None:
                asset = Asset(
                    sha256=file_hash.sha256,
                    byte_size=image_info.byte_size,
                    detected_media_type=detected_media_type,
                    state="ready",
                )
                uow.session.add(asset)
                uow.session.flush()
            else:
                # Asset identity is content-addressed. A fresh COS/CI verification may safely
                # revive an otherwise soft-deleted row with the same bytes.
                asset.state = "ready"
                asset.deleted_at = None
                asset.updated_at = utc_now()
            location = uow.session.scalar(
                select(AssetLocation).where(
                    AssetLocation.asset_id == asset.id,
                    AssetLocation.provider == "cos",
                )
            )
            if location is None:
                uow.session.add(
                    AssetLocation(
                        asset_id=asset.id,
                        provider="cos",
                        storage_key=final_storage_key,
                        state="available",
                    )
                )
            elif location.state != "available":
                location.storage_key = final_storage_key
                location.state = "available"
            uow.session.add(
                AssetSource(
                    asset_id=asset.id,
                    original_filename=image.display_name,
                    source="labs_image",
                    source_ref=image.id,
                )
            )
            now = utc_now()
            upload.actual_sha256 = file_hash.sha256
            upload.actual_size = image_info.byte_size
            upload.completed_asset_id = asset.id
            upload.state = "completed"
            upload.updated_at = now
            image.asset_id = asset.id
            image.media_type = detected_media_type
            image.image_format = image_info.image_format
            image.width = image_info.width
            image.height = image_info.height
            image.byte_size = image_info.byte_size
            image.state = "ready"
            image.failure_code = None
            image.revision += 1
            image.ready_at = now
            image.updated_at = now
            batch = uow.session.get(ClientImageBatch, image.batch_id)
            if batch is not None:
                self._refresh_batch(uow.session, batch)
            self._audit(
                uow.session,
                actor,
                "image_upload.completed",
                image_id=image.id,
                batch_id=image.batch_id,
                details={"asset_id": asset.id, "byte_size": image.byte_size},
            )
            uow.session.flush()
            response = self._record(uow.session, image)
        self._delete_best_effort(object_key)
        return response

    def cancel_upload(self, upload_id: str, actor: ActorContext) -> ClientImageUploadResponse:
        object_key = None
        with self.uow_factory() as uow:
            image = self._owned_image_by_upload(uow.session, upload_id, actor)
            upload = uow.session.get(UploadSession, upload_id)
            if image.state == "ready":
                return self._upload_response(uow.session, image)
            if image.state == "deleted":
                raise V2Conflict("image upload was deleted")
            if image.state != "cancelled":
                image.state = "cancelled"
                image.revision += 1
                image.updated_at = utc_now()
                if upload is not None:
                    object_key = self._temporary_object_key(upload)
                    upload.state = "cancelled"
                    upload.updated_at = utc_now()
                batch = uow.session.get(ClientImageBatch, image.batch_id)
                if batch is not None:
                    self._refresh_batch(uow.session, batch)
                self._audit(
                    uow.session,
                    actor,
                    "image_upload.cancelled",
                    image_id=image.id,
                    batch_id=image.batch_id,
                )
            response = self._upload_response(uow.session, image)
        if object_key:
            self._delete_best_effort(object_key)
        return response

    def list_own(
        self, actor: ActorContext, cursor: str | None, limit: int
    ) -> tuple[list[ClientImageRecord], str | None]:
        with self.uow_factory() as uow:
            query = select(ClientImage).where(
                ClientImage.owner_user_id == actor.actor_id,
                ClientImage.state == "ready",
                ClientImage.deleted_at.is_(None),
            )
            query = self._after_cursor(uow.session, query, cursor)
            rows = list(
                uow.session.scalars(
                    query.order_by(ClientImage.created_at.desc(), ClientImage.id.desc()).limit(
                        limit + 1
                    )
                )
            )
            next_cursor = rows[limit - 1].id if len(rows) > limit else None
            return [self._record(uow.session, item) for item in rows[:limit]], next_cursor

    def get_own(self, image_id: str, actor: ActorContext) -> ClientImageRecord:
        with self.uow_factory() as uow:
            return self._record(uow.session, self._owned_ready_image(uow.session, image_id, actor))

    def own_delivery(
        self, image_id: str, purpose: str, actor: ActorContext
    ) -> ClientImageDelivery:
        variants = {"thumbnail": "thumbnail_512", "preview": "preview_2048", "download": "original"}
        if purpose not in variants:
            raise V2DomainError("unsupported image delivery purpose")
        with self.uow_factory() as uow:
            image = self._owned_ready_image(uow.session, image_id, actor)
            return self._delivery(uow.session, image, variants[purpose], shared=False)

    def own_thumbnail_deliveries(
        self, request: ThumbnailDeliveryRequest, actor: ActorContext
    ) -> ThumbnailDeliveryResponse:
        self._validate_delivery_count(request.image_ids)
        with self.uow_factory() as uow:
            rows = {
                item.id: item
                for item in uow.session.scalars(
                    select(ClientImage).where(
                        ClientImage.id.in_(request.image_ids),
                        ClientImage.owner_user_id == actor.actor_id,
                        ClientImage.state == "ready",
                        ClientImage.deleted_at.is_(None),
                    )
                )
            }
            return ThumbnailDeliveryResponse(
                items=[
                    ThumbnailDeliveryItem(
                        image_id=image_id,
                        status="ready" if image_id in rows else "not_found",
                        delivery=(
                            self._delivery(uow.session, rows[image_id], request.variant, shared=False)
                            if image_id in rows
                            else None
                        ),
                    )
                    for image_id in request.image_ids
                ]
            )

    def delete_own(self, image_id: str, actor: ActorContext) -> str:
        object_key = None
        with self.uow_factory() as uow:
            image = uow.session.get(ClientImage, image_id)
            if image is None or image.owner_user_id != actor.actor_id:
                return "not_found"
            if image.deleted_at is not None or image.state == "deleted":
                return "already_deleted"
            if image.state in {"upload_pending", "verifying"}:
                upload = uow.session.get(UploadSession, image.upload_session_id)
                if upload is not None:
                    object_key = self._temporary_object_key(upload)
                    upload.state = "cancelled"
                    upload.updated_at = utc_now()
            now = utc_now()
            image.state = "deleted"
            image.deleted_at = now
            image.updated_at = now
            image.revision += 1
            batch = uow.session.get(ClientImageBatch, image.batch_id)
            if batch is not None:
                self._refresh_batch(uow.session, batch)
            self._audit(
                uow.session,
                actor,
                "client_image.deleted",
                image_id=image.id,
                batch_id=image.batch_id,
            )
        if object_key:
            self._delete_best_effort(object_key)
        return "deleted"

    def visibility(self, actor: ActorContext) -> UserImageVisibilityResponse:
        with self.uow_factory() as uow:
            row = uow.session.get(UserImageAdminVisibility, actor.actor_id)
            return self._visibility_response(row)

    def update_visibility(
        self, enabled: bool, expected_revision: int, actor: ActorContext
    ) -> UserImageVisibilityResponse:
        with self.uow_factory() as uow:
            self._ensure_actor_user(uow.session, actor)
            row = uow.session.get(UserImageAdminVisibility, actor.actor_id)
            current_revision = row.revision if row is not None else 1
            if current_revision != expected_revision:
                raise StaleRevision(f'"rev-{expected_revision}"', f'"rev-{current_revision}"')
            now = utc_now()
            if row is None:
                row = UserImageAdminVisibility(
                    owner_user_id=actor.actor_id,
                    enabled=enabled,
                    revision=2,
                    enabled_at=now if enabled else None,
                    updated_at=now,
                )
                uow.session.add(row)
            else:
                row.enabled = enabled
                row.revision += 1
                row.enabled_at = now if enabled else None
                row.updated_at = now
            self._audit(
                uow.session,
                actor,
                "image_admin_visibility.enabled" if enabled else "image_admin_visibility.disabled",
            )
            uow.session.flush()
            return self._visibility_response(row)

    def list_shared(
        self,
        cursor: str | None,
        limit: int,
        owner_user_id: str | None = None,
        image_format: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> tuple[list[ClientImageRecord], str | None]:
        with self.uow_factory() as uow:
            query = (
                select(ClientImage)
                .join(
                    UserImageAdminVisibility,
                    UserImageAdminVisibility.owner_user_id == ClientImage.owner_user_id,
                )
                .where(
                    UserImageAdminVisibility.enabled.is_(True),
                    ClientImage.state == "ready",
                    ClientImage.deleted_at.is_(None),
                )
            )
            if owner_user_id:
                query = query.where(ClientImage.owner_user_id == owner_user_id)
            if image_format:
                query = query.where(ClientImage.image_format == image_format.lower())
            if created_from:
                query = query.where(ClientImage.created_at >= created_from)
            if created_to:
                query = query.where(ClientImage.created_at <= created_to)
            query = self._after_cursor(uow.session, query, cursor)
            rows = list(
                uow.session.scalars(
                    query.order_by(ClientImage.created_at.desc(), ClientImage.id.desc()).limit(
                        limit + 1
                    )
                )
            )
            next_cursor = rows[limit - 1].id if len(rows) > limit else None
            return [self._record(uow.session, item) for item in rows[:limit]], next_cursor

    def get_shared(self, image_id: str) -> ClientImageRecord:
        with self.uow_factory() as uow:
            return self._record(uow.session, self._shared_ready_image(uow.session, image_id))

    def shared_detail(self, image_id: str) -> AdminClientImageDetail:
        with self.uow_factory() as uow:
            image = self._shared_ready_image(uow.session, image_id)
            asset = uow.session.get(Asset, image.asset_id)
            location = uow.session.scalar(
                select(AssetLocation).where(
                    AssetLocation.asset_id == image.asset_id,
                    AssetLocation.provider == "cos",
                )
            )
            source = uow.session.scalar(
                select(AssetSource)
                .where(
                    AssetSource.asset_id == image.asset_id,
                    AssetSource.source_ref == image.id,
                )
                .order_by(AssetSource.created_at.desc())
            )
            upload = uow.session.get(UploadSession, image.upload_session_id)
            audits = list(
                uow.session.scalars(
                    select(ClientImageAuditEvent)
                    .where(ClientImageAuditEvent.image_id == image.id)
                    .order_by(ClientImageAuditEvent.created_at.desc())
                    .limit(100)
                )
            )
            if asset is None or location is None or source is None or upload is None:
                raise V2NotFound("shared image diagnostics are incomplete")
            return AdminClientImageDetail(
                image=self._record(uow.session, image),
                asset_location={
                    "provider": location.provider,
                    "storage_key": location.storage_key,
                    "state": location.state,
                },
                asset_source={
                    "source": source.source,
                    "source_ref": source.source_ref,
                    "original_filename": source.original_filename,
                    "created_at": source.created_at,
                },
                upload_session={
                    "id": upload.id,
                    "state": upload.state,
                    "expected_size": upload.expected_size,
                    "actual_size": upload.actual_size,
                    "created_at": upload.created_at,
                    "updated_at": upload.updated_at,
                },
                audit_events=[
                    {
                        "operation": item.operation,
                        "actor_id": item.actor_id,
                        "device_id": item.device_id,
                        "details": item.details_json,
                        "created_at": item.created_at,
                    }
                    for item in audits
                ],
            )

    def shared_delivery(self, image_id: str, variant: str) -> ClientImageDelivery:
        if variant not in {"thumbnail_512", "preview_2048"}:
            raise V2DomainError("shared image delivery only supports preview variants")
        with self.uow_factory() as uow:
            image = self._shared_ready_image(uow.session, image_id)
            return self._delivery(uow.session, image, variant, shared=True)

    def shared_thumbnail_deliveries(
        self, request: ThumbnailDeliveryRequest
    ) -> ThumbnailDeliveryResponse:
        self._validate_delivery_count(request.image_ids)
        with self.uow_factory() as uow:
            rows = {
                item.id: item
                for item in uow.session.scalars(
                    select(ClientImage)
                    .join(
                        UserImageAdminVisibility,
                        UserImageAdminVisibility.owner_user_id == ClientImage.owner_user_id,
                    )
                    .where(
                        ClientImage.id.in_(request.image_ids),
                        ClientImage.state == "ready",
                        ClientImage.deleted_at.is_(None),
                        UserImageAdminVisibility.enabled.is_(True),
                    )
                )
            }
            return ThumbnailDeliveryResponse(
                items=[
                    ThumbnailDeliveryItem(
                        image_id=image_id,
                        status="ready" if image_id in rows else "not_found",
                        delivery=(
                            self._delivery(uow.session, rows[image_id], request.variant, shared=True)
                            if image_id in rows
                            else None
                        ),
                    )
                    for image_id in request.image_ids
                ]
            )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise V2Unavailable("client image COS/CI capability is not configured")

    def _validate_upload_request(self, request: ClientImageUploadCreate) -> None:
        if request.byte_size > self.settings.client_image_max_bytes:
            raise V2DomainError("image exceeds the configured byte limit")
        if request.width * request.height > self.settings.client_image_max_pixels:
            raise V2DomainError("image exceeds the configured pixel limit")
        if not request.metadata_sanitized:
            raise V2DomainError("image metadata must be sanitized before upload")

    def _upload_response(self, session: Session, image: ClientImage) -> ClientImageUploadResponse:
        upload = session.get(UploadSession, image.upload_session_id)
        if upload is None:
            raise V2Conflict("image upload session is missing")
        if image.state == "ready":
            return ClientImageUploadResponse(
                upload_id=upload.id,
                image_id=image.id,
                state="completed",
                asset_id=image.asset_id,
            )
        if image.state == "deleted":
            raise V2Conflict("image upload was deleted")
        if image.state in {"cancelled", "rejected"}:
            return ClientImageUploadResponse(
                upload_id=upload.id,
                image_id=image.id,
                state=image.state,
                asset_id=image.asset_id,
            )
        if is_expired(upload.expires_at):
            upload.expires_at = utc_now() + timedelta(
                seconds=self.settings.upload_session_ttl_seconds
            )
            upload.state = "created"
            upload.updated_at = utc_now()
        key = self._temporary_object_key(upload)
        if key is None:
            raise V2Conflict("image upload target is invalid")
        headers = {"Content-Type": image.media_type, "Content-MD5": image.content_md5}
        url, expires_at = presign_cos_put(
            self.settings.client_image_cos_bucket,
            self.settings.cos_region,
            key,
            self.settings.cos_secret_id,
            self.settings.cos_secret_key,
            self.settings.client_image_presign_expires_seconds,
            headers=headers,
        )
        return ClientImageUploadResponse(
            upload_id=upload.id,
            image_id=image.id,
            state="upload_required",
            upload=ClientImageUploadTarget(
                url=url,
                expires_at=expires_at,
                required_headers=headers,
            ),
        )

    def _inspection_failure(self, image, metadata, image_info, file_hash) -> str | None:
        if metadata.byte_size != image.byte_size or image_info.byte_size != image.byte_size:
            return "uploaded size does not match the declaration"
        if file_hash.byte_size is not None and file_hash.byte_size != image.byte_size:
            return "COS hash result size does not match the declaration"
        if metadata.content_type != image.media_type:
            return "uploaded Content-Type does not match the declaration"
        detected_media_type = _FORMAT_MEDIA_TYPES.get(image_info.image_format)
        if detected_media_type is None or detected_media_type != image.media_type:
            return "uploaded object is not a supported static image"
        if image_info.frame_count != 1:
            return "animated images are not supported"
        if image_info.width != image.width or image_info.height != image.height:
            return "uploaded dimensions do not match the declaration"
        if image_info.width * image_info.height > self.settings.client_image_max_pixels:
            return "uploaded image exceeds the configured pixel limit"
        if image_info.md5_hex != base64.b64decode(image.content_md5).hex():
            return "uploaded MD5 does not match Content-MD5"
        if file_hash.sha256 != image.client_sha256:
            return "uploaded SHA-256 does not match the declaration"
        return None

    def _reject_upload(
        self, upload_id: str, actor: ActorContext, object_key: str, reason: str
    ) -> None:
        with self.uow_factory() as uow:
            image = self._owned_image_by_upload(uow.session, upload_id, actor)
            upload = uow.session.get(UploadSession, upload_id)
            image.state = "rejected"
            image.failure_code = "validation_failed"
            image.revision += 1
            image.updated_at = utc_now()
            if upload is not None:
                upload.state = "failed"
                upload.updated_at = utc_now()
            batch = uow.session.get(ClientImageBatch, image.batch_id)
            if batch is not None:
                self._refresh_batch(uow.session, batch)
            self._audit(
                uow.session,
                actor,
                "image_upload.rejected",
                image_id=image.id,
                batch_id=image.batch_id,
                details={"reason": reason},
            )
        self._delete_best_effort(object_key)

    def _delivery(
        self, session: Session, image: ClientImage, variant: str, *, shared: bool
    ) -> ClientImageDelivery:
        if variant not in _VARIANT_RECIPES:
            raise V2DomainError("unsupported image delivery variant")
        asset = session.get(Asset, image.asset_id)
        if asset is None or asset.state != "ready" or asset.deleted_at is not None:
            raise V2NotFound("image Asset was not found")
        location = session.scalar(
            select(AssetLocation).where(
                AssetLocation.asset_id == asset.id,
                AssetLocation.provider == "cos",
                AssetLocation.state == "available",
            )
        )
        if location is None:
            raise V2Unavailable("image has no available COS location")
        bucket, separator, key = location.storage_key.partition("/")
        if not bucket or not separator or not key:
            raise V2Unavailable("image COS location is invalid")
        ttl = (
            self.settings.client_image_shared_expires_seconds
            if shared
            else self.settings.client_image_presign_expires_seconds
        )
        preview_host = (
            self.settings.client_image_preview_host
            if bucket == self.settings.client_image_cos_bucket
            and self.settings.client_image_preview_host
            else None
        )
        url, expires_at = presign_cos_get(
            bucket,
            self.settings.cos_region,
            key,
            self.settings.cos_secret_id,
            self.settings.cos_secret_key,
            ttl,
            query_parameters=_VARIANT_RECIPES[variant],
            host=preview_host,
        )
        return ClientImageDelivery(
            image_id=image.id,
            asset_id=asset.id,
            variant=variant,
            signed_url=url,
            expires_at=expires_at,
            stable_cache_key=f"client-image:{image.id}:{asset.sha256}:{variant}:v1",
            media_type=asset.detected_media_type,
            byte_size=asset.byte_size if variant == "original" else None,
            suggested_filename=(self._download_name(image) if variant == "original" else None),
        )

    def _record(self, session: Session, image: ClientImage) -> ClientImageRecord:
        if image.state != "ready" or not image.asset_id or image.ready_at is None:
            raise V2NotFound("ready image was not found")
        asset = session.get(Asset, image.asset_id)
        if asset is None:
            raise V2NotFound("image Asset was not found")
        return ClientImageRecord(
            id=image.id,
            owner_user_id=image.owner_user_id,
            batch_id=image.batch_id,
            client_item_id=image.client_item_id,
            asset_id=asset.id,
            asset_sha256=asset.sha256,
            uploader_device_id=image.uploader_device_id,
            display_name=image.display_name,
            media_type=image.media_type,
            image_format=image.image_format or _MEDIA_FORMATS[image.media_type],
            width=image.width,
            height=image.height,
            byte_size=image.byte_size,
            state="ready",
            revision=image.revision,
            created_at=image.created_at,
            ready_at=image.ready_at,
        )

    def _owned_batch(
        self, session: Session, batch_id: str, actor: ActorContext
    ) -> ClientImageBatch:
        batch = session.get(ClientImageBatch, batch_id)
        if batch is None or batch.owner_user_id != actor.actor_id:
            raise V2NotFound("image batch was not found")
        return batch

    def _owned_image_by_upload(
        self, session: Session, upload_id: str, actor: ActorContext
    ) -> ClientImage:
        image = session.scalar(
            select(ClientImage).where(
                ClientImage.upload_session_id == upload_id,
                ClientImage.owner_user_id == actor.actor_id,
            )
        )
        if image is None:
            raise V2NotFound("image upload was not found")
        return image

    def _owned_ready_image(
        self, session: Session, image_id: str, actor: ActorContext
    ) -> ClientImage:
        image = session.get(ClientImage, image_id)
        if (
            image is None
            or image.owner_user_id != actor.actor_id
            or image.state != "ready"
            or image.deleted_at is not None
        ):
            raise V2NotFound("image was not found")
        return image

    def _shared_ready_image(self, session: Session, image_id: str) -> ClientImage:
        image = session.scalar(
            select(ClientImage)
            .join(
                UserImageAdminVisibility,
                UserImageAdminVisibility.owner_user_id == ClientImage.owner_user_id,
            )
            .where(
                ClientImage.id == image_id,
                ClientImage.state == "ready",
                ClientImage.deleted_at.is_(None),
                UserImageAdminVisibility.enabled.is_(True),
            )
        )
        if image is None:
            raise V2NotFound("shared image was not found")
        return image

    def _after_cursor(self, session: Session, query, cursor: str | None):
        if cursor is None:
            return query
        # Resolve the anchor through the already-authorized/filtered query. A
        # direct session.get() would let a caller distinguish a private image ID
        # from a nonexistent ID by using it as a shared-list cursor.
        anchor = session.scalar(query.where(ClientImage.id == cursor).limit(1))
        if anchor is None:
            raise V2DomainError("image cursor is invalid")
        return query.where(
            or_(
                ClientImage.created_at < anchor.created_at,
                (ClientImage.created_at == anchor.created_at) & (ClientImage.id < anchor.id),
            )
        )

    def _refresh_batch(self, session: Session, batch: ClientImageBatch) -> None:
        # UnitOfWork sessions disable autoflush. Persist item transitions before
        # deriving the aggregate batch state in the same transaction.
        session.flush()
        states = list(
            session.scalars(select(ClientImage.state).where(ClientImage.batch_id == batch.id))
        )
        batch.succeeded_count = states.count("ready")
        batch.failed_count = sum(state in {"rejected", "cancelled", "deleted"} for state in states)
        if batch.state != "cancelled":
            finished = batch.succeeded_count + batch.failed_count
            if finished >= batch.total_count:
                batch.state = "partial" if batch.failed_count else "completed"
            elif finished:
                batch.state = "partial"
            else:
                batch.state = "active"
        batch.updated_at = utc_now()

    @staticmethod
    def _batch_response(batch: ClientImageBatch) -> ClientImageBatchResponse:
        return ClientImageBatchResponse(
            id=batch.id,
            client_batch_id=batch.client_batch_id,
            total_count=batch.total_count,
            total_bytes=batch.total_bytes,
            succeeded_count=batch.succeeded_count,
            failed_count=batch.failed_count,
            state=batch.state,
            created_at=batch.created_at,
            updated_at=batch.updated_at,
        )

    @staticmethod
    def _visibility_response(
        row: UserImageAdminVisibility | None,
    ) -> UserImageVisibilityResponse:
        if row is None:
            now = utc_now()
            return UserImageVisibilityResponse(
                enabled=False, revision=1, enabled_at=None, updated_at=now
            )
        return UserImageVisibilityResponse(
            enabled=row.enabled,
            revision=row.revision,
            enabled_at=row.enabled_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _ensure_actor_user(session: Session, actor: ActorContext) -> None:
        if session.get(AuthUser, actor.actor_id) is None:
            session.add(AuthUser(id=actor.actor_id, display_name=actor.actor_id))
            session.flush()

    @staticmethod
    def _temporary_object_key(upload: UploadSession) -> str | None:
        value = upload.temporary_key or ""
        prefix = "cos://"
        if not value.startswith(prefix):
            return None
        _, separator, key = value[len(prefix) :].partition("/")
        return key if separator and key else None

    @staticmethod
    def _assert_same_upload(image: ClientImage, request: ClientImageUploadCreate) -> None:
        supplied = (
            request.display_name,
            request.media_type,
            request.byte_size,
            request.content_md5,
            request.client_sha256,
            request.width,
            request.height,
            request.metadata_sanitized,
        )
        stored = (
            image.display_name,
            image.media_type,
            image.byte_size,
            image.content_md5,
            image.client_sha256,
            image.width,
            image.height,
            image.metadata_sanitized,
        )
        if supplied != stored:
            raise V2Conflict("client_item_id was reused with different image metadata")

    def _validate_delivery_count(self, image_ids: list[str]) -> None:
        if len(image_ids) > self.settings.client_image_max_thumbnail_deliveries:
            raise V2DomainError("too many thumbnail deliveries were requested")

    def _delete_best_effort(self, key: str) -> None:
        try:
            self.object_gateway.delete(key)
        except CosImageGatewayError:
            # COS lifecycle remains the second line of cleanup for temporary objects.
            pass

    @staticmethod
    def _download_name(image: ClientImage) -> str:
        safe = image.display_name.replace("/", "_").replace("\\", "_").strip(". ")
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[
            image.media_type
        ]
        accepted = (".jpg", ".jpeg") if image.media_type == "image/jpeg" else (extension,)
        if safe.lower().endswith(accepted):
            return safe
        for suffix in (".png", ".jpg", ".jpeg", ".webp"):
            if safe.lower().endswith(suffix):
                safe = safe[: -len(suffix)].rstrip(". ")
                break
        return f"{safe or image.id}{extension}"

    @staticmethod
    def _audit(
        session: Session,
        actor: ActorContext,
        operation: str,
        *,
        image_id: str | None = None,
        batch_id: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        session.add(
            ClientImageAuditEvent(
                owner_user_id=actor.actor_id,
                image_id=image_id,
                batch_id=batch_id,
                actor_id=actor.actor_id,
                device_id=actor.device_id,
                operation=operation,
                details_json=details or {},
            )
        )
