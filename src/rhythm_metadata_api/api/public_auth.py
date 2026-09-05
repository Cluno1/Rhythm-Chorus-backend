from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request

from rhythm_metadata_api.application.device_auth import DeviceAuthError, DeviceAuthService
from rhythm_metadata_api.domain.auth_schemas import (
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
    authorization: Annotated[str | None, Header()] = None,
) -> InviteCreateResponse:
    auth = service(request)
    try:
        admin_id = auth.require_admin(_token(authorization, "Bearer"))
        code, expires_at = auth.issue_invite(
            admin_id,
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
    authorization: Annotated[str | None, Header()] = None,
) -> DeviceStatusResponse:
    auth = service(request)
    try:
        auth.require_admin(_token(authorization, "Bearer"))
        device = auth.device_status(user_id)
    except DeviceAuthError as error:
        _raise(error)
    return DeviceStatusResponse(
        user_id=user_id,
        device_id=device.id if device else None,
        display_name=device.display_name if device else None,
        status=device.status if device else None,
        last_seen_at=device.last_seen_at.isoformat() if device and device.last_seen_at else None,
    )


@router.post("/admin/devices/{device_id}/revoke", response_model=RevokeResponse)
def revoke_device(
    device_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> RevokeResponse:
    auth = service(request)
    try:
        admin_id = auth.require_admin(_token(authorization, "Bearer"))
        revoked = auth.revoke(device_id, admin_id)
    except DeviceAuthError as error:
        _raise(error)
    return RevokeResponse(revoked=revoked)


@router.post("/device/challenge", response_model=NonceResponse)
def enrollment_challenge(
    body: EnrollmentChallengeRequest, request: Request
) -> NonceResponse:
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
