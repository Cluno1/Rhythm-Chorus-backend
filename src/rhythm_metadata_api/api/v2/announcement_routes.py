"""issue 154: announcements. Enrolled devices read published ones; administrators manage everything."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from rhythm_metadata_api.api.public_auth import AdminActor
from rhythm_metadata_api.api.v2.routes import Actor, model_response
from rhythm_metadata_api.application.announcement_service import (
    MAX_IMAGE_BYTES,
    AnnouncementImageTooLarge,
    AnnouncementService,
)
from rhythm_metadata_api.domain.v2.announcements import (
    AnnouncementPatch,
    AnnouncementTransition,
    AnnouncementWrite,
)

router = APIRouter(prefix="/v2", tags=["announcements"])
admin_router = APIRouter(prefix="/v2/admin", tags=["announcement administration"])


def announcements(request: Request) -> AnnouncementService:
    return request.app.state.v2_container.announcements


Announcements = Annotated[AnnouncementService, Depends(announcements)]
_NO_STORE = {"Cache-Control": "no-store"}


@router.get("/announcements")
def list_announcements(
    service: Announcements,
    _: Actor,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    cursor: Annotated[str | None, Query(max_length=300)] = None,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    result = service.list_published(limit=limit, cursor=cursor)
    tag = '"' + hashlib.sha256(result.model_dump_json().encode()).hexdigest()[:32] + '"'
    headers = {"ETag": tag, "Cache-Control": "no-cache"}
    if if_none_match == tag:
        return Response(status_code=304, headers=headers)
    return model_response(result, headers=headers)


@router.get("/announcements/images/{name}")
def announcement_image(name: str, service: Announcements) -> Response:
    # Open like /v2/music/images: the content-addressed name is the capability, and image loaders send no token.
    return RedirectResponse(
        service.image_url(name), status_code=307,
        headers={"Cache-Control": "public, max-age=300", "Referrer-Policy": "no-referrer"},
    )


@router.get("/announcements/{announcement_id}")
def get_announcement(announcement_id: str, service: Announcements, _: Actor) -> Response:
    return model_response(service.get_published(announcement_id), headers={"Cache-Control": "no-cache"})


@admin_router.get("/announcements")
def admin_list(
    service: Announcements,
    _: AdminActor,
    status: Annotated[Literal["draft", "published", "archived"] | None, Query()] = None,
) -> Response:
    return model_response(service.list_admin(status=status), headers=_NO_STORE)


@admin_router.post("/announcements", status_code=201)
def admin_create(body: AnnouncementWrite, service: Announcements, actor: AdminActor) -> Response:
    return model_response(service.create(body, actor.actor_id), status_code=201, headers=_NO_STORE)


@admin_router.post("/announcement-images", status_code=201)
async def admin_upload_image(
    request: Request,
    service: Announcements,
    actor: AdminActor,
    content_type: Annotated[str, Header(alias="Content-Type")] = "",
) -> Response:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_IMAGE_BYTES:
            raise AnnouncementImageTooLarge("announcement image must not exceed 10 MB")
        chunks.append(chunk)
    result = await run_in_threadpool(service.upload_image, b"".join(chunks), content_type, actor.actor_id)
    return model_response(result, status_code=201, headers=_NO_STORE)


@admin_router.get("/announcements/{announcement_id}")
def admin_get(announcement_id: str, service: Announcements, _: AdminActor) -> Response:
    return model_response(service.get_admin(announcement_id), headers=_NO_STORE)


@admin_router.patch("/announcements/{announcement_id}")
def admin_update(
    announcement_id: str, body: AnnouncementPatch, service: Announcements, actor: AdminActor,
) -> Response:
    return model_response(service.update(announcement_id, body, actor.actor_id), headers=_NO_STORE)


@admin_router.post("/announcements/{announcement_id}/publish")
def admin_publish(
    announcement_id: str, body: AnnouncementTransition, service: Announcements, actor: AdminActor,
) -> Response:
    return model_response(service.publish(announcement_id, body.revision, actor.actor_id), headers=_NO_STORE)


@admin_router.post("/announcements/{announcement_id}/archive")
def admin_archive(
    announcement_id: str, body: AnnouncementTransition, service: Announcements, actor: AdminActor,
) -> Response:
    return model_response(service.archive(announcement_id, body.revision, actor.actor_id), headers=_NO_STORE)


@admin_router.delete("/announcements/{announcement_id}", status_code=204)
def admin_delete(
    announcement_id: str,
    service: Announcements,
    actor: AdminActor,
    revision: Annotated[int, Query(ge=1)],
) -> Response:
    service.delete(announcement_id, revision, actor.actor_id)
    return Response(status_code=204)
