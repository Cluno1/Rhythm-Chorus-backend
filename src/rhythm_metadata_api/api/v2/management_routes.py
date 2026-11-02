from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from rhythm_metadata_api.api.v2.routes import Actor, Catalog
from rhythm_metadata_api.application.overview_service import OverviewService
from rhythm_metadata_api.application.pending_center import PendingCenterService

router = APIRouter(prefix="/v2", tags=["management"])


@router.get("/management/overview")
def management_overview(service: Catalog, _: Actor) -> Response:
    payload = OverviewService(service.uow_factory).summary()
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})


@router.get("/management/pending")
def management_pending(service: Catalog, _: Actor) -> Response:
    payload = PendingCenterService(service.uow_factory).summary().model_dump(mode="json")
    return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})
