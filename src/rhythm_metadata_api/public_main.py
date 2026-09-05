from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from rhythm_metadata_api.api.public_auth import router as public_auth_router
from rhythm_metadata_api.api.routes import health
from rhythm_metadata_api.api.v2.routes import actor_context
from rhythm_metadata_api.api.v2.routes import router as v2_router
from rhythm_metadata_api.application.catalog_service import ActorContext
from rhythm_metadata_api.application.container import V2Container
from rhythm_metadata_api.application.device_auth import DeviceAuthError, DeviceAuthService
from rhythm_metadata_api.core.config import Settings, get_settings
from rhythm_metadata_api.domain.v2.errors import V2DomainError
from rhythm_metadata_api.main import problem_response

_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_PUBLIC_READ_ROUTES = (
    ("GET", re.compile(r"^/v2/works$")),
    ("GET", re.compile(r"^/v2/library/songs$")),
    ("GET", re.compile(r"^/v2/library/albums$")),
    ("GET", re.compile(r"^/v2/library/albums/[^/]+$")),
    ("GET", re.compile(r"^/v2/works/[^/]+/bundle$")),
    ("GET", re.compile(r"^/v2/score-revisions/[^/]+$")),
    ("GET", re.compile(r"^/v2/renditions/[^/]+/playback$")),
    ("GET", re.compile(r"^/v2/assets/[^/]+/delivery$")),
    ("GET", re.compile(r"^/v2/assets/[^/]+/content$")),
    ("HEAD", re.compile(r"^/v2/assets/[^/]+/content$")),
    ("GET", re.compile(r"^/v2/sync/changes$")),
)


def _public_read_allowed(method: str, path: str) -> bool:
    return any(method == expected and pattern.fullmatch(path) for expected, pattern in _PUBLIC_READ_ROUTES)


def _device_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "device" or not token:
        raise HTTPException(401, "Device authorization is required")
    return token


def public_actor_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    device_id: Annotated[str | None, Header(alias="X-Rhythm-Device-ID")] = None,
    timestamp: Annotated[int | None, Header(alias="X-Rhythm-Timestamp")] = None,
    nonce: Annotated[str | None, Header(alias="X-Rhythm-Nonce")] = None,
    content_sha256: Annotated[str | None, Header(alias="X-Rhythm-Content-SHA256")] = None,
    signature: Annotated[str | None, Header(alias="X-Rhythm-Signature")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> ActorContext:
    if not _public_read_allowed(request.method, request.url.path):
        raise HTTPException(404, "not found")
    if None in (device_id, timestamp, nonce, content_sha256, signature):
        raise HTTPException(401, "device proof headers are required")
    if request.method in ("GET", "HEAD") and content_sha256.lower() != _EMPTY_SHA256:
        raise HTTPException(401, "GET and HEAD requests must use the empty content hash")
    try:
        principal = request.app.state.device_auth.authenticate_request(
            _device_token(authorization),
            device_id,
            timestamp,
            nonce,
            content_sha256,
            signature,
            request.method,
            request.url.path,
            request.scope.get("query_string", b"").decode("ascii"),
        )
    except (DeviceAuthError, UnicodeDecodeError) as error:
        if isinstance(error, DeviceAuthError):
            raise HTTPException(error.status_code, error.detail) from error
        raise HTTPException(400, "query string must be ASCII percent-encoded") from error
    return ActorContext(device_id=principal.device_id, request_id=request_id)


def create_public_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    if len(resolved.public_token_secret.encode()) < 32:
        raise ValueError("RHYTHM_PUBLIC_TOKEN_SECRET must contain at least 32 bytes")
    if not resolved.public_admin_password_hash.startswith("scrypt$"):
        raise ValueError("RHYTHM_PUBLIC_ADMIN_PASSWORD_HASH must be an encoded scrypt hash")

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        container = V2Container.build(resolved)
        lifespan_app.state.v2_container = container
        lifespan_app.state.device_auth = DeviceAuthService(container.engine, resolved)
        try:
            yield
        finally:
            container.close()

    app = FastAPI(
        title="Rhythm Public Catalog Gateway",
        version="0.4.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.dependency_overrides[get_settings] = lambda: resolved
    app.dependency_overrides[actor_context] = public_actor_context

    @app.middleware("http")
    async def public_route_allowlist(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if (
            path.startswith("/v2/")
            and not path.startswith(("/v2/admin/", "/v2/device/"))
            and not _public_read_allowed(request.method, path)
        ):
            return JSONResponse({"detail": "not found"}, status_code=404)
        return await call_next(request)

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
    app.include_router(public_auth_router)
    app.include_router(v2_router)
    return app
