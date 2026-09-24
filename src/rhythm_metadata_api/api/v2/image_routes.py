from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from rhythm_metadata_api.api.public_auth import admin_actor_context
from rhythm_metadata_api.api.v2.routes import (
    Actor,
    model_response,
    require_idempotency,
    require_if_match,
)
from rhythm_metadata_api.application.catalog_service import ActorContext, etag
from rhythm_metadata_api.application.client_image_service import ClientImageService
from rhythm_metadata_api.application.device_auth import DeviceAuthError
from rhythm_metadata_api.domain.v2.images import (
    ClientImageBatchCreate,
    ClientImageBulkDeleteRequest,
    ClientImageBulkDeleteResponse,
    ClientImageDeleteItem,
    ClientImageListResponse,
    ClientImageUploadCreate,
    ThumbnailDeliveryRequest,
    UserImageVisibilityPatch,
)

router = APIRouter(prefix="/v2/labs", tags=["client images"])
admin_router = APIRouter(prefix="/v2/admin/shared-images", tags=["shared client images"])


def image_service(request: Request) -> ClientImageService:
    return request.app.state.v2_container.client_images


Images = Annotated[ClientImageService, Depends(image_service)]


def shared_admin_context(
    request: Request,
    actor: Annotated[ActorContext, Depends(admin_actor_context)],
    authorization: Annotated[str | None, Header()] = None,
) -> ActorContext:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() == "device":
        try:
            request.app.state.device_auth.require_scope(token, "image:read-shared")
        except DeviceAuthError as error:
            from fastapi import HTTPException

            raise HTTPException(error.status_code, error.detail) from error
    return actor


SharedAdmin = Annotated[ActorContext, Depends(shared_admin_context)]


@router.get("/images/capabilities")
def capabilities(service: Images, _: Actor):
    return service.capabilities()


@router.post("/image-upload-batches", status_code=201)
def create_batch(
    body: ClientImageBatchCreate,
    service: Images,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require_idempotency(idempotency_key)
    return service.create_batch(body, actor)


@router.get("/image-upload-batches/{batch_id}")
def get_batch(batch_id: str, service: Images, actor: Actor):
    return service.get_batch(batch_id, actor)


@router.post("/image-upload-batches/{batch_id}/cancel")
def cancel_batch(
    batch_id: str,
    service: Images,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require_idempotency(idempotency_key)
    return service.cancel_batch(batch_id, actor)


@router.post("/image-uploads", status_code=201)
def create_upload(
    body: ClientImageUploadCreate,
    service: Images,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require_idempotency(idempotency_key)
    return service.create_upload(body, actor)


@router.post("/image-uploads/{upload_id}/refresh")
def refresh_upload(
    upload_id: str,
    service: Images,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require_idempotency(idempotency_key)
    return service.refresh_upload(upload_id, actor)


@router.post("/image-uploads/{upload_id}/complete")
def complete_upload(
    upload_id: str,
    service: Images,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require_idempotency(idempotency_key)
    return service.complete_upload(upload_id, actor)


@router.post("/image-uploads/{upload_id}/cancel")
def cancel_upload(
    upload_id: str,
    service: Images,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require_idempotency(idempotency_key)
    return service.cancel_upload(upload_id, actor)


@router.get("/images/admin-visibility")
def get_visibility(service: Images, actor: Actor) -> Response:
    item = service.visibility(actor)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.patch("/images/admin-visibility")
def update_visibility(
    body: UserImageVisibilityPatch,
    service: Images,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    require_idempotency(idempotency_key)
    item = service.update_visibility(body.enabled, require_if_match(if_match), actor)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.post("/images/thumbnail-deliveries")
def own_thumbnail_deliveries(
    body: ThumbnailDeliveryRequest, service: Images, actor: Actor
):
    return service.own_thumbnail_deliveries(body, actor)


@router.post("/images:batch-delete")
def bulk_delete(
    body: ClientImageBulkDeleteRequest,
    service: Images,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientImageBulkDeleteResponse:
    require_idempotency(idempotency_key)
    return ClientImageBulkDeleteResponse(
        items=[
            ClientImageDeleteItem(
                image_id=image_id,
                result=service.delete_own(image_id, actor),
            )
            for image_id in body.image_ids
        ]
    )


@router.get("/images", response_model=ClientImageListResponse)
def list_own_images(
    service: Images,
    actor: Actor,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> ClientImageListResponse:
    items, next_cursor = service.list_own(actor, cursor, limit)
    return ClientImageListResponse(items=items, next_cursor=next_cursor)


@router.get("/images/{image_id}")
def get_own_image(image_id: str, service: Images, actor: Actor) -> Response:
    item = service.get_own(image_id, actor)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.get("/images/{image_id}/delivery")
def own_delivery(
    image_id: str,
    service: Images,
    actor: Actor,
    purpose: Annotated[Literal["thumbnail", "preview", "download"], Query()] = "thumbnail",
):
    return service.own_delivery(image_id, purpose, actor)


@router.delete("/images/{image_id}")
def delete_own_image(
    image_id: str,
    service: Images,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    require_idempotency(idempotency_key)
    return ClientImageDeleteItem(image_id=image_id, result=service.delete_own(image_id, actor))


@admin_router.get("")
def list_shared_images(
    service: Images,
    _: SharedAdmin,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    owner_user_id: str | None = None,
    image_format: Literal["png", "jpeg", "webp"] | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> ClientImageListResponse:
    if created_from and created_to and created_from > created_to:
        from fastapi import HTTPException

        raise HTTPException(422, "created_from must not be after created_to")
    items, next_cursor = service.list_shared(
        cursor,
        limit,
        owner_user_id,
        image_format,
        created_from,
        created_to,
    )
    return ClientImageListResponse(items=items, next_cursor=next_cursor)


@admin_router.post("/thumbnail-deliveries")
def shared_thumbnail_deliveries(
    body: ThumbnailDeliveryRequest, service: Images, _: SharedAdmin
):
    return service.shared_thumbnail_deliveries(body)


@admin_router.get("/{image_id}")
def get_shared_image(image_id: str, service: Images, _: SharedAdmin):
    return service.shared_detail(image_id)


@admin_router.get("/{image_id}/delivery")
def shared_delivery(
    image_id: str,
    service: Images,
    _: SharedAdmin,
    variant: Annotated[
        Literal["thumbnail_512", "preview_2048", "original"], Query()
    ] = "preview_2048",
):
    return service.shared_delivery(image_id, variant)
