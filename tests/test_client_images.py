from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.catalog_service import ActorContext
from rhythm_metadata_api.application.device_auth import (
    enrollment_canonical,
    hash_admin_password,
    request_canonical,
)
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.models import UploadSession
from rhythm_metadata_api.infrastructure.storage.cos_images import CosObjectMetadata
from rhythm_metadata_api.public_main import (
    _public_read_allowed,
    _public_write_allowed,
    create_public_app,
)

DEBUG_CERTIFICATE_SHA256 = "ab" * 32


class FakeImageObjectGateway:
    def __init__(self, content: bytes, thumbnail: bytes, media_type: str = "image/png") -> None:
        self.content = content
        self.thumbnail = thumbnail
        self.media_type = media_type
        self.promotions: list[tuple[str, str]] = []
        self.deletions: list[str] = []
        self.before_head: Callable[[], None] | None = None

    def head(self, key: str) -> CosObjectMetadata:
        before_head, self.before_head = self.before_head, None
        if before_head is not None:
            before_head()
        content = self.thumbnail if key.endswith("/thumbnail_512") else self.content
        return CosObjectMetadata(
            len(content),
            self.media_type,
            f'"{hashlib.md5(content, usedforsecurity=False).hexdigest()}"',
            str(int.from_bytes(hashlib.sha256(content).digest()[:8], "big")),
        )

    def promote(self, source_key: str, destination_key: str) -> None:
        self.promotions.append((source_key, destination_key))

    def delete(self, key: str) -> None:
        self.deletions.append(key)


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def image_settings(tmp_path: Path) -> Settings:
    return Settings(
        bootstrap_token="private-test-token",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
        public_token_secret="test-only-secret-that-is-longer-than-32-bytes",
        public_admin_username="owner",
        public_admin_password_hash=hash_admin_password("correct horse battery staple"),
        sonorus_debug_certificate_sha256=DEBUG_CERTIFICATE_SHA256,
        cos_secret_id="AKID-test",
        cos_secret_key="secret-test",
        client_image_cos_bucket="images-1250000000",
    )


def object_declaration(
    content: bytes,
    *,
    media_type: str = "image/png",
    width: int = 320,
    height: int = 240,
) -> dict[str, Any]:
    return {
        "media_type": media_type,
        "byte_size": len(content),
        "content_md5": base64.b64encode(
            hashlib.md5(content, usedforsecurity=False).digest()
        ).decode(),
        "client_sha256": hashlib.sha256(content).hexdigest(),
        "width": width,
        "height": height,
    }


def admin_token(client: TestClient) -> str:
    response = client.post(
        "/v2/admin/session",
        json={"username": "owner", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return response.json()["accessToken"]


def enroll(client: TestClient, admin: str) -> tuple[dict[str, Any], ec.EllipticCurvePrivateKey]:
    invite = client.post(
        "/v2/admin/invites",
        headers={"Authorization": f"Bearer {admin}"},
        json={"userId": "image-user", "displayName": "Image User"},
    )
    assert invite.status_code == 200
    invite_code = invite.json()["inviteCode"]
    challenge = client.post("/v2/device/challenge", json={"inviteCode": invite_code})
    assert challenge.status_code == 200
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
            invite_code,
            thumbprint,
            "io.github.cluno1.sonorus.debug",
            DEBUG_CERTIFICATE_SHA256,
        ),
        ec.ECDSA(hashes.SHA256()),
    )
    response = client.post(
        "/v2/device/enroll",
        json={
            "inviteCode": invite_code,
            "nonce": nonce,
            "publicKeySpki": b64url(public_der),
            "signature": b64url(signature),
            "displayName": "Pixel Test",
            "applicationId": "io.github.cluno1.sonorus.debug",
            "signingCertificateSha256": DEBUG_CERTIFICATE_SHA256,
        },
    )
    assert response.status_code == 200, response.text
    return response.json(), key


def signed_headers(
    client: TestClient,
    credentials: dict[str, Any],
    key: ec.EllipticCurvePrivateKey,
    path: str,
    *,
    method: str = "GET",
    query: str = "",
    body: bytes = b"",
) -> dict[str, str]:
    nonce_response = client.post(
        "/v2/device/nonce",
        headers={"Authorization": f"Device {credentials['accessToken']}"},
        json={"deviceId": credentials["deviceId"]},
    )
    assert nonce_response.status_code == 200
    nonce = nonce_response.json()["nonce"]
    timestamp = int(time.time())
    digest = hashlib.sha256(body).hexdigest()
    signature = key.sign(
        request_canonical(
            method,
            path,
            query,
            digest,
            credentials["deviceId"],
            timestamp,
            nonce,
        ),
        ec.ECDSA(hashes.SHA256()),
    )
    return {
        "Authorization": f"Device {credentials['accessToken']}",
        "X-Rhythm-Device-ID": credentials["deviceId"],
        "X-Rhythm-Timestamp": str(timestamp),
        "X-Rhythm-Nonce": nonce,
        "X-Rhythm-Content-SHA256": digest,
        "X-Rhythm-Signature": b64url(signature),
    }


def json_request(
    client: TestClient,
    credentials: dict[str, Any],
    key: ec.EllipticCurvePrivateKey,
    method: str,
    path: str,
    payload: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
):
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = signed_headers(
        client, credentials, key, path, method=method, body=body
    ) | {"Content-Type": "application/json"}
    headers.update(extra_headers or {})
    return client.request(method, path, content=body, headers=headers)


def test_public_image_routes_are_narrowly_allowlisted() -> None:
    assert _public_read_allowed("GET", "/v2/labs/images/capabilities")
    assert _public_read_allowed("GET", "/v2/labs/images/an-image/delivery")
    assert _public_write_allowed("POST", "/v2/labs/image-uploads")
    assert _public_write_allowed("POST", "/v2/labs/images/thumbnail-deliveries")
    assert _public_write_allowed("DELETE", "/v2/labs/images/an-image")
    assert not _public_write_allowed("PUT", "/v2/labs/images/an-image/content")


def test_direct_upload_gallery_visibility_delivery_and_delete(tmp_path: Path) -> None:
    content = b"sanitized-png-image"
    thumbnail = b"thumbnail-png-image"
    gateway = FakeImageObjectGateway(content, thumbnail)
    app = create_public_app(image_settings(tmp_path))
    with TestClient(app) as client:
        client.app.state.v2_container.client_images.object_gateway = gateway
        admin = admin_token(client)
        credentials, key = enroll(client, admin)

        capability_path = "/v2/labs/images/capabilities"
        capabilities = client.get(
            capability_path,
            headers=signed_headers(client, credentials, key, capability_path),
        )
        assert capabilities.status_code == 200
        assert capabilities.json()["enabled"] is True
        assert capabilities.json()["max_batch_items"] >= 200

        batch_path = "/v2/labs/image-upload-batches"
        batch = json_request(
            client,
            credentials,
            key,
            "POST",
            batch_path,
            {"client_batch_id": "batch-1", "total_count": 1, "total_bytes": len(content)},
            extra_headers={"Idempotency-Key": "batch-1"},
        )
        assert batch.status_code == 201, batch.text
        batch_id = batch.json()["id"]

        upload_path = "/v2/labs/image-uploads"
        upload_payload = {
            "batch_id": batch_id,
            "client_item_id": "item-1",
            "display_name": "透明图片.png",
            **object_declaration(content, width=640, height=480),
            "metadata_sanitized": True,
            "thumbnail_512": object_declaration(thumbnail),
        }
        upload = json_request(
            client,
            credentials,
            key,
            "POST",
            upload_path,
            upload_payload,
            extra_headers={"Idempotency-Key": "upload-1"},
        )
        assert upload.status_code == 201, upload.text
        upload_body = upload.json()
        upload_id = upload_body["upload_id"]
        image_id = upload_body["image_id"]
        assert upload_body["upload"]["required_headers"] == {
            "Content-Type": "image/png",
            "Content-MD5": upload_payload["content_md5"],
        }
        assert "q-header-list=content-md5;content-type;host" in upload_body["upload"]["url"]
        assert upload_body["thumbnail_upload"]["required_headers"]["Content-MD5"] == (
            upload_payload["thumbnail_512"]["content_md5"]
        )
        assert "secret-test" not in upload_body["upload"]["url"]

        replay = json_request(
            client,
            credentials,
            key,
            "POST",
            upload_path,
            upload_payload,
            extra_headers={"Idempotency-Key": "upload-replay"},
        )
        assert replay.json()["upload_id"] == upload_id

        with Session(client.app.state.v2_container.engine) as session, session.begin():
            stored_upload = session.get(UploadSession, upload_id)
            assert stored_upload is not None
            stored_upload.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        refresh_path = f"/v2/labs/image-uploads/{upload_id}/refresh"
        refreshed = client.post(
            refresh_path,
            headers=signed_headers(
                client, credentials, key, refresh_path, method="POST"
            )
            | {"Idempotency-Key": "refresh-expired"},
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["upload_id"] == upload_id
        assert refreshed.json()["state"] == "upload_required"

        complete_path = f"/v2/labs/image-uploads/{upload_id}/complete"
        complete_headers = signed_headers(
            client, credentials, key, complete_path, method="POST"
        ) | {"Idempotency-Key": "complete-1"}
        completed = client.post(complete_path, headers=complete_headers)
        assert completed.status_code == 200, completed.text
        assert completed.json()["id"] == image_id
        asset_id = completed.json()["asset_id"]
        assert gateway.promotions[0][1].endswith(hashlib.sha256(content).hexdigest())
        assert gateway.deletions

        list_path = "/v2/labs/images?limit=30"
        own_list = client.get(
            list_path,
            headers=signed_headers(
                client, credentials, key, "/v2/labs/images", query="limit=30"
            ),
        )
        assert [item["asset_id"] for item in own_list.json()["items"]] == [asset_id]
        assert "signed_url" not in own_list.text

        thumbnail_path = "/v2/labs/images/thumbnail-deliveries"
        thumbnails = json_request(
            client,
            credentials,
            key,
            "POST",
            thumbnail_path,
            {"image_ids": [image_id], "variant": "thumbnail_512"},
        )
        delivery = thumbnails.json()["items"][0]["delivery"]
        assert delivery["signed_url"].startswith(
            "https://images-1250000000.cos.ap-guangzhou.myqcloud.com/"
        )
        assert "imageMogr2" not in delivery["signed_url"]
        assert "q-sign" not in delivery["stable_cache_key"]

        shared_before = client.get(
            "/v2/admin/shared-images",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert shared_before.json()["items"] == []
        hidden_cursor = client.get(
            "/v2/admin/shared-images",
            headers={"Authorization": f"Bearer {admin}"},
            params={"cursor": image_id},
        )
        assert hidden_cursor.status_code == 422

        visibility_path = "/v2/labs/images/admin-visibility"
        enabled = json_request(
            client,
            credentials,
            key,
            "PATCH",
            visibility_path,
            {"enabled": True},
            extra_headers={"If-Match": '"rev-1"', "Idempotency-Key": "visibility-on"},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["enabled"] is True

        shared = client.get(
            "/v2/admin/shared-images",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert [item["id"] for item in shared.json()["items"]] == [image_id]
        assert client.get(
            "/v2/admin/shared-images",
            headers={"Authorization": f"Bearer {admin}"},
            params={"created_from": "2999-01-01T00:00:00Z"},
        ).json()["items"] == []
        assert client.get(
            "/v2/admin/shared-images",
            headers={"Authorization": f"Bearer {admin}"},
            params={
                "created_from": "2026-01-02T00:00:00Z",
                "created_to": "2026-01-01T00:00:00Z",
            },
        ).status_code == 422
        shared_delivery = client.get(
            f"/v2/admin/shared-images/{image_id}/delivery?variant=preview_2048",
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert shared_delivery.status_code == 200
        assert shared_delivery.json()["variant"] == "preview_2048"
        assert "/thumbnail_512?" in shared_delivery.json()["signed_url"]

        disabled = json_request(
            client,
            credentials,
            key,
            "PATCH",
            visibility_path,
            {"enabled": False},
            extra_headers={"If-Match": '"rev-2"', "Idempotency-Key": "visibility-off"},
        )
        assert disabled.status_code == 200
        assert (
            client.get(
                f"/v2/admin/shared-images/{image_id}/delivery",
                headers={"Authorization": f"Bearer {admin}"},
            ).status_code
            == 404
        )

        delete_path = f"/v2/labs/images/{image_id}"
        deleted = client.delete(
            delete_path,
            headers=signed_headers(
                client, credentials, key, delete_path, method="DELETE"
            )
            | {"Idempotency-Key": "delete-1"},
        )
        assert deleted.json()["result"] == "deleted"


def test_create_rejects_thumbnail_larger_than_512(tmp_path: Path) -> None:
    content = b"sanitized-webp"
    thumbnail = b"oversized-thumbnail"
    gateway = FakeImageObjectGateway(content, thumbnail, "image/webp")
    app = create_public_app(image_settings(tmp_path))
    with TestClient(app) as client:
        client.app.state.v2_container.client_images.object_gateway = gateway
        admin = admin_token(client)
        credentials, key = enroll(client, admin)
        batch = json_request(
            client,
            credentials,
            key,
            "POST",
            "/v2/labs/image-upload-batches",
            {"client_batch_id": "bad-thumb", "total_count": 1, "total_bytes": len(content)},
            extra_headers={"Idempotency-Key": "bad-thumb-batch"},
        )
        upload = json_request(
            client,
            credentials,
            key,
            "POST",
            "/v2/labs/image-uploads",
            {
                "batch_id": batch.json()["id"],
                "client_item_id": "bad-thumb-1",
                "display_name": "static.webp",
                **object_declaration(content, media_type="image/webp"),
                "metadata_sanitized": True,
                "thumbnail_512": object_declaration(
                    thumbnail,
                    media_type="image/webp",
                    width=513,
                    height=100,
                ),
            },
            extra_headers={"Idempotency-Key": "bad-thumb-upload"},
        )
        assert upload.status_code == 422
        assert "thumbnail_512 dimensions" in upload.json()["detail"]
        assert gateway.promotions == []


def test_delete_during_cos_inspection_cannot_resurrect_image(tmp_path: Path) -> None:
    content = b"cancel-race-png"
    thumbnail = b"cancel-race-thumbnail"
    gateway = FakeImageObjectGateway(content, thumbnail)
    app = create_public_app(image_settings(tmp_path))
    with TestClient(app) as client:
        service = client.app.state.v2_container.client_images
        service.object_gateway = gateway
        admin = admin_token(client)
        credentials, key = enroll(client, admin)
        batch = json_request(
            client,
            credentials,
            key,
            "POST",
            "/v2/labs/image-upload-batches",
            {"client_batch_id": "delete-race", "total_count": 1, "total_bytes": len(content)},
            extra_headers={"Idempotency-Key": "delete-race-batch"},
        )
        upload = json_request(
            client,
            credentials,
            key,
            "POST",
            "/v2/labs/image-uploads",
            {
                "batch_id": batch.json()["id"],
                "client_item_id": "delete-race-1",
                "display_name": "delete-race.png",
                **object_declaration(content),
                "metadata_sanitized": True,
                "thumbnail_512": object_declaration(thumbnail),
            },
            extra_headers={"Idempotency-Key": "delete-race-upload"},
        )
        upload_id = upload.json()["upload_id"]
        image_id = upload.json()["image_id"]
        gateway.before_head = lambda: service.delete_own(
            image_id,
            ActorContext(
                actor_id="image-user",
                device_id=credentials["deviceId"],
            ),
        )

        complete_path = f"/v2/labs/image-uploads/{upload_id}/complete"
        response = client.post(
            complete_path,
            headers=signed_headers(
                client,
                credentials,
                key,
                complete_path,
                method="POST",
            )
            | {"Idempotency-Key": "delete-race-complete"},
        )

        assert response.status_code == 409
        assert "cannot complete while deleted" in response.json()["detail"]
        assert gateway.promotions == []
        list_path = "/v2/labs/images"
        own_list = client.get(
            list_path,
            headers=signed_headers(client, credentials, key, list_path),
        )
        assert own_list.status_code == 200
        assert own_list.json()["items"] == []
