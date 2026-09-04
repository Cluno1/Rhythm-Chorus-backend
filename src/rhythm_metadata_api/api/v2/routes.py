from __future__ import annotations

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse

from rhythm_metadata_api.application.catalog_service import (
    ActorContext,
    CatalogService,
    StoredResponse,
    bundle_etag,
    etag,
    parse_etag,
)
from rhythm_metadata_api.domain.v2.schemas import (
    ArrangementCreate,
    ArrangementPatch,
    ContributorCreate,
    PartInput,
    RenditionAssetInput,
    RenditionCreate,
    RenditionPatch,
    ScoreCreate,
    ScorePatch,
    ScoreRevisionCreate,
    UploadCreate,
    WorkCreate,
    WorkPatch,
    WorkResolveRequest,
)

router = APIRouter(prefix="/v2", tags=["work catalog v2"])


def actor_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    device_id: Annotated[str | None, Header(alias="X-Device-ID")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> ActorContext:
    expected = request.app.state.v2_container.settings.bootstrap_token
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied or not secrets.compare_digest(supplied, expected):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return ActorContext(device_id=device_id, request_id=request_id)


Actor = Annotated[ActorContext, Depends(actor_context)]


def catalog(request: Request) -> CatalogService:
    return request.app.state.v2_container.catalog


Catalog = Annotated[CatalogService, Depends(catalog)]


def require_idempotency(value: str | None) -> str:
    if value is None:
        from rhythm_metadata_api.domain.v2.errors import V2DomainError

        raise V2DomainError("Idempotency-Key is required")
    return value


def require_if_match(value: str | None) -> int:
    if value is None:
        from rhythm_metadata_api.domain.v2.errors import V2DomainError

        raise V2DomainError("If-Match is required")
    return parse_etag(value)


def stored_response(result: StoredResponse) -> JSONResponse:
    headers = dict(result.headers)
    if result.replayed:
        headers["Idempotency-Replayed"] = "true"
    return JSONResponse(result.body, status_code=result.status_code, headers=headers)


def model_response(
    model: Any, *, status_code: int = 200, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(model.model_dump(mode="json"), status_code=status_code, headers=headers)


@router.post("/contributors")
def create_contributor(
    body: ContributorCreate,
    service: Catalog,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.create_contributor(body, require_idempotency(idempotency_key), actor)
    )


@router.get("/contributors/{contributor_id}")
def get_contributor(contributor_id: str, service: Catalog, _: Actor) -> Response:
    item = service.get_contributor(contributor_id)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.post("/works/resolve")
def resolve_work(body: WorkResolveRequest, service: Catalog, _: Actor) -> Response:
    return model_response(service.resolve_work(body))


@router.post("/works")
def create_work(
    body: WorkCreate,
    service: Catalog,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(service.create_work(body, require_idempotency(idempotency_key), actor))


@router.get("/works")
def list_works(
    service: Catalog,
    _: Actor,
    q: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, next_cursor = service.list_works(q, cursor, limit)
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/works/{work_id}")
def get_work(work_id: str, service: Catalog, _: Actor) -> Response:
    item = service.get_work(work_id)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.patch("/works/{work_id}")
def patch_work(
    work_id: str,
    body: WorkPatch,
    service: Catalog,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    item = service.patch_work(work_id, body, require_if_match(if_match), actor)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.get("/works/{work_id}/bundle")
def get_work_bundle(
    work_id: str,
    service: Catalog,
    _: Actor,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    version = service.bundle_version(work_id)
    current = bundle_etag(work_id, version)
    if if_none_match == current:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": current})
    return model_response(service.work_bundle(work_id), headers={"ETag": current})


@router.post("/works/{work_id}/arrangements")
def create_arrangement(
    work_id: str,
    body: ArrangementCreate,
    service: Catalog,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.create_arrangement(work_id, body, require_idempotency(idempotency_key), actor)
    )


@router.get("/arrangements/{arrangement_id}")
def get_arrangement(arrangement_id: str, service: Catalog, _: Actor) -> Response:
    item = service.get_arrangement(arrangement_id)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.patch("/arrangements/{arrangement_id}")
def patch_arrangement(
    arrangement_id: str,
    body: ArrangementPatch,
    service: Catalog,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    item = service.patch_arrangement(arrangement_id, body, require_if_match(if_match), actor)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.post("/arrangements/{arrangement_id}/parts")
def add_part(
    arrangement_id: str,
    body: PartInput,
    service: Catalog,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.add_part(
            arrangement_id,
            body,
            require_if_match(if_match),
            require_idempotency(idempotency_key),
            actor,
        )
    )


@router.post("/uploads")
def create_upload(
    body: UploadCreate,
    service: Catalog,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(service.create_upload(body, require_idempotency(idempotency_key), actor))


@router.put("/uploads/{upload_id}/content")
async def write_upload(upload_id: str, request: Request, service: Catalog, _: Actor) -> Response:
    return model_response(await service.write_upload(upload_id, request.stream()))


@router.post("/uploads/{upload_id}/complete")
def complete_upload(
    upload_id: str,
    service: Catalog,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.complete_upload(
            upload_id,
            require_idempotency(idempotency_key),
            actor,
        )
    )


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str, service: Catalog, _: Actor) -> Response:
    return model_response(service.get_upload(upload_id))


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, service: Catalog, _: Actor) -> Response:
    item = service.get_asset(asset_id)
    return model_response(item, headers={"ETag": f'"sha256:{item.sha256}"'})


@router.get("/assets/{asset_id}/delivery")
def get_asset_delivery(asset_id: str, service: Catalog, _: Actor) -> Response:
    return model_response(service.asset_delivery(asset_id))


@router.get("/assets/{asset_id}/content")
def get_asset_content(asset_id: str, service: Catalog, _: Actor) -> Response:
    path, asset = service.asset_content(asset_id)
    return FileResponse(
        path,
        media_type=asset.detected_media_type,
        filename=None,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, immutable",
            "ETag": f'"sha256:{asset.sha256}"',
        },
    )


@router.head("/assets/{asset_id}/content", include_in_schema=False)
def head_asset_content(asset_id: str, service: Catalog, _: Actor) -> Response:
    return get_asset_content(asset_id, service, _)


@router.post("/arrangements/{arrangement_id}/scores")
def create_score(
    arrangement_id: str,
    body: ScoreCreate,
    service: Catalog,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.create_score(arrangement_id, body, require_idempotency(idempotency_key), actor)
    )


@router.get("/scores/{score_id}")
def get_score(score_id: str, service: Catalog, _: Actor) -> Response:
    item = service.get_score(score_id)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.patch("/scores/{score_id}")
def patch_score(
    score_id: str,
    body: ScorePatch,
    service: Catalog,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    item = service.patch_score(score_id, body, require_if_match(if_match), actor)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.post("/scores/{score_id}/revisions")
def create_score_revision(
    score_id: str,
    body: ScoreRevisionCreate,
    service: Catalog,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.create_score_revision(
            score_id,
            body,
            require_if_match(if_match),
            require_idempotency(idempotency_key),
            actor,
        )
    )


@router.get("/score-revisions/{revision_id}")
def get_score_revision(revision_id: str, service: Catalog, _: Actor) -> Response:
    return model_response(service.get_score_revision(revision_id))


@router.post("/arrangements/{arrangement_id}/renditions")
def create_rendition(
    arrangement_id: str,
    body: RenditionCreate,
    service: Catalog,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.create_rendition(arrangement_id, body, require_idempotency(idempotency_key), actor)
    )


@router.get("/renditions/{rendition_id}")
def get_rendition(rendition_id: str, service: Catalog, _: Actor) -> Response:
    item = service.get_rendition(rendition_id)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.patch("/renditions/{rendition_id}")
def patch_rendition(
    rendition_id: str,
    body: RenditionPatch,
    service: Catalog,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    item = service.patch_rendition(rendition_id, body, require_if_match(if_match), actor)
    return model_response(item, headers={"ETag": etag(item.revision)})


@router.post("/renditions/{rendition_id}/assets")
def add_rendition_asset(
    rendition_id: str,
    body: RenditionAssetInput,
    service: Catalog,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.add_rendition_asset(
            rendition_id,
            body,
            require_if_match(if_match),
            require_idempotency(idempotency_key),
            actor,
        )
    )


@router.get("/renditions/{rendition_id}/playback")
def get_playback(
    rendition_id: str,
    service: Catalog,
    _: Actor,
    prefer: str | None = None,
) -> Response:
    return model_response(service.playback(rendition_id, prefer))


@router.get("/library/songs")
def list_library_songs(
    service: Catalog,
    _: Actor,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, next_cursor = service.list_library_songs(cursor, limit)
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/library/albums")
def list_library_albums(
    service: Catalog,
    _: Actor,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, next_cursor = service.list_library_albums(cursor, limit)
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "next_cursor": next_cursor,
    }


@router.get("/library/albums/{album_id}")
def get_library_album(album_id: str, service: Catalog, _: Actor) -> Response:
    return model_response(service.get_library_album(album_id))


@router.get("/sync/changes")
def get_changes(
    service: Catalog,
    _: Actor,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Response:
    return model_response(service.changes(after, limit))
