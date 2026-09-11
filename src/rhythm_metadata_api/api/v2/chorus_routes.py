from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response

from rhythm_metadata_api.api.v2.routes import (
    Actor,
    model_response,
    require_idempotency,
    require_if_match,
    stored_response,
)
from rhythm_metadata_api.application.chorus_service import ChorusService
from rhythm_metadata_api.domain.v2.chorus import (
    ChorusMixResolveRequest,
    ChorusModerationRequest,
    ChorusProjectCreate,
    ChorusTrackAlignmentPatch,
    ChorusTrackCreate,
)

router = APIRouter(prefix="/v2", tags=["online chorus"])


def chorus(request: Request) -> ChorusService:
    return request.app.state.v2_container.chorus


Chorus = Annotated[ChorusService, Depends(chorus)]


@router.get("/works/{work_id}/chorus")
def list_work_chorus(work_id: str, service: Chorus, actor: Actor) -> Response:
    return model_response(service.list_for_work(work_id, actor))


@router.post("/works/{work_id}/chorus-projects")
def create_chorus_project(
    work_id: str,
    body: ChorusProjectCreate,
    service: Chorus,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.create_project(work_id, body, require_idempotency(idempotency_key), actor)
    )


@router.get("/chorus-projects/{project_id}")
def get_chorus_project(project_id: str, service: Chorus, actor: Actor) -> Response:
    item = service.get_project(project_id, actor)
    return model_response(item, headers={"ETag": f'"rev-{item.revision}"'})


@router.post("/chorus-projects/{project_id}/tracks")
def create_chorus_track(
    project_id: str,
    body: ChorusTrackCreate,
    service: Chorus,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.create_track(project_id, body, require_idempotency(idempotency_key), actor)
    )


@router.put("/chorus-tracks/{track_id}/content")
async def upload_chorus_track_content(
    track_id: str,
    request: Request,
    service: Chorus,
    actor: Actor,
) -> Response:
    return model_response(await service.write_track_content(track_id, request.stream(), actor))


@router.post("/chorus-tracks/{track_id}/complete")
def complete_chorus_track(
    track_id: str,
    background_tasks: BackgroundTasks,
    service: Chorus,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    result = service.complete_track(track_id, require_idempotency(idempotency_key), actor)
    background_tasks.add_task(service.process_track, track_id)
    return stored_response(result)


@router.patch("/chorus-tracks/{track_id}/alignment")
def update_chorus_track_alignment(
    track_id: str,
    body: ChorusTrackAlignmentPatch,
    service: Chorus,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    item = service.update_alignment(track_id, body, require_if_match(if_match), actor)
    return model_response(item, headers={"ETag": f'"rev-{item.revision}"'})


@router.post("/chorus-tracks/{track_id}/submit")
def submit_chorus_track(
    track_id: str,
    service: Chorus,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    return stored_response(
        service.submit_track(track_id, require_idempotency(idempotency_key), actor)
    )


@router.patch("/chorus-tracks/{track_id}/moderation")
def moderate_chorus_track(
    track_id: str,
    body: ChorusModerationRequest,
    background_tasks: BackgroundTasks,
    service: Chorus,
    actor: Actor,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    item = service.moderate_track(track_id, body, require_if_match(if_match), actor)
    if item.status == "published":
        background_tasks.add_task(_prepare_default_mix, service, item.chorus_project_id)
    return model_response(item, headers={"ETag": f'"rev-{item.revision}"'})


def _prepare_default_mix(service: ChorusService, project_id: str) -> None:
    mix_id = service.resolve_default_mix(project_id)
    if mix_id is not None:
        service.render_mix(mix_id)


@router.delete("/chorus-tracks/{track_id}")
def withdraw_chorus_track(track_id: str, service: Chorus, actor: Actor) -> Response:
    item = service.withdraw_track(track_id, actor)
    return model_response(item, headers={"ETag": f'"rev-{item.revision}"'})


@router.post("/chorus-projects/{project_id}/mixes:resolve")
def resolve_chorus_mix(
    project_id: str,
    body: ChorusMixResolveRequest,
    background_tasks: BackgroundTasks,
    service: Chorus,
    actor: Actor,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    result = service.resolve_mix(project_id, body, require_idempotency(idempotency_key), actor)
    if result.body.get("state") in {"queued", "failed"}:
        background_tasks.add_task(service.render_mix, str(result.body["id"]))
    return stored_response(result)


@router.get("/chorus-mixes/{mix_id}")
def get_chorus_mix(mix_id: str, service: Chorus, _: Actor) -> Response:
    return model_response(service.get_mix(mix_id))
