from fastapi import APIRouter, HTTPException, status

from rhythm_metadata_api.api.routes.tracks import repository

router = APIRouter(tags=["system"])


@router.get("/healthz")
def health() -> dict[str, str]:
    if not repository.ping():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable"
        )
    return {"status": "ok", "database": "sqlite"}
