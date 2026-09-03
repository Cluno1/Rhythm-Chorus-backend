from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from rhythm_metadata_api.api.routes import health, tracks
from rhythm_metadata_api.api.v2.routes import router as v2_router
from rhythm_metadata_api.application.container import V2Container
from rhythm_metadata_api.core.config import Settings, get_settings
from rhythm_metadata_api.domain.v2.errors import V2DomainError
from rhythm_metadata_api.infrastructure.storage.base import (
    EmptyUploadError,
    UploadTooLargeError,
    UploadValidationError,
)


def problem_response(
    request: Request,
    status_code: int,
    problem_type: str,
    title: str,
    detail: str,
    **extensions: object,
) -> JSONResponse:
    return JSONResponse(
        {
            "type": f"https://rhythm.invalid/problems/{problem_type}",
            "title": title,
            "status": status_code,
            "detail": detail,
            "instance": str(request.url.path),
            **extensions,
        },
        status_code=status_code,
        media_type="application/problem+json",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        container = V2Container.build(resolved_settings)
        lifespan_app.state.v2_container = container
        try:
            yield
        finally:
            container.close()

    app = FastAPI(
        title="Rhythm Metadata API",
        version="0.3.0",
        description="Private Work, Arrangement, Score, Rendition, and Asset API for Rhythm.",
        lifespan=lifespan,
    )
    app.dependency_overrides[get_settings] = lambda: resolved_settings

    @app.exception_handler(V2DomainError)
    async def domain_error(request: Request, error: V2DomainError) -> JSONResponse:
        return problem_response(
            request,
            error.status_code,
            error.problem_type,
            error.title,
            error.detail,
            **error.extensions,
        )

    @app.exception_handler(UploadValidationError)
    async def invalid_upload(request: Request, error: UploadValidationError) -> JSONResponse:
        return problem_response(
            request, 422, "invalid-upload", "Invalid uploaded asset", str(error)
        )

    @app.exception_handler(EmptyUploadError)
    async def empty_upload(request: Request, error: EmptyUploadError) -> JSONResponse:
        return problem_response(request, 400, "empty-upload", "Empty upload", str(error))

    @app.exception_handler(UploadTooLargeError)
    async def large_upload(request: Request, error: UploadTooLargeError) -> JSONResponse:
        return problem_response(request, 413, "upload-too-large", "Upload is too large", str(error))

    @app.exception_handler(RequestValidationError)
    async def request_validation(request: Request, error: RequestValidationError) -> JSONResponse:
        return problem_response(
            request,
            422,
            "request-validation",
            "Request validation failed",
            "One or more request values are invalid",
            errors=error.errors(),
        )

    app.include_router(health.router)
    app.include_router(tracks.router, prefix="/v1")
    app.include_router(v2_router)
    return app


app = create_app()
