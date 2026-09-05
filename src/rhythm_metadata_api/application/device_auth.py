from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.models import (
    AuthAuditEvent,
    AuthNonce,
    AuthUser,
    DeviceInvite,
    DeviceSession,
    RegisteredDevice,
    new_id,
    utc_now,
)


class DeviceAuthError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class DevicePrincipal:
    user_id: str
    device_id: str
    session_id: str
    key_thumbprint: str


@dataclass(frozen=True)
class EnrollmentResult:
    principal: DevicePrincipal
    access_token: str
    session_expires_at: datetime


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sha256(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def hash_admin_password(password: str, *, salt: bytes | None = None) -> str:
    actual_salt = salt or secrets.token_bytes(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode(), salt=actual_salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${_b64url_encode(actual_salt)}${_b64url_encode(digest)}"


def verify_admin_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=_b64url_decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_b64url_decode(expected)),
        )
        return secrets.compare_digest(actual, _b64url_decode(expected))
    except (ValueError, TypeError):
        return False


def enrollment_canonical(nonce: str, invite_code: str, key_thumbprint: str) -> bytes:
    return f"RHYTHM-ENROLL-V1\n{nonce}\n{invite_code}\n{key_thumbprint}".encode()


def refresh_canonical(
    device_id: str, session_id: str, timestamp: int, nonce: str
) -> bytes:
    return (
        f"RHYTHM-REFRESH-V1\n{device_id}\n{session_id}\n{timestamp}\n{nonce}"
    ).encode()


def request_canonical(
    method: str,
    path: str,
    query: str,
    body_sha256: str,
    device_id: str,
    timestamp: int,
    nonce: str,
) -> bytes:
    return "\n".join(
        (
            "RHYTHM-DEVICE-V1",
            method.upper(),
            path,
            query,
            body_sha256.lower(),
            device_id,
            str(timestamp),
            nonce,
        )
    ).encode()


class DeviceAuthService:
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    def _token(self, claims: dict[str, Any]) -> str:
        header = _b64url_encode(b'{"alg":"HS256","typ":"JWT"}')
        payload = _b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        signing_input = f"{header}.{payload}"
        signature = hmac.new(
            self.settings.public_token_secret.encode(), signing_input.encode(), hashlib.sha256
        ).digest()
        return f"{signing_input}.{_b64url_encode(signature)}"

    def _claims(self, token: str, expected_type: str) -> dict[str, Any]:
        try:
            header, payload, supplied = token.split(".")
            signing_input = f"{header}.{payload}"
            expected = hmac.new(
                self.settings.public_token_secret.encode(), signing_input.encode(), hashlib.sha256
            ).digest()
            if not secrets.compare_digest(expected, _b64url_decode(supplied)):
                raise ValueError
            claims = json.loads(_b64url_decode(payload))
            if claims.get("typ") != expected_type or int(claims["exp"]) <= int(utc_now().timestamp()):
                raise ValueError
            return claims
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise DeviceAuthError(401, "invalid or expired access token") from None

    def _audit(
        self,
        session: Session,
        event_type: str,
        result: str,
        *,
        actor_type: str,
        actor_id: str | None = None,
        source_ip: str | None = None,
    ) -> None:
        session.add(
            AuthAuditEvent(
                actor_type=actor_type,
                actor_id=actor_id,
                event_type=event_type,
                source_ip=source_ip,
                result=result,
            )
        )

    def create_admin_session(self, username: str, password: str, source_ip: str | None) -> str:
        now = utc_now()
        with Session(self.engine) as session:
            attempts = session.scalar(
                select(func.count(AuthAuditEvent.id)).where(
                    AuthAuditEvent.event_type == "admin_login",
                    AuthAuditEvent.result == "failure",
                    AuthAuditEvent.source_ip == source_ip,
                    AuthAuditEvent.created_at >= now - timedelta(minutes=15),
                )
            )
            if attempts is not None and attempts >= 5:
                self._audit(
                    session, "admin_login", "rate_limited", actor_type="admin", source_ip=source_ip
                )
                session.commit()
                raise DeviceAuthError(429, "too many admin login attempts")
            valid = secrets.compare_digest(username, self.settings.public_admin_username)
            valid = verify_admin_password(password, self.settings.public_admin_password_hash) and valid
            if not valid:
                self._audit(
                    session, "admin_login", "failure", actor_type="admin", source_ip=source_ip
                )
                session.commit()
                raise DeviceAuthError(401, "invalid admin credentials")
            self._audit(
                session,
                "admin_login",
                "success",
                actor_type="admin",
                actor_id=username,
                source_ip=source_ip,
            )
            session.commit()
        issued = int(now.timestamp())
        return self._token(
            {
                "typ": "admin",
                "sub": username,
                "iat": issued,
                "exp": issued + self.settings.public_admin_token_ttl_seconds,
                "jti": new_id(),
            }
        )

    def require_admin(self, token: str) -> str:
        claims = self._claims(token, "admin")
        return str(claims["sub"])

    def issue_invite(
        self,
        admin_id: str,
        user_id: str,
        display_name: str | None,
        replace_existing_device: bool,
    ) -> tuple[str, datetime]:
        code = _b64url_encode(secrets.token_bytes(24))
        expires_at = utc_now() + timedelta(seconds=self.settings.public_invite_ttl_seconds)
        with Session(self.engine) as session:
            user = session.get(AuthUser, user_id)
            if user is None:
                user = AuthUser(id=user_id, display_name=display_name)
                session.add(user)
            else:
                if user.status != "active":
                    raise DeviceAuthError(409, "user is disabled")
                if display_name is not None:
                    user.display_name = display_name
            session.add(
                DeviceInvite(
                    code_hash=_sha256(code),
                    user_id=user_id,
                    expires_at=expires_at,
                    issued_by=admin_id,
                    replace_existing_device=replace_existing_device,
                )
            )
            self._audit(
                session,
                "invite_issued",
                "success",
                actor_type="admin",
                actor_id=admin_id,
            )
            session.commit()
        return code, expires_at

    def _active_invite(self, session: Session, invite_code: str) -> DeviceInvite:
        invite = session.scalar(
            select(DeviceInvite).where(DeviceInvite.code_hash == _sha256(invite_code))
        )
        if (
            invite is None
            or invite.consumed_at is not None
            or _aware(invite.expires_at) <= utc_now()
        ):
            raise DeviceAuthError(401, "invalid, expired, or consumed invite")
        return invite

    def create_enrollment_challenge(self, invite_code: str) -> tuple[str, datetime]:
        nonce = _b64url_encode(secrets.token_bytes(24))
        expires_at = utc_now() + timedelta(seconds=self.settings.public_nonce_ttl_seconds)
        with Session(self.engine) as session:
            self._active_invite(session, invite_code)
            session.add(
                AuthNonce(
                    nonce_hash=_sha256(nonce),
                    purpose="enroll",
                    expires_at=expires_at,
                )
            )
            session.commit()
        return nonce, expires_at

    def _load_public_key(self, public_key_spki: str) -> tuple[ec.EllipticCurvePublicKey, str]:
        try:
            der = _b64url_decode(public_key_spki)
            key = serialization.load_der_public_key(der)
        except (ValueError, TypeError):
            raise DeviceAuthError(422, "public key must be base64url DER SubjectPublicKeyInfo") from None
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise DeviceAuthError(422, "only P-256 device keys are supported")
        canonical_der = key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return key, _sha256(canonical_der)

    def _verify_signature(
        self, key: ec.EllipticCurvePublicKey, signature: str, canonical: bytes
    ) -> None:
        try:
            key.verify(_b64url_decode(signature), canonical, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, ValueError):
            raise DeviceAuthError(401, "invalid device signature") from None

    def _consume_nonce(
        self, session: Session, nonce: str, purpose: str, device_id: str | None = None
    ) -> None:
        now = utc_now()
        result = session.execute(
            update(AuthNonce)
            .where(
                AuthNonce.nonce_hash == _sha256(nonce),
                AuthNonce.purpose == purpose,
                AuthNonce.device_id == device_id,
                AuthNonce.consumed_at.is_(None),
                AuthNonce.expires_at > now,
            )
            .values(consumed_at=now)
        )
        if result.rowcount != 1:
            raise DeviceAuthError(401, "invalid, expired, or consumed nonce")

    def _access_token(self, principal: DevicePrincipal) -> str:
        now = int(utc_now().timestamp())
        return self._token(
            {
                "typ": "device",
                "sub": principal.user_id,
                "device_id": principal.device_id,
                "session_id": principal.session_id,
                "scope": "catalog:read",
                "cnf": {"jkt": principal.key_thumbprint},
                "iat": now,
                "exp": now + self.settings.public_access_token_ttl_seconds,
                "jti": new_id(),
            }
        )

    def enroll(
        self,
        invite_code: str,
        nonce: str,
        public_key_spki: str,
        signature: str,
        display_name: str | None,
        source_ip: str | None,
    ) -> EnrollmentResult:
        public_key, thumbprint = self._load_public_key(public_key_spki)
        self._verify_signature(
            public_key, signature, enrollment_canonical(nonce, invite_code, thumbprint)
        )
        now = utc_now()
        session_expires = now + timedelta(days=self.settings.public_device_session_ttl_days)
        with Session(self.engine) as session:
            invite = self._active_invite(session, invite_code)
            self._consume_nonce(session, nonce, "enroll")
            active = session.scalar(
                select(RegisteredDevice).where(
                    RegisteredDevice.user_id == invite.user_id,
                    RegisteredDevice.status == "active",
                )
            )
            if active is not None:
                if not invite.replace_existing_device:
                    raise DeviceAuthError(409, "user already has an active device")
                active.status = "revoked"
                active.revoked_at = now
                session.execute(
                    update(DeviceSession)
                    .where(
                        DeviceSession.device_id == active.id,
                        DeviceSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                )
            device = RegisteredDevice(
                id=new_id(),
                user_id=invite.user_id,
                public_key_spki=public_key_spki,
                public_key_thumbprint=thumbprint,
                display_name=display_name,
            )
            session.add(device)
            session.flush()
            device_session = DeviceSession(
                id=new_id(), device_id=device.id, expires_at=session_expires
            )
            session.add(device_session)
            session.flush()
            consumed = session.execute(
                update(DeviceInvite)
                .where(DeviceInvite.id == invite.id, DeviceInvite.consumed_at.is_(None))
                .values(consumed_at=now, consumed_by_device_id=device.id)
            )
            if consumed.rowcount != 1:
                raise DeviceAuthError(401, "invite was already consumed")
            self._audit(
                session,
                "device_enrolled",
                "success",
                actor_type="device",
                actor_id=device.id,
                source_ip=source_ip,
            )
            principal = DevicePrincipal(
                invite.user_id, device.id, device_session.id, device.public_key_thumbprint
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise DeviceAuthError(409, "device or user is already registered") from None
        return EnrollmentResult(principal, self._access_token(principal), session_expires)

    def require_device_token(self, token: str, expected_device_id: str | None = None) -> DevicePrincipal:
        claims = self._claims(token, "device")
        principal = DevicePrincipal(
            user_id=str(claims["sub"]),
            device_id=str(claims["device_id"]),
            session_id=str(claims["session_id"]),
            key_thumbprint=str(claims["cnf"]["jkt"]),
        )
        if expected_device_id is not None and not secrets.compare_digest(
            principal.device_id, expected_device_id
        ):
            raise DeviceAuthError(401, "token is bound to another device")
        return principal

    def _active_device_and_session(
        self, session: Session, principal: DevicePrincipal
    ) -> tuple[RegisteredDevice, DeviceSession]:
        device = session.get(RegisteredDevice, principal.device_id)
        device_session = session.get(DeviceSession, principal.session_id)
        user = session.get(AuthUser, principal.user_id)
        if (
            device is None
            or device.user_id != principal.user_id
            or not secrets.compare_digest(
                device.public_key_thumbprint, principal.key_thumbprint
            )
            or device.status != "active"
            or user is None
            or user.status != "active"
            or device_session is None
            or device_session.device_id != device.id
            or device_session.revoked_at is not None
            or _aware(device_session.expires_at) <= utc_now()
        ):
            raise DeviceAuthError(401, "device session is inactive")
        return device, device_session

    def create_api_nonce(self, token: str, device_id: str) -> tuple[str, datetime]:
        principal = self.require_device_token(token, device_id)
        nonce = _b64url_encode(secrets.token_bytes(24))
        expires_at = utc_now() + timedelta(seconds=self.settings.public_nonce_ttl_seconds)
        with Session(self.engine) as session:
            self._active_device_and_session(session, principal)
            session.add(
                AuthNonce(
                    nonce_hash=_sha256(nonce),
                    purpose="api",
                    device_id=device_id,
                    expires_at=expires_at,
                )
            )
            session.commit()
        return nonce, expires_at

    def create_refresh_nonce(self, device_id: str, session_id: str) -> tuple[str, datetime]:
        nonce = _b64url_encode(secrets.token_bytes(24))
        expires_at = utc_now() + timedelta(seconds=self.settings.public_nonce_ttl_seconds)
        with Session(self.engine) as session:
            device = session.get(RegisteredDevice, device_id)
            if device is None:
                raise DeviceAuthError(401, "unknown device")
            principal = DevicePrincipal(
                device.user_id, device_id, session_id, device.public_key_thumbprint
            )
            self._active_device_and_session(session, principal)
            session.add(
                AuthNonce(
                    nonce_hash=_sha256(nonce),
                    purpose="refresh",
                    device_id=device_id,
                    expires_at=expires_at,
                )
            )
            session.commit()
        return nonce, expires_at

    def refresh(
        self,
        device_id: str,
        session_id: str,
        timestamp: int,
        nonce: str,
        signature: str,
    ) -> tuple[DevicePrincipal, str, datetime]:
        self._validate_timestamp(timestamp)
        with Session(self.engine) as session:
            device = session.get(RegisteredDevice, device_id)
            if device is None:
                raise DeviceAuthError(401, "unknown device")
            principal = DevicePrincipal(
                device.user_id, device_id, session_id, device.public_key_thumbprint
            )
            _, device_session = self._active_device_and_session(session, principal)
            key, _ = self._load_public_key(device.public_key_spki)
            self._verify_signature(
                key,
                signature,
                refresh_canonical(device_id, session_id, timestamp, nonce),
            )
            self._consume_nonce(session, nonce, "refresh", device_id)
            device.last_seen_at = utc_now()
            session.commit()
            expires_at = _aware(device_session.expires_at)
        return principal, self._access_token(principal), expires_at

    def _validate_timestamp(self, timestamp: int) -> None:
        if abs(int(utc_now().timestamp()) - timestamp) > self.settings.public_nonce_ttl_seconds:
            raise DeviceAuthError(401, "request timestamp is outside the allowed window")

    def authenticate_request(
        self,
        token: str,
        device_id: str,
        timestamp: int,
        nonce: str,
        body_sha256: str,
        signature: str,
        method: str,
        path: str,
        query: str,
    ) -> DevicePrincipal:
        if len(body_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in body_sha256):
            raise DeviceAuthError(401, "invalid content hash")
        self._validate_timestamp(timestamp)
        principal = self.require_device_token(token, device_id)
        with Session(self.engine) as session:
            device, _ = self._active_device_and_session(session, principal)
            key, thumbprint = self._load_public_key(device.public_key_spki)
            if not secrets.compare_digest(thumbprint, device.public_key_thumbprint):
                raise DeviceAuthError(401, "registered device key is invalid")
            self._verify_signature(
                key,
                signature,
                request_canonical(
                    method,
                    path,
                    query,
                    body_sha256,
                    device_id,
                    timestamp,
                    nonce,
                ),
            )
            self._consume_nonce(session, nonce, "api", device_id)
            device.last_seen_at = utc_now()
            session.commit()
        return principal

    def device_status(self, user_id: str) -> RegisteredDevice | None:
        with Session(self.engine) as session:
            return session.scalar(
                select(RegisteredDevice).where(
                    RegisteredDevice.user_id == user_id,
                    RegisteredDevice.status == "active",
                )
            )

    def revoke(self, device_id: str, admin_id: str) -> bool:
        now = utc_now()
        with Session(self.engine) as session:
            device = session.get(RegisteredDevice, device_id)
            if device is None or device.status != "active":
                return False
            device.status = "revoked"
            device.revoked_at = now
            session.execute(
                update(DeviceSession)
                .where(DeviceSession.device_id == device_id, DeviceSession.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            self._audit(
                session,
                "device_revoked",
                "success",
                actor_type="admin",
                actor_id=admin_id,
            )
            session.commit()
        return True
