"""issue 183：管理员停用、恢复、物理删除客户端用户。

不变量
- 删除在一个数据库事务里完成：用户、设备、会话、邀请码、图片记录、该用户创建的房间要么全没，
  要么全在。对象存储文件在事务提交之后才删。
- 用户名下还有图片时，后端必须收到与用户 ID 完全一致的 ``confirm_user_id`` 才删除。预览和删除
  之间用户新传了图片时，“简单确认”的请求会被 409 拦下，不依赖页面判断。
- 文件按 sha256 去重：只有不再被任何人或曲库引用，且所有位置都在图片桶里的资产才删除。
- 保留：歌词修订 ``editor_id``、公告创建人、审计记录、别人房间里该用户加的歌。这些是普通文本
  字段，用户删除后只剩 ID。
- 设备身份的管理员不能停用或删除自己；Web 管理员账号不在用户表里，不受此限制。

为什么这样写
- 外键级联只会删图片记录，资产行和存储文件会变成垃圾，所以这里显式按顺序删除。
- 停用不撤销设备和会话：鉴权每次都检查用户状态，立即生效，恢复后原设备可继续使用。
- 停用和删除都清空模板里的负责人：模板保存校验只接受正常状态的用户，留着会让模板无法再保存。

改错会怎样静默出问题
- 不做共享检查：删一个用户会让另一个用户或曲库的同一文件 404，而数据库里一切正常。
- 文件删除放进事务之前：事务回滚后文件已经没了，用户和图片记录却还在。
- 停用不让待用邀请码失效：恢复用户后旧邀请码又能绑定新设备。

接受的残留
- 事务提交后存储文件删除失败：只留下没有数据库记录的孤立文件，方向无害，记 warning，
  不把已成功的删除报成失败。

已否决
- 保留图片并把所有者改为可空（用户决定随用户一起删除）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.catalog_service import ActorContext, CatalogService
from rhythm_metadata_api.application.client_image_service import ClientImageService
from rhythm_metadata_api.application.device_auth import DeviceAuthError
from rhythm_metadata_api.application.room_service import RoomService
from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.models import (
    Asset,
    AssetLocation,
    AssetSource,
    AuthAuditEvent,
    AuthUser,
    ClientImage,
    ClientImageAuditEvent,
    ClientImageBatch,
    DeviceInvite,
    DeviceSession,
    RegisteredDevice,
    Room,
    RoomSection,
    RoomTemplate,
    RoomTemplateSection,
    UploadSession,
    UserImageAdminVisibility,
    utc_now,
)
from rhythm_metadata_api.infrastructure.storage.cos_images import CosImageGatewayError

LOGGER = logging.getLogger(__name__)

_INACTIVE_IMAGE_STATES = ("deleted", "rejected", "cancelled")
_UNFINISHED_IMAGE_STATES = ("upload_pending", "verifying")
UserStatus = Literal["active", "disabled"]


@dataclass(frozen=True)
class UserDeletionImpact:
    user_id: str
    device_count: int
    room_count: int
    image_count: int


class UserAdminService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
        client_images: ClientImageService,
    ) -> None:
        self.uow_factory = uow_factory
        self.settings = settings
        self.client_images = client_images

    def deletion_preview(self, user_id: str) -> UserDeletionImpact:
        with self.uow_factory() as uow:
            self._require_user(uow.session, user_id)
            return self._impact(uow.session, user_id)

    def set_status(self, user_id: str, status: UserStatus, actor: ActorContext) -> UserStatus:
        if status == "disabled":
            self._refuse_own_user(user_id, actor)
        now = utc_now()
        with self.uow_factory() as uow:
            session = uow.session
            user = self._require_user(session, user_id)
            if user.status == status:
                return status
            user.status = status
            user.updated_at = now
            if status == "disabled":
                session.execute(
                    update(DeviceInvite)
                    .where(
                        DeviceInvite.user_id == user_id,
                        DeviceInvite.consumed_at.is_(None),
                        DeviceInvite.expires_at > now,
                    )
                    .values(expires_at=now)
                )
                self._clear_template_assignees(session, user_id)
            self._audit(session, "user_disabled" if status == "disabled" else "user_enabled", actor)
        LOGGER.info("user status changed: user=%s status=%s by=%s", user_id, status, actor.actor_id)
        return status

    def delete(
        self, user_id: str, confirm_user_id: str | None, actor: ActorContext
    ) -> UserDeletionImpact:
        self._refuse_own_user(user_id, actor)
        with self.uow_factory() as uow:
            session = uow.session
            self._require_user(session, user_id)
            impact = self._impact(session, user_id)
            if impact.image_count and confirm_user_id != user_id:
                LOGGER.warning(
                    "user deletion refused without typed confirmation: user=%s images=%s by=%s",
                    user_id, impact.image_count, actor.actor_id,
                )
                raise DeviceAuthError(409, "typed user id confirmation is required")
            object_keys = self._delete_images(session, user_id)
            room_ids = list(
                session.scalars(select(Room.id).where(Room.created_by_user_id == user_id))
            )
            if room_ids:
                RoomService._delete_rooms(session, room_ids)
            self._clear_room_assignees(session, user_id)
            self._clear_template_assignees(session, user_id)
            device_ids = select(RegisteredDevice.id).where(RegisteredDevice.user_id == user_id)
            session.execute(delete(DeviceSession).where(DeviceSession.device_id.in_(device_ids)))
            session.execute(delete(RegisteredDevice).where(RegisteredDevice.user_id == user_id))
            session.execute(delete(DeviceInvite).where(DeviceInvite.user_id == user_id))
            session.execute(
                delete(UserImageAdminVisibility).where(
                    UserImageAdminVisibility.owner_user_id == user_id
                )
            )
            session.execute(delete(AuthUser).where(AuthUser.id == user_id))
            if impact.image_count or object_keys:
                session.add(
                    ClientImageAuditEvent(
                        owner_user_id=user_id,
                        actor_id=actor.actor_id,
                        device_id=actor.device_id,
                        operation="client_image.owner_deleted",
                        details_json={
                            "image_count": impact.image_count,
                            "object_count": len(object_keys),
                        },
                    )
                )
            self._audit(session, "user_deleted", actor)
        failed = self._delete_objects(object_keys)
        LOGGER.info(
            "user deleted: user=%s devices=%s rooms=%s images=%s objects=%s objects_failed=%s by=%s",
            user_id, impact.device_count, impact.room_count, impact.image_count,
            len(object_keys), failed, actor.actor_id,
        )
        return impact

    # ---- 内部 ----

    @staticmethod
    def _require_user(session: Session, user_id: str) -> AuthUser:
        user = session.get(AuthUser, user_id)
        if user is None:
            raise DeviceAuthError(404, "user not found")
        return user

    @staticmethod
    def _refuse_own_user(user_id: str, actor: ActorContext) -> None:
        if actor.device_id is not None and actor.actor_id == user_id:
            LOGGER.warning("user change refused on own account: user=%s", user_id)
            raise DeviceAuthError(409, "administrator devices cannot change their own user")

    @staticmethod
    def _impact(session: Session, user_id: str) -> UserDeletionImpact:
        devices = session.scalar(
            select(func.count(RegisteredDevice.id)).where(RegisteredDevice.user_id == user_id)
        )
        rooms = session.scalar(
            select(func.count(Room.id)).where(
                Room.created_by_user_id == user_id, Room.expires_at > utc_now()
            )
        )
        images = session.scalar(
            select(func.count(ClientImage.id)).where(
                ClientImage.owner_user_id == user_id,
                ClientImage.deleted_at.is_(None),
                ClientImage.state.not_in(_INACTIVE_IMAGE_STATES),
            )
        )
        return UserDeletionImpact(user_id, devices or 0, rooms or 0, images or 0)

    def _delete_images(self, session: Session, user_id: str) -> list[str]:
        """Remove every image row of the user and return object keys to delete after commit."""
        images = list(
            session.scalars(select(ClientImage).where(ClientImage.owner_user_id == user_id))
        )
        object_keys: list[str] = []
        image_ids = [image.id for image in images]
        upload_ids = [image.upload_session_id for image in images]
        asset_ids = {image.asset_id for image in images if image.asset_id}
        uploads = {
            upload.id: upload
            for upload in session.scalars(
                select(UploadSession).where(UploadSession.id.in_(upload_ids))
            )
        }
        for image in images:
            _, separator, thumbnail_key = (image.thumbnail_storage_key or "").partition("/")
            if separator and thumbnail_key:
                object_keys.append(thumbnail_key)
            upload = uploads.get(image.upload_session_id)
            if upload is not None and image.state in _UNFINISHED_IMAGE_STATES:
                temporary_key = ClientImageService._temporary_object_key(upload)
                if temporary_key:
                    object_keys.append(temporary_key)
                    object_keys.append(
                        ClientImageService._thumbnail_temporary_object_key(temporary_key)
                    )
        session.execute(delete(ClientImage).where(ClientImage.owner_user_id == user_id))
        session.execute(delete(ClientImageBatch).where(ClientImageBatch.owner_user_id == user_id))
        if upload_ids:
            session.execute(delete(UploadSession).where(UploadSession.id.in_(upload_ids)))
        if image_ids:
            session.execute(
                delete(AssetSource).where(
                    AssetSource.source == "labs_image", AssetSource.source_ref.in_(image_ids)
                )
            )
        session.flush()
        for asset_id in asset_ids:
            object_keys.extend(self._delete_unshared_asset(session, asset_id))
        return object_keys

    def _delete_unshared_asset(self, session: Session, asset_id: str) -> list[str]:
        if CatalogService._asset_has_live_business_reference(session, asset_id):
            return []
        locations = list(
            session.scalars(select(AssetLocation).where(AssetLocation.asset_id == asset_id))
        )
        keys: list[str] = []
        for location in locations:
            bucket, separator, key = location.storage_key.partition("/")
            if (
                location.provider != "cos"
                or bucket != self.settings.client_image_cos_bucket
                or not separator
                or not key
            ):
                return []
            keys.append(key)
        session.execute(delete(AssetLocation).where(AssetLocation.asset_id == asset_id))
        session.execute(delete(AssetSource).where(AssetSource.asset_id == asset_id))
        session.execute(delete(Asset).where(Asset.id == asset_id))
        return keys

    def _delete_objects(self, object_keys: list[str]) -> int:
        failed = 0
        for key in object_keys:
            try:
                self.client_images.object_gateway.delete(key)
            except CosImageGatewayError:
                failed += 1
        if failed:
            LOGGER.warning(
                "user deletion left orphan objects: failed=%s total=%s", failed, len(object_keys)
            )
        return failed

    @staticmethod
    def _clear_template_assignees(session: Session, user_id: str) -> None:
        template_ids = set(
            session.scalars(
                select(RoomTemplateSection.template_id).where(
                    RoomTemplateSection.assignee_user_id == user_id
                )
            )
        )
        if not template_ids:
            return
        session.execute(
            update(RoomTemplateSection)
            .where(RoomTemplateSection.assignee_user_id == user_id)
            .values(assignee_user_id=None)
        )
        session.execute(
            update(RoomTemplate)
            .where(RoomTemplate.id.in_(template_ids))
            .values(revision=RoomTemplate.revision + 1, updated_at=utc_now())
        )

    @staticmethod
    def _clear_room_assignees(session: Session, user_id: str) -> None:
        room_ids = set(
            session.scalars(
                select(RoomSection.room_id).where(RoomSection.assignee_user_id == user_id)
            )
        )
        if not room_ids:
            return
        session.execute(
            update(RoomSection)
            .where(RoomSection.assignee_user_id == user_id)
            .values(assignee_user_id=None)
        )
        session.execute(
            update(Room)
            .where(Room.id.in_(room_ids))
            .values(revision=Room.revision + 1, updated_at=utc_now())
        )

    @staticmethod
    def _audit(session: Session, event_type: str, actor: ActorContext) -> None:
        session.add(
            AuthAuditEvent(
                actor_type="admin",
                actor_id=actor.actor_id,
                event_type=event_type,
                result="success",
            )
        )
