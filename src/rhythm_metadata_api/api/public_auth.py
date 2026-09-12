from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request

from rhythm_metadata_api.api.v2.chorus_routes import _prepare_default_mix
from rhythm_metadata_api.api.v2.routes import model_response, require_if_match
from rhythm_metadata_api.application.catalog_service import ActorContext
from rhythm_metadata_api.application.chorus_service import ChorusService
from rhythm_metadata_api.application.device_auth import DeviceAuthError, DeviceAuthService
from rhythm_metadata_api.domain.auth_schemas import (
    AdminDeviceListResponse,
    AdminDeviceResponse,
    AdministratorChangeResponse,
    AdminSessionRequest,
    AdminSessionResponse,
    DeviceEnrollRequest,
    DeviceNonceRequest,
    DeviceRefreshRequest,
    DeviceSessionResponse,
    DeviceStatusResponse,
    EnrollmentChallengeRequest,
    InviteCreateRequest,
    InviteCreateResponse,
    NonceResponse,
    RevokeResponse,
)
from rhythm_metadata_api.domain.v2.chorus import (
    ChorusModerationRequest,
    ChorusModerationSettingsPatch,
    ChorusModerationSettingsResponse,
)

router = APIRouter(prefix="/v2", tags=["public device authentication"])


def service(request: Request) -> DeviceAuthService:
    return request.app.state.device_auth


def _token(authorization: str | None, scheme: str) -> str:
    supplied_scheme, _, token = (authorization or "").partition(" ")
    if supplied_scheme.lower() != scheme.lower() or not token:
        raise HTTPException(401, f"{scheme} authorization is required")
    return token


def _raise(error: DeviceAuthError) -> None:
    raise HTTPException(error.status_code, error.detail) from error


def _source_ip(request: Request) -> str | None:
    # Deliberately do not trust X-Forwarded-For on the directly exposed listener.
    return request.client.host if request.client else None


def admin_actor_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    rhythm_device_id: Annotated[str | None, Header(alias="X-Rhythm-Device-ID")] = None,
    timestamp: Annotated[int | None, Header(alias="X-Rhythm-Timestamp")] = None,
    nonce: Annotated[str | None, Header(alias="X-Rhythm-Nonce")] = None,
    content_sha256: Annotated[str | None, Header(alias="X-Rhythm-Content-SHA256")] = None,
    signature: Annotated[str | None, Header(alias="X-Rhythm-Signature")] = None,
    request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> ActorContext:
    auth = service(request)
    scheme, _, token = (authorization or "").partition(" ")
    try:
        if scheme.lower() == "bearer" and token:
            return ActorContext(actor_id=auth.require_admin(token), request_id=request_id)
        if scheme.lower() != "device" or not token:
            raise DeviceAuthError(401, "administrator authorization is required")
        if None in (rhythm_device_id, timestamp, nonce, content_sha256, signature):
            raise DeviceAuthError(401, "device proof headers are required")
        expected_hash = (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            if request.method in {"GET", "HEAD"}
            else getattr(request.state, "content_sha256", None)
        )
        if expected_hash is None or content_sha256.lower() != expected_hash:
            raise DeviceAuthError(401, "request content hash does not match the body")
        principal = auth.authenticate_request(
            token,
            rhythm_device_id,
            timestamp,
            nonce,
            content_sha256,
            signature,
            request.method,
            request.url.path,
            request.scope.get("query_string", b"").decode("ascii"),
        )
        auth.require_administrator(principal)
        return ActorContext(
            actor_id=principal.user_id,
            device_id=principal.device_id,
            request_id=request_id,
        )
    except UnicodeDecodeError:
        raise HTTPException(400, "query string must be ASCII percent-encoded") from None
    except DeviceAuthError as error:
        _raise(error)


AdminActor = Annotated[ActorContext, Depends(admin_actor_context)]


def chorus_service(request: Request) -> ChorusService:
    return request.app.state.v2_container.chorus


@router.post("/admin/session", response_model=AdminSessionResponse)
def create_admin_session(body: AdminSessionRequest, request: Request) -> AdminSessionResponse:
    try:
        token = service(request).create_admin_session(
            body.username, body.password, _source_ip(request)
        )
    except DeviceAuthError as error:
        _raise(error)
    return AdminSessionResponse(
        access_token=token,
        expires_in=service(request).settings.public_admin_token_ttl_seconds,
    )


@router.post("/admin/invites", response_model=InviteCreateResponse)
def create_invite(
    body: InviteCreateRequest,
    request: Request,
    admin: AdminActor,
) -> InviteCreateResponse:
    auth = service(request)
    try:
        code, expires_at = auth.issue_invite(
            admin.actor_id,
            body.user_id,
            body.display_name,
            body.replace_existing_device,
        )
    except DeviceAuthError as error:
        _raise(error)
    return InviteCreateResponse(
        invite_code=code,
        user_id=body.user_id,
        expires_at=expires_at.isoformat(),
    )


@router.get("/admin/users/{user_id}/device", response_model=DeviceStatusResponse)
def get_device_status(
    user_id: str,
    request: Request,
    _: AdminActor,
) -> DeviceStatusResponse:
    auth = service(request)
    try:
        device = auth.device_status(user_id)
    except DeviceAuthError as error:
        _raise(error)
    return DeviceStatusResponse(
        user_id=user_id,
        device_id=device.id if device else None,
        display_name=device.display_name if device else None,
        status=device.status if device else None,
        last_seen_at=device.last_seen_at.isoformat() if device and device.last_seen_at else None,
        is_administrator=device.is_administrator if device else False,
    )


@router.get("/admin/devices", response_model=AdminDeviceListResponse)
def list_devices(request: Request, _: AdminActor) -> AdminDeviceListResponse:
    return AdminDeviceListResponse(
        items=[
            AdminDeviceResponse(
                device_id=item.id,
                user_id=item.user_id,
                display_name=item.display_name,
                application_id=item.application_id,
                status=item.status,
                is_administrator=item.is_administrator,
                created_at=item.created_at.isoformat(),
                last_seen_at=item.last_seen_at.isoformat() if item.last_seen_at else None,
            )
            for item in service(request).list_devices()
        ]
    )


@router.post(
    "/admin/devices/{device_id}/administrator",
    response_model=AdministratorChangeResponse,
)
def grant_administrator(
    device_id: str, request: Request, admin: AdminActor
) -> AdministratorChangeResponse:
    try:
        enabled = service(request).set_administrator(device_id, True, admin.actor_id)
    except DeviceAuthError as error:
        _raise(error)
    return AdministratorChangeResponse(device_id=device_id, is_administrator=enabled)


@router.delete(
    "/admin/devices/{device_id}/administrator",
    response_model=AdministratorChangeResponse,
)
def revoke_administrator(
    device_id: str, request: Request, admin: AdminActor
) -> AdministratorChangeResponse:
    try:
        enabled = service(request).set_administrator(device_id, False, admin.actor_id)
    except DeviceAuthError as error:
        _raise(error)
    return AdministratorChangeResponse(device_id=device_id, is_administrator=enabled)


@router.post("/admin/devices/{device_id}/revoke", response_model=RevokeResponse)
def revoke_device(
    device_id: str,
    request: Request,
    admin: AdminActor,
) -> RevokeResponse:
    auth = service(request)
    try:
        revoked = auth.revoke(device_id, admin.actor_id)
    except DeviceAuthError as error:
        _raise(error)
    return RevokeResponse(revoked=revoked)


@router.get("/admin/chorus/moderation-settings", response_model=ChorusModerationSettingsResponse)
def get_chorus_moderation_settings(
    request: Request, _: AdminActor
) -> ChorusModerationSettingsResponse:
    return chorus_service(request).moderation_settings()


@router.patch("/admin/chorus/moderation-settings")
def patch_chorus_moderation_settings(
    body: ChorusModerationSettingsPatch,
    request: Request,
    admin: AdminActor,
):
    return model_response(
        chorus_service(request).update_moderation_settings(body.automatic_approval, admin)
    )


@router.get("/admin/chorus/tracks")
def list_chorus_tracks_for_moderation(
    request: Request,
    admin: AdminActor,
    status: Annotated[
        Literal["pending_review", "published", "rejected"], Query()
    ] = "pending_review",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    return model_response(chorus_service(request).list_tracks_for_moderation(status, admin, limit))


@router.patch("/admin/chorus/tracks/{track_id}/moderation")
def moderate_chorus_track(
    track_id: str,
    body: ChorusModerationRequest,
    request: Request,
    admin: AdminActor,
    background_tasks: BackgroundTasks,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
):
    chorus = chorus_service(request)
    item = chorus.moderate_track(track_id, body, require_if_match(if_match), admin)
    if item.status == "published":
        background_tasks.add_task(_prepare_default_mix, chorus, item.chorus_project_id)
    return model_response(item, headers={"ETag": f'"rev-{item.revision}"'})


@router.post("/device/challenge", response_model=NonceResponse)
def enrollment_challenge(body: EnrollmentChallengeRequest, request: Request) -> NonceResponse:
    try:
        nonce, expires_at = service(request).create_enrollment_challenge(body.invite_code)
    except DeviceAuthError as error:
        _raise(error)
    return NonceResponse(nonce=nonce, expires_at=expires_at.isoformat())


@router.post("/device/enroll", response_model=DeviceSessionResponse)
def enroll_device(body: DeviceEnrollRequest, request: Request) -> DeviceSessionResponse:
    auth = service(request)
    try:
        result = auth.enroll(
            body.invite_code,
            body.nonce,
            body.public_key_spki,
            body.signature,
            body.display_name,
            body.application_id,
            body.signing_certificate_sha256,
            _source_ip(request),
        )
    except DeviceAuthError as error:
        _raise(error)
    return DeviceSessionResponse(
        user_id=result.principal.user_id,
        device_id=result.principal.device_id,
        session_id=result.principal.session_id,
        access_token=result.access_token,
        access_token_expires_in=auth.settings.public_access_token_ttl_seconds,
        session_expires_at=result.session_expires_at.isoformat(),
    )


@router.post("/device/nonce", response_model=NonceResponse)
def api_nonce(
    body: DeviceNonceRequest,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> NonceResponse:
    try:
        nonce, expires_at = service(request).create_api_nonce(
            _token(authorization, "Device"), body.device_id
        )
    except DeviceAuthError as error:
        _raise(error)
    return NonceResponse(nonce=nonce, expires_at=expires_at.isoformat())


@router.post("/device/session/challenge", response_model=NonceResponse)
def refresh_challenge(body: DeviceNonceRequest, request: Request) -> NonceResponse:
    session_id = request.headers.get("X-Rhythm-Session-ID", "")
    if len(session_id) != 36:
        raise HTTPException(422, "X-Rhythm-Session-ID is required")
    try:
        nonce, expires_at = service(request).create_refresh_nonce(body.device_id, session_id)
    except DeviceAuthError as error:
        _raise(error)
    return NonceResponse(nonce=nonce, expires_at=expires_at.isoformat())


@router.post("/device/session/refresh", response_model=DeviceSessionResponse)
def refresh_session(body: DeviceRefreshRequest, request: Request) -> DeviceSessionResponse:
    auth = service(request)
    try:
        principal, token, expires_at = auth.refresh(
            body.device_id,
            body.session_id,
            body.timestamp,
            body.nonce,
            body.signature,
        )
    except DeviceAuthError as error:
        _raise(error)
    return DeviceSessionResponse(
        user_id=principal.user_id,
        device_id=principal.device_id,
        session_id=principal.session_id,
        access_token=token,
        access_token_expires_in=auth.settings.public_access_token_ttl_seconds,
        session_expires_at=expires_at.isoformat(),
    )
