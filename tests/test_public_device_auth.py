from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from rhythm_metadata_api.application.device_auth import (
    enrollment_canonical,
    hash_admin_password,
    refresh_canonical,
    request_canonical,
)
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.public_main import create_public_app

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def settings(tmp_path: Path) -> Settings:
    return Settings(
        bootstrap_token="private-test-token",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
        public_token_secret="test-only-secret-that-is-longer-than-32-bytes",
        public_admin_username="owner",
        public_admin_password_hash=hash_admin_password("correct horse battery staple"),
    )


def admin_token(client: TestClient) -> str:
    response = client.post(
        "/v2/admin/session",
        json={"username": "owner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return response.json()["accessToken"]


def create_invite(client: TestClient, token: str, user_id: str = "user-1") -> str:
    response = client.post(
        "/v2/admin/invites",
        headers={"Authorization": f"Bearer {token}"},
        json={"userId": user_id, "displayName": "Test User"},
    )
    assert response.status_code == 200
    return response.json()["inviteCode"]


def enroll(
    client: TestClient,
    invite: str,
    key: ec.EllipticCurvePrivateKey,
) -> dict[str, Any]:
    challenge = client.post("/v2/device/challenge", json={"inviteCode": invite})
    assert challenge.status_code == 200
    nonce = challenge.json()["nonce"]
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    thumbprint = hashlib.sha256(public_der).hexdigest()
    signature = key.sign(
        enrollment_canonical(nonce, invite, thumbprint), ec.ECDSA(hashes.SHA256())
    )
    response = client.post(
        "/v2/device/enroll",
        json={
            "inviteCode": invite,
            "nonce": nonce,
            "publicKeySpki": b64url(public_der),
            "signature": b64url(signature),
            "displayName": "Pixel Test",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def signed_headers(
    client: TestClient,
    credentials: dict[str, Any],
    key: ec.EllipticCurvePrivateKey,
    path: str,
    query: str = "",
) -> dict[str, str]:
    nonce_response = client.post(
        "/v2/device/nonce",
        headers={"Authorization": f"Device {credentials['accessToken']}"},
        json={"deviceId": credentials["deviceId"]},
    )
    assert nonce_response.status_code == 200, nonce_response.text
    nonce = nonce_response.json()["nonce"]
    timestamp = int(time.time())
    canonical = request_canonical(
        "GET", path, query, EMPTY_SHA256, credentials["deviceId"], timestamp, nonce
    )
    signature = key.sign(canonical, ec.ECDSA(hashes.SHA256()))
    return {
        "Authorization": f"Device {credentials['accessToken']}",
        "X-Rhythm-Device-ID": credentials["deviceId"],
        "X-Rhythm-Timestamp": str(timestamp),
        "X-Rhythm-Nonce": nonce,
        "X-Rhythm-Content-SHA256": EMPTY_SHA256,
        "X-Rhythm-Signature": b64url(signature),
    }


def test_enroll_signed_read_replay_refresh_and_revoke(tmp_path: Path) -> None:
    app = create_public_app(settings(tmp_path))
    with TestClient(app) as client:
        admin = admin_token(client)
        key = ec.generate_private_key(ec.SECP256R1())
        credentials = enroll(client, create_invite(client, admin), key)

        token_only = client.get(
            "/v2/works", headers={"Authorization": f"Device {credentials['accessToken']}"}
        )
        assert token_only.status_code == 401

        headers = signed_headers(client, credentials, key, "/v2/works", "limit=1")
        response = client.get("/v2/works?limit=1", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None}
        assert client.get("/v2/works?limit=1", headers=headers).status_code == 401

        # The public process mounts private handlers, but the middleware hides every write route.
        assert client.post("/v2/works", json={}).status_code == 404

        challenge = client.post(
            "/v2/device/session/challenge",
            headers={"X-Rhythm-Session-ID": credentials["sessionId"]},
            json={"deviceId": credentials["deviceId"]},
        )
        assert challenge.status_code == 200
        nonce = challenge.json()["nonce"]
        timestamp = int(time.time())
        signature = key.sign(
            refresh_canonical(
                credentials["deviceId"], credentials["sessionId"], timestamp, nonce
            ),
            ec.ECDSA(hashes.SHA256()),
        )
        refreshed = client.post(
            "/v2/device/session/refresh",
            json={
                "deviceId": credentials["deviceId"],
                "sessionId": credentials["sessionId"],
                "timestamp": timestamp,
                "nonce": nonce,
                "signature": b64url(signature),
            },
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["accessToken"] != credentials["accessToken"]

        revoked = client.post(
            f"/v2/admin/devices/{credentials['deviceId']}/revoke",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert revoked.json() == {"revoked": True}
        nonce_after_revoke = client.post(
            "/v2/device/nonce",
            headers={"Authorization": f"Device {refreshed.json()['accessToken']}"},
            json={"deviceId": credentials["deviceId"]},
        )
        assert nonce_after_revoke.status_code == 401


def test_invite_is_single_use_and_one_active_device_per_user(tmp_path: Path) -> None:
    app = create_public_app(settings(tmp_path))
    with TestClient(app) as client:
        admin = admin_token(client)
        first_invite = create_invite(client, admin, "same-user")
        enroll(client, first_invite, ec.generate_private_key(ec.SECP256R1()))
        assert (
            client.post("/v2/device/challenge", json={"inviteCode": first_invite}).status_code
            == 401
        )

        second_invite = create_invite(client, admin, "same-user")
        challenge = client.post("/v2/device/challenge", json={"inviteCode": second_invite})
        nonce = challenge.json()["nonce"]
        key = ec.generate_private_key(ec.SECP256R1())
        public_der = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        signature = key.sign(
            enrollment_canonical(nonce, second_invite, hashlib.sha256(public_der).hexdigest()),
            ec.ECDSA(hashes.SHA256()),
        )
        rejected = client.post(
            "/v2/device/enroll",
            json={
                "inviteCode": second_invite,
                "nonce": nonce,
                "publicKeySpki": b64url(public_der),
                "signature": b64url(signature),
            },
        )
        assert rejected.status_code == 409


def test_public_app_rejects_missing_secrets(tmp_path: Path) -> None:
    invalid = Settings(
        bootstrap_token="private-test-token",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
    )
    try:
        create_public_app(invalid)
    except ValueError as error:
        assert "PUBLIC_TOKEN_SECRET" in str(error)
    else:
        raise AssertionError("public app must fail closed without public secrets")
