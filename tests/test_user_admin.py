"""issue 183: administrators can disable, restore and permanently delete client users."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_client_images import FakeImageObjectGateway
from test_public_device_auth import admin_token, settings
from test_rooms import Device, add_works, invite

from rhythm_metadata_api.infrastructure.db.models import (
    Asset,
    AssetLocation,
    AuthUser,
    ClientImage,
    ClientImageAuditEvent,
    ClientImageBatch,
    DeviceInvite,
    DeviceSession,
    RegisteredDevice,
    Room,
    RoomItem,
    RoomSection,
    RoomTemplate,
    RoomTemplateSection,
    ScoreRevision,
    UploadSession,
)
from rhythm_metadata_api.infrastructure.storage.cos_images import CosImageGatewayError
from rhythm_metadata_api.public_main import (
    _public_admin_device_write_allowed,
    create_public_app,
)

BUCKET = "images-1250000000"
FUTURE = datetime.now(UTC) + timedelta(days=3)


@pytest.fixture
def public(tmp_path: Path):
    configured = settings(tmp_path, max_active_devices=4).model_copy(
        update={
            "cos_secret_id": "AKID-test",
            "cos_secret_key": "secret-test",
            "client_image_cos_bucket": BUCKET,
        }
    )
    with TestClient(create_public_app(configured)) as client:
        client.app.state.v2_container.client_images.object_gateway = FakeImageObjectGateway(b"a", b"b")
        yield client


def bearer(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token(client)}"}


def engine(client: TestClient) -> Any:
    return client.app.state.v2_container.engine


def gateway(client: TestClient) -> FakeImageObjectGateway:
    return client.app.state.v2_container.client_images.object_gateway


def seed_image(client: TestClient, owner: str, sha: str, *, name: str = "a") -> str:
    """Insert one ready image; assets are shared by sha256 exactly as production stores them."""
    with Session(engine(client)) as session:
        batch = ClientImageBatch(
            owner_user_id=owner, client_batch_id=f"{owner}-{name}", total_count=1, total_bytes=1
        )
        upload = UploadSession(
            expected_sha256=sha, expected_size=1, media_type="image/png", state="completed",
            expires_at=FUTURE,
        )
        session.add_all([batch, upload])
        asset = session.scalar(select(Asset).where(Asset.sha256 == sha))
        if asset is None:
            asset = Asset(sha256=sha, byte_size=1, detected_media_type="image/png")
            session.add(asset)
            session.flush()
            session.add(
                AssetLocation(
                    asset_id=asset.id, provider="cos",
                    storage_key=f"{BUCKET}/labs/images/assets/{sha[:2]}/{sha}",
                )
            )
        session.flush()
        image = ClientImage(
            owner_user_id=owner, batch_id=batch.id, client_item_id=name,
            upload_session_id=upload.id, asset_id=asset.id, display_name="x.png",
            media_type="image/png", width=1, height=1, byte_size=1, content_md5="m",
            client_sha256=sha, thumbnail_media_type="image/webp", thumbnail_width=1,
            thumbnail_height=1, thumbnail_byte_size=1, thumbnail_content_md5="t",
            thumbnail_client_sha256="t", metadata_sanitized=True, state="ready",
            thumbnail_storage_key=f"{BUCKET}/labs/images/thumbnails/{owner}-{name}/thumbnail_512",
        )
        session.add(image)
        session.commit()
        return image.id


def seed_room(
    client: TestClient, creator: str, work_id: str, *, assignee: str | None, added_by: str
) -> str:
    with Session(engine(client)) as session:
        room = Room(
            name="本周", service_date=date(2030, 1, 1), expires_at=FUTURE, created_by_user_id=creator
        )
        session.add(room)
        session.flush()
        section = RoomSection(room_id=room.id, position=1, assignee_user_id=assignee)
        session.add(section)
        session.flush()
        session.add(
            RoomItem(
                room_id=room.id, section_id=section.id, work_id=work_id, position=1,
                added_by_user_id=added_by,
            )
        )
        session.commit()
        return room.id


def seed_template(client: TestClient, assignee: str) -> str:
    with Session(engine(client)) as session:
        template = RoomTemplate(name="主日")
        session.add(template)
        session.flush()
        session.add(
            RoomTemplateSection(
                template_id=template.id, name="开场", position=1, assignee_user_id=assignee
            )
        )
        session.commit()
        return template.id


def session_rejected(client: TestClient, device: Device) -> bool:
    """The first step of every signed request; a dead account fails here with 401."""
    credentials = device.credentials
    response = client.post(
        "/v2/device/nonce",
        headers={"Authorization": f"Device {credentials['accessToken']}"},
        json={"deviceId": credentials["deviceId"]},
    )
    return response.status_code == 401


def count(client: TestClient, model: Any, *conditions: Any) -> int:
    with Session(engine(client)) as session:
        return session.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def test_预览统计设备房间和有效图片_软删除的图片不算(public: TestClient) -> None:
    auth = bearer(public)
    Device(public, auth["Authorization"].split()[1], "alice", "小明")
    work = add_works(public, 1)[0]
    seed_room(public, "alice", work, assignee=None, added_by="alice")
    seed_image(public, "alice", "a" * 64, name="kept")
    gone = seed_image(public, "alice", "b" * 64, name="gone")
    with Session(engine(public)) as session:
        image = session.get(ClientImage, gone)
        image.state = "deleted"
        image.deleted_at = FUTURE
        session.commit()

    response = public.get("/v2/admin/users/alice/deletion-preview", headers=auth)

    assert response.status_code == 200, response.text
    assert response.json() == {"userId": "alice", "deviceCount": 1, "roomCount": 1, "imageCount": 1}


def test_没有图片的用户无需手输确认_设备会话邀请码和自建房间一起删除(public: TestClient) -> None:
    auth = bearer(public)
    token = auth["Authorization"].split()[1]
    alice = Device(public, token, "alice", "小明")
    invite(public, token, "alice", "小明")
    work = add_works(public, 1)[0]
    room = seed_room(public, "alice", work, assignee="alice", added_by="alice")

    response = public.post("/v2/admin/users/alice/delete", headers=auth, json={})

    assert response.status_code == 200, response.text
    assert response.json() == {"userId": "alice", "deviceCount": 1, "roomCount": 1, "imageCount": 0}
    assert count(public, AuthUser, AuthUser.id == "alice") == 0
    assert count(public, RegisteredDevice, RegisteredDevice.user_id == "alice") == 0
    assert count(public, DeviceInvite, DeviceInvite.user_id == "alice") == 0
    assert count(public, DeviceSession) == 0
    assert count(public, Room, Room.id == room) == 0
    assert count(public, RoomSection, RoomSection.room_id == room) == 0
    assert count(public, RoomItem, RoomItem.room_id == room) == 0
    assert session_rejected(public, alice)


def test_别人的房间和模板只清负责人_该用户加的歌保留(public: TestClient) -> None:
    auth = bearer(public)
    token = auth["Authorization"].split()[1]
    Device(public, token, "alice", "小明")
    Device(public, token, "bob", "小红")
    work = add_works(public, 1)[0]
    room = seed_room(public, "bob", work, assignee="alice", added_by="alice")
    template = seed_template(public, "alice")

    assert public.post("/v2/admin/users/alice/delete", headers=auth, json={}).status_code == 200

    with Session(engine(public)) as session:
        kept = session.get(Room, room)
        assert kept is not None and kept.revision == 2
        assert session.scalar(select(RoomSection.assignee_user_id).where(RoomSection.room_id == room)) is None
        assert session.scalar(select(RoomItem.added_by_user_id).where(RoomItem.room_id == room)) == "alice"
        assert session.scalar(
            select(RoomTemplateSection.assignee_user_id).where(RoomTemplateSection.template_id == template)
        ) is None
        assert session.get(RoomTemplate, template).revision == 2


@pytest.mark.parametrize("confirmation", [{}, {"confirmUserId": None}, {"confirmUserId": "ALICE"}])
def test_有图片时缺少或输错用户ID_拒绝且不删除任何东西(public: TestClient, confirmation: dict) -> None:
    auth = bearer(public)
    Device(public, auth["Authorization"].split()[1], "alice", "小明")
    seed_image(public, "alice", "a" * 64)

    response = public.post("/v2/admin/users/alice/delete", headers=auth, json=confirmation)

    assert response.status_code == 409
    assert count(public, AuthUser, AuthUser.id == "alice") == 1
    assert count(public, ClientImage) == 1
    assert gateway(public).deletions == []


def test_有图片时输对用户ID_记录和存储文件各删一次(public: TestClient) -> None:
    auth = bearer(public)
    Device(public, auth["Authorization"].split()[1], "alice", "小明")
    sha = "a" * 64
    seed_image(public, "alice", sha)

    response = public.post(
        "/v2/admin/users/alice/delete", headers=auth, json={"confirmUserId": "alice"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["imageCount"] == 1
    assert count(public, ClientImage) == 0
    assert count(public, ClientImageBatch) == 0
    assert count(public, UploadSession) == 0
    assert count(public, Asset) == 0
    assert count(public, AssetLocation) == 0
    assert sorted(gateway(public).deletions) == sorted(
        [f"labs/images/assets/{sha[:2]}/{sha}", "labs/images/thumbnails/alice-a/thumbnail_512"]
    )
    assert count(public, ClientImageAuditEvent, ClientImageAuditEvent.owner_user_id == "alice") == 1


def test_同一文件被别的用户共用时_只删缩略图不删原文件(public: TestClient) -> None:
    auth = bearer(public)
    token = auth["Authorization"].split()[1]
    Device(public, token, "alice", "小明")
    Device(public, token, "bob", "小红")
    sha = "a" * 64
    seed_image(public, "alice", sha)
    seed_image(public, "bob", sha)

    response = public.post(
        "/v2/admin/users/alice/delete", headers=auth, json={"confirmUserId": "alice"}
    )

    assert response.status_code == 200, response.text
    assert count(public, Asset) == 1
    assert count(public, ClientImage, ClientImage.owner_user_id == "bob") == 1
    assert gateway(public).deletions == ["labs/images/thumbnails/alice-a/thumbnail_512"]


def test_存储文件删除失败不影响用户已删除_并留下告警(
    public: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    auth = bearer(public)
    Device(public, auth["Authorization"].split()[1], "alice", "小明")
    seed_image(public, "alice", "a" * 64)

    def failing_delete(key: str) -> None:
        raise CosImageGatewayError("unavailable")

    gateway(public).delete = failing_delete  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        response = public.post(
            "/v2/admin/users/alice/delete", headers=auth, json={"confirmUserId": "alice"}
        )

    assert response.status_code == 200, response.text
    assert count(public, AuthUser, AuthUser.id == "alice") == 0
    assert "orphan objects" in caplog.text


def test_删除后歌词修订编辑者字段不依赖用户表() -> None:
    assert ScoreRevision.__table__.c.editor_id.foreign_keys == set()


def test_停用立即生效_待用邀请码作废_模板负责人清空_恢复后原设备可用(public: TestClient) -> None:
    auth = bearer(public)
    token = auth["Authorization"].split()[1]
    alice = Device(public, token, "alice", "小明")
    code = invite(public, token, "alice", "小明")
    template = seed_template(public, "alice")
    assert alice.call("GET", "/v2/rooms").status_code == 200

    disabled = public.post(
        "/v2/admin/users/alice/status", headers=auth, json={"status": "disabled"}
    )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json() == {"userId": "alice", "status": "disabled"}
    assert session_rejected(public, alice)
    assert public.post("/v2/device/challenge", json={"inviteCode": code}).status_code != 200
    assert count(public, RegisteredDevice, RegisteredDevice.status == "active") == 1
    with Session(engine(public)) as session:
        assert session.scalar(
            select(RoomTemplateSection.assignee_user_id).where(RoomTemplateSection.template_id == template)
        ) is None

    restored = public.post("/v2/admin/users/alice/status", headers=auth, json={"status": "active"})

    assert restored.json() == {"userId": "alice", "status": "active"}
    assert alice.call("GET", "/v2/rooms").status_code == 200
    assert public.post("/v2/device/challenge", json={"inviteCode": code}).status_code != 200


def test_管理员设备不能停用或删除自己所属的用户(public: TestClient) -> None:
    auth = bearer(public)
    alice = Device(public, auth["Authorization"].split()[1], "alice", "小明")
    device_id = alice.credentials["deviceId"]
    assert public.post(f"/v2/admin/devices/{device_id}/administrator", headers=auth).status_code == 200

    assert alice.call("POST", "/v2/admin/users/alice/delete", {}).status_code == 409
    assert alice.call("POST", "/v2/admin/users/alice/status", {"status": "disabled"}).status_code == 409
    assert count(public, AuthUser, AuthUser.id == "alice", AuthUser.status == "active") == 1


def test_普通设备不能管理用户_不存在的用户返回404(public: TestClient) -> None:
    auth = bearer(public)
    token = auth["Authorization"].split()[1]
    alice = Device(public, token, "alice", "小明")
    Device(public, token, "bob", "小红")

    assert alice.call("POST", "/v2/admin/users/bob/delete", {}).status_code in {401, 403}
    assert public.post("/v2/admin/users/nobody/delete", headers=auth, json={}).status_code == 404
    assert public.get("/v2/admin/users/nobody/deletion-preview", headers=auth).status_code == 404


def test_用户管理写接口走管理员设备的请求体校验() -> None:
    assert _public_admin_device_write_allowed("POST", "/v2/admin/users/alice/delete")
    assert _public_admin_device_write_allowed("POST", "/v2/admin/users/alice/status")
    assert not _public_admin_device_write_allowed("DELETE", "/v2/admin/users/alice")
