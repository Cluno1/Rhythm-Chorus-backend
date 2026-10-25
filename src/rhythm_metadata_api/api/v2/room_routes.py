"""issue 124：房间接口。读对所有登记设备开放；写需要 scope ``rooms:write``。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from rhythm_metadata_api.api.v2.routes import (
    Actor,
    model_response,
    require_idempotency,
    require_if_match,
    stored_response,
)
from rhythm_metadata_api.application.catalog_service import etag
from rhythm_metadata_api.application.room_service import RoomService
from rhythm_metadata_api.domain.v2.rooms import RoomCreate, RoomItemCreate, RoomOrderUpdate

router = APIRouter(prefix="/v2", tags=["rooms"])


def rooms(request: Request) -> RoomService:
    return request.app.state.v2_container.rooms


Rooms = Annotated[RoomService, Depends(rooms)]


@router.get("/rooms")
def list_rooms(
    service: Rooms,
    actor: Actor,
    work_id: Annotated[str | None, Query(max_length=36)] = None,
) -> Response:
    return model_response(service.list_rooms(actor, work_id), headers={"Cache-Control": "no-store"})


@router.post("/rooms")
def create_room(
    body: RoomCreate,
    service: Rooms,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(service.create_room(body, require_idempotency(idempotency_key), actor))


@router.get("/rooms/{room_id}")
def get_room(room_id: str, service: Rooms, actor: Actor) -> Response:
    room = service.get_room(room_id, actor)
    return model_response(room, headers={"ETag": etag(room.revision), "Cache-Control": "no-store"})


@router.delete("/rooms/{room_id}", status_code=204)
def delete_room(room_id: str, service: Rooms, actor: Actor) -> Response:
    service.delete_room(room_id, actor)
    return Response(status_code=204)


@router.post("/rooms/{room_id}/items")
def add_room_item(
    room_id: str,
    body: RoomItemCreate,
    service: Rooms,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.add_item(room_id, body, require_idempotency(idempotency_key), actor)
    )


@router.delete("/rooms/{room_id}/items/{item_id}")
def remove_room_item(room_id: str, item_id: str, service: Rooms, actor: Actor) -> Response:
    room = service.remove_item(room_id, item_id, actor)
    return model_response(room, headers={"ETag": etag(room.revision)})


@router.put("/rooms/{room_id}/order")
def reorder_room(
    room_id: str,
    body: RoomOrderUpdate,
    service: Rooms,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    room = service.reorder(room_id, body, require_if_match(if_match), actor)
    return model_response(room, headers={"ETag": etag(room.revision)})
