from __future__ import annotations

import base64
import hashlib
import json
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
from rhythm_metadata_api.public_main import _public_read_allowed, create_public_app

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
DEBUG_CERTIFICATE_SHA256 = "ab" * 32
STABLE_CERTIFICATE_SHA256 = "cd" * 32


def test_effective_lyric_sources_is_public_read_only() -> None:
    path = "/v2/renditions/33333333-3333-4333-8333-333333333333/effective-lyric-sources"
    assert _public_read_allowed("GET", path)
    assert not _public_read_allowed("POST", path)


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
        sonorus_updates_root=str(tmp_path / "updates"),
        sonorus_debug_certificate_sha256=DEBUG_CERTIFICATE_SHA256,
        sonorus_stable_certificate_sha256=STABLE_CERTIFICATE_SHA256,
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
    application_id: str = "io.github.cluno1.sonorus.debug",
    certificate_sha256: str = DEBUG_CERTIFICATE_SHA256,
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
        enrollment_canonical(
            nonce, invite, thumbprint, application_id, certificate_sha256
        ),
        ec.ECDSA(hashes.SHA256()),
    )
    response = client.post(
        "/v2/device/enroll",
        json={
            "inviteCode": invite,
            "nonce": nonce,
            "publicKeySpki": b64url(public_der),
            "signature": b64url(signature),
            "displayName": "Pixel Test",
            "applicationId": application_id,
            "signingCertificateSha256": certificate_sha256,
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
    method: str = "GET",
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
        method, path, query, EMPTY_SHA256, credentials["deviceId"], timestamp, nonce
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


def update_headers(
    client: TestClient,
    credentials: dict[str, Any],
    key: ec.EllipticCurvePrivateKey,
    path: str,
    *,
    method: str = "GET",
) -> dict[str, str]:
    return signed_headers(client, credentials, key, path, method=method) | {
        "X-Sonorus-Application-ID": "io.github.cluno1.sonorus.debug",
        "X-Sonorus-Update-Channel": "debug",
        "X-Sonorus-Version-Code": "1000000",
        "X-Sonorus-Signing-Certificate-SHA256": DEBUG_CERTIFICATE_SHA256,
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
            enrollment_canonical(
                nonce,
                second_invite,
                hashlib.sha256(public_der).hexdigest(),
                "io.github.cluno1.sonorus.debug",
                DEBUG_CERTIFICATE_SHA256,
            ),
            ec.ECDSA(hashes.SHA256()),
        )
        rejected = client.post(
            "/v2/device/enroll",
            json={
                "inviteCode": second_invite,
                "nonce": nonce,
                "publicKeySpki": b64url(public_der),
                "signature": b64url(signature),
                "applicationId": "io.github.cluno1.sonorus.debug",
                "signingCertificateSha256": DEBUG_CERTIFICATE_SHA256,
            },
        )
        assert rejected.status_code == 409


def test_same_user_can_register_debug_and_release_separately(tmp_path: Path) -> None:
    app = create_public_app(settings(tmp_path))
    with TestClient(app) as client:
        admin = admin_token(client)
        debug = enroll(
            client,
            create_invite(client, admin, "dual-app-user"),
            ec.generate_private_key(ec.SECP256R1()),
        )
        stable = enroll(
            client,
            create_invite(client, admin, "dual-app-user"),
            ec.generate_private_key(ec.SECP256R1()),
            application_id="io.github.cluno1.sonorus",
            certificate_sha256=STABLE_CERTIFICATE_SHA256,
        )
        assert debug["deviceId"] != stable["deviceId"]


def test_enrollment_signature_binds_application_identity(tmp_path: Path) -> None:
    app = create_public_app(settings(tmp_path))
    with TestClient(app) as client:
        admin = admin_token(client)
        invite = create_invite(client, admin, "identity-bound-user")
        challenge = client.post("/v2/device/challenge", json={"inviteCode": invite})
        nonce = challenge.json()["nonce"]
        key = ec.generate_private_key(ec.SECP256R1())
        public_der = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        thumbprint = hashlib.sha256(public_der).hexdigest()
        signature = key.sign(
            enrollment_canonical(
                nonce,
                invite,
                thumbprint,
                "io.github.cluno1.sonorus.debug",
                DEBUG_CERTIFICATE_SHA256,
            ),
            ec.ECDSA(hashes.SHA256()),
        )
        tampered = client.post(
            "/v2/device/enroll",
            json={
                "inviteCode": invite,
                "nonce": nonce,
                "publicKeySpki": b64url(public_der),
                "signature": b64url(signature),
                "applicationId": "io.github.cluno1.sonorus",
                "signingCertificateSha256": STABLE_CERTIFICATE_SHA256,
            },
        )
        assert tampered.status_code == 401


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


def test_authenticated_debug_update_manifest_range_and_track_isolation(tmp_path: Path) -> None:
    update_root = tmp_path / "updates"
    release_dir = update_root / "debug" / "releases" / "2001001"
    release_dir.mkdir(parents=True)
    apk = b"signed-sonorus-debug-apk-bytes"
    file_name = "Sonorus-1.1.0-debug.2001001-arm64-v8a.apk"
    (release_dir / file_name).write_bytes(apk)
    manifest = {
        "schemaVersion": 1,
        "channel": "debug",
        "applicationId": "io.github.cluno1.sonorus.debug",
        "signingCertificateSha256": DEBUG_CERTIFICATE_SHA256,
        "versionCode": 2001001,
        "versionName": "1.1.0-debug.2001001",
        "publishedAt": "2026-09-06T12:00:00Z",
        "minimumAndroidSdk": 26,
        "mandatory": False,
        "releaseNotes": ["test"],
        "assets": [
            {
                "abi": "arm64-v8a",
                "fileName": file_name,
                "url": f"/v2/app-updates/files/2001001/{file_name}",
                "sizeBytes": len(apk),
                "sha256": hashlib.sha256(apk).hexdigest(),
            }
        ],
        "signatureAlgorithm": "Ed25519",
        "manifestSignature": base64.b64encode(bytes(64)).decode(),
    }
    encoded = json.dumps(manifest, sort_keys=True).encode()
    (release_dir / "manifest.json").write_bytes(encoded)
    (update_root / "debug" / "latest.json").write_bytes(encoded)

    app = create_public_app(settings(tmp_path))
    with TestClient(app) as client:
        admin = admin_token(client)
        key = ec.generate_private_key(ec.SECP256R1())
        credentials = enroll(client, create_invite(client, admin), key)

        latest_path = "/v2/app-updates/latest"
        response = client.get(latest_path, headers=update_headers(client, credentials, key, latest_path))
        assert response.status_code == 200
        assert response.json()["versionCode"] == 2001001
        etag = response.headers["etag"]

        conditional = update_headers(client, credentials, key, latest_path) | {"If-None-Match": etag}
        assert client.get(latest_path, headers=conditional).status_code == 304

        file_path = f"/v2/app-updates/files/2001001/{file_name}"
        ranged = update_headers(client, credentials, key, file_path) | {"Range": "bytes=7-13"}
        partial = client.get(file_path, headers=ranged)
        assert partial.status_code == 206
        assert partial.content == apk[7:14]
        assert partial.headers["etag"] == f'"{hashlib.sha256(apk).hexdigest()}"'

        head_headers = update_headers(client, credentials, key, file_path, method="HEAD")
        head = client.head(file_path, headers=head_headers)
        assert head.status_code == 200
        assert int(head.headers["content-length"]) == len(apk)

        complete = client.get(
            file_path, headers=update_headers(client, credentials, key, file_path)
        )
        assert complete.status_code == 200
        assert hashlib.sha256(complete.content).hexdigest() == manifest["assets"][0]["sha256"]

        stale = update_headers(client, credentials, key, file_path) | {
            "If-Match": '"not-the-current-apk"'
        }
        assert client.get(file_path, headers=stale).status_code == 412

        assert client.post("/v2/app-updates/latest", json={}).status_code == 404
        assert client.put(file_path, content=b"replacement").status_code == 404
        assert client.delete(file_path).status_code == 404

        wrong_track = update_headers(client, credentials, key, latest_path) | {
            "X-Sonorus-Application-ID": "io.github.cluno1.sonorus",
            "X-Sonorus-Update-Channel": "stable",
            "X-Sonorus-Signing-Certificate-SHA256": STABLE_CERTIFICATE_SHA256,
        }
        assert client.get(latest_path, headers=wrong_track).status_code == 403
