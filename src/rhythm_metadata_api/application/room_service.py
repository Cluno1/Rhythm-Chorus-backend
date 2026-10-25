"""issue 124：房间服务——几个人一起为同一次聚会挑选、排序诗歌。

不变量
- 每首歌都属于某个段；段内 ``position`` 从 1 开始连续；同一房间内同一作品最多一首
  （数据库唯一约束兜底）。对外的排序号按「段顺序 → 段内顺序」连续计算。
- 任何改动（加歌、移除、排序）都让 ``Room.revision`` 加 1；排序必须带 If-Match，旧版本号
  返回 412，所以两个人同时拖动不会静默覆盖对方。
- 过期 = 聚会日期次日 00:00（UTC+8）。**懒清理**：每次检索房间（列表、详情、加歌、排序……）
  先在独立事务里删掉已过期的房间并提交；查询本身也按 ``expires_at`` 过滤，清理没跑也不会返回
  过期房间。
  不使用定时任务（用户决定）。
- 权限：所有登记用户可以看全部房间、加歌、排序；移除一首仅限添加人或房间创建者；销毁房间
  仅限创建者。

为什么这样写
- 「段」第一期就进数据结构：以后加管理员模板、段名、段负责人和「我负责的段优先推荐」时
  只新增数据，不用迁移已有歌曲、不改接口形状。新房间有哪些段集中在 ``_initial_sections``，
  第二期在那里按模板生成。
- 固定 UTC+8 偏移而不是 ZoneInfo：中国不实行夏令时，且不依赖容器里有没有 tzdata。

改错会怎样静默出问题
- 排序不校验「提交的歌恰好是房间里的全部歌」：漏传的歌会丢失排序号，重复传的歌会出现两次。
- 加歌/移除不加 revision：别人基于旧列表的排序会覆盖掉刚加的歌。
- 清理只删房间不删段和歌：SQLite 外键级联依赖 PRAGMA；这里显式按顺序删除，不依赖级联。
- 清理和请求放在同一事务：请求以 404/403 结束时清理被一起回滚，过期房间永远删不掉。

已否决
- 定时任务清理（用户不需要）；软删除过期房间（没有恢复需求，只会让表变大）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.catalog_service import (
    ActorContext,
    StoredResponse,
    etag,
)
from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.domain.v2.errors import (
    StaleRevision,
    V2Conflict,
    V2DomainError,
    V2NotFound,
)
from rhythm_metadata_api.domain.v2.rooms import (
    RoomContainedWork,
    RoomCreate,
    RoomItemCreate,
    RoomItemResponse,
    RoomListResponse,
    RoomOrderUpdate,
    RoomResponse,
    RoomSectionResponse,
    RoomSectionSummary,
    RoomSummary,
    RoomUser,
)
from rhythm_metadata_api.domain.v2.schemas import LibrarySearchItem
from rhythm_metadata_api.infrastructure.db.models import (
    AuthUser,
    Room,
    RoomItem,
    RoomSection,
    Work,
    utc_now,
)

LOGGER = logging.getLogger(__name__)


# 聚会日期按北京时间理解；过期 = 次日 00:00。
ROOM_TIMEZONE = timezone(timedelta(hours=8))
# 防止误选年份：最远可以建 90 天后的房间。
ROOM_MAX_DAYS_AHEAD = 90
ROOM_MAX_ITEMS = 200

WorkItems = Callable[[Session, list[Work]], list[LibrarySearchItem]]
Idempotent = Callable[..., StoredResponse]


class RoomForbidden(V2DomainError):
    status_code = 403
    problem_type = "room-forbidden"
    title = "Not allowed in this room"


def room_expires_at(service_date: date) -> datetime:
    """聚会日期当天全天可见，次日 00:00（UTC+8）起过期。

    以 UTC 返回：SQLite 存 DateTime 时会丢掉时区，只能统一存 UTC，否则会差 8 小时。
    """
    local = datetime.combine(service_date + timedelta(days=1), time(0), tzinfo=ROOM_TIMEZONE)
    return local.astimezone(UTC)


def room_today(now: datetime) -> date:
    return now.astimezone(ROOM_TIMEZONE).date()


class RoomService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
        work_items: WorkItems,
        idempotent: Idempotent,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.uow_factory = uow_factory
        self.settings = settings
        self._work_items = work_items
        self._idempotent = idempotent
        self._now = now

    # ---- 读 ----

    def list_rooms(self, actor: ActorContext, work_id: str | None = None) -> RoomListResponse:
        now = self._purge_expired()
        with self.uow_factory() as uow:
            session = uow.session
            rooms = list(
                session.scalars(
                    select(Room)
                    .where(Room.expires_at > now)
                    .order_by(Room.service_date, Room.created_at, Room.id)
                )
            )
            return RoomListResponse(
                items=[self._summary(session, room, actor, work_id) for room in rooms]
            )

    def get_room(self, room_id: str, actor: ActorContext) -> RoomResponse:
        now = self._purge_expired()
        with self.uow_factory() as uow:
            session = uow.session
            room = self._require_room(session, room_id, now)
            return self._response(session, room, actor)

    # ---- 写 ----

    def create_room(
        self, request: RoomCreate, idempotency_key: str, actor: ActorContext
    ) -> StoredResponse:
        now = self._purge_expired()

        def operation(session: Session) -> tuple[RoomResponse, int, dict[str, str]]:
            today = room_today(now)
            if request.service_date < today:
                raise V2DomainError("service_date must not be in the past")
            if request.service_date > today + timedelta(days=ROOM_MAX_DAYS_AHEAD):
                raise V2DomainError(f"service_date must be within {ROOM_MAX_DAYS_AHEAD} days")
            room = Room(
                name=request.name,
                service_date=request.service_date,
                expires_at=room_expires_at(request.service_date),
                created_by_user_id=actor.actor_id,
                revision=1,
            )
            session.add(room)
            session.flush()
            for section in self._initial_sections(room):
                session.add(section)
            session.flush()
            LOGGER.info(
                "room created: room=%s by=%s service_date=%s",
                room.id, actor.actor_id, room.service_date.isoformat(),
            )
            response = self._response(session, room, actor)
            return response, 201, {"Location": f"/v2/rooms/{room.id}", "ETag": etag(room.revision)}

        return self._idempotent("room.create", idempotency_key, request, actor, operation)

    def add_item(
        self,
        room_id: str,
        request: RoomItemCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        now = self._purge_expired()

        def operation(session: Session) -> tuple[RoomResponse, int, dict[str, str]]:
            room = self._require_room(session, room_id, now)
            work = session.get(Work, request.work_id)
            if work is None or work.deleted_at is not None or work.status != "active":
                raise V2NotFound("Work not found")
            existing = session.scalar(
                select(RoomItem).where(RoomItem.room_id == room.id, RoomItem.work_id == work.id)
            )
            if existing is not None:
                added_by = self._users(session, [existing.added_by_user_id])[existing.added_by_user_id]
                raise V2Conflict(
                    "this Work is already in the room",
                    item_id=existing.id,
                    added_by=added_by.model_dump(mode="json"),
                )
            count = session.scalar(
                select(func.count()).select_from(RoomItem).where(RoomItem.room_id == room.id)
            ) or 0
            if count >= ROOM_MAX_ITEMS:
                raise V2Conflict(f"a room holds at most {ROOM_MAX_ITEMS} songs")
            sections = self._sections(session, room.id)
            if request.section_id is None:
                section = sections[-1]
            else:
                section = next((s for s in sections if s.id == request.section_id), None)
                if section is None:
                    raise V2NotFound("section not found in this room")
            last = session.scalar(
                select(func.max(RoomItem.position)).where(RoomItem.section_id == section.id)
            ) or 0
            item = RoomItem(
                room_id=room.id,
                section_id=section.id,
                work_id=work.id,
                position=last + 1,
                added_by_user_id=actor.actor_id,
                added_at=self._now(),
            )
            session.add(item)
            room.revision += 1
            room.updated_at = self._now()
            session.flush()
            LOGGER.info(
                "room song added: room=%s item=%s work=%s by=%s revision=%s",
                room.id, item.id, work.id, actor.actor_id, room.revision,
            )
            response = self._response(session, room, actor)
            return response, 201, {"ETag": etag(room.revision)}

        return self._idempotent(
            f"room.items.add:{room_id}", idempotency_key, request, actor, operation
        )

    def remove_item(self, room_id: str, item_id: str, actor: ActorContext) -> RoomResponse:
        now = self._purge_expired()
        with self.uow_factory() as uow:
            session = uow.session
            room = self._require_room(session, room_id, now)
            item = session.get(RoomItem, item_id)
            if item is None or item.room_id != room.id:
                raise V2NotFound("song not found in this room")
            if actor.actor_id not in {item.added_by_user_id, room.created_by_user_id}:
                LOGGER.warning(
                    "room song removal refused: room=%s item=%s by=%s", room.id, item.id, actor.actor_id
                )
                raise RoomForbidden("only the person who added the song or the room creator can remove it")
            section_id = item.section_id
            session.delete(item)
            session.flush()
            self._renumber(session, section_id)
            room.revision += 1
            room.updated_at = self._now()
            session.flush()
            LOGGER.info(
                "room song removed: room=%s item=%s by=%s revision=%s",
                room.id, item_id, actor.actor_id, room.revision,
            )
            return self._response(session, room, actor)

    def reorder(
        self,
        room_id: str,
        request: RoomOrderUpdate,
        expected_revision: int,
        actor: ActorContext,
    ) -> RoomResponse:
        now = self._purge_expired()
        with self.uow_factory() as uow:
            session = uow.session
            room = self._require_room(session, room_id, now)
            if room.revision != expected_revision:
                LOGGER.warning(
                    "room reorder conflict: room=%s by=%s expected=%s current=%s",
                    room.id, actor.actor_id, expected_revision, room.revision,
                )
                raise StaleRevision(etag(expected_revision), etag(room.revision))
            sections = {section.id: section for section in self._sections(session, room.id)}
            items = {
                item.id: item
                for item in session.scalars(select(RoomItem).where(RoomItem.room_id == room.id))
            }
            submitted_sections = [entry.section_id for entry in request.sections]
            if sorted(submitted_sections) != sorted(sections):
                raise V2DomainError("the new order must list every section of the room exactly once")
            submitted_items = [item_id for entry in request.sections for item_id in entry.item_ids]
            if sorted(submitted_items) != sorted(items):
                raise V2DomainError("the new order must list every song of the room exactly once")
            for entry in request.sections:
                for index, item_id in enumerate(entry.item_ids, start=1):
                    item = items[item_id]
                    item.section_id = entry.section_id
                    item.position = index
            room.revision += 1
            room.updated_at = self._now()
            session.flush()
            LOGGER.info(
                "room reordered: room=%s by=%s songs=%s revision=%s",
                room.id, actor.actor_id, len(items), room.revision,
            )
            return self._response(session, room, actor)

    def delete_room(self, room_id: str, actor: ActorContext) -> None:
        now = self._purge_expired()
        with self.uow_factory() as uow:
            session = uow.session
            room = self._require_room(session, room_id, now)
            if actor.actor_id != room.created_by_user_id:
                LOGGER.warning("room deletion refused: room=%s by=%s", room.id, actor.actor_id)
                raise RoomForbidden("only the room creator can delete the room")
            self._delete_rooms(session, [room.id])
            LOGGER.info("room deleted: room=%s by=%s", room_id, actor.actor_id)

    # ---- 内部 ----

    def _initial_sections(self, room: Room) -> list[RoomSection]:
        """新房间的段。第一期固定一个无名默认段；第二期在这里按 ``room.template_id`` 生成。"""
        return [RoomSection(room_id=room.id, name=None, position=1)]

    def _purge_expired(self) -> datetime:
        """懒清理：在**独立事务**里删掉已过期的房间（连同段和歌）并提交，返回本次的当前时间。

        必须独立提交：如果和请求本身同一个事务，请求以 404 / 403 等错误结束时会回滚，
        已过期的房间就永远删不掉。
        """
        now = self._now()
        with self.uow_factory() as uow:
            session = uow.session
            expired = list(session.scalars(select(Room.id).where(Room.expires_at <= now)))
            if expired:
                self._delete_rooms(session, expired)
                LOGGER.info("expired rooms purged: count=%s", len(expired))
        return now

    @staticmethod
    def _delete_rooms(session: Session, room_ids: list[str]) -> None:
        session.execute(delete(RoomItem).where(RoomItem.room_id.in_(room_ids)))
        session.execute(delete(RoomSection).where(RoomSection.room_id.in_(room_ids)))
        session.execute(delete(Room).where(Room.id.in_(room_ids)))
        session.flush()

    @staticmethod
    def _require_room(session: Session, room_id: str, now: datetime) -> Room:
        room = session.get(Room, room_id)
        if room is None or _aware(room.expires_at) <= now:
            raise V2NotFound("room not found")
        return room

    @staticmethod
    def _sections(session: Session, room_id: str) -> list[RoomSection]:
        return list(
            session.scalars(
                select(RoomSection)
                .where(RoomSection.room_id == room_id)
                .order_by(RoomSection.position, RoomSection.id)
            )
        )

    @staticmethod
    def _items(session: Session, room_id: str) -> list[RoomItem]:
        return list(
            session.scalars(
                select(RoomItem).where(RoomItem.room_id == room_id).order_by(RoomItem.position, RoomItem.id)
            )
        )

    @staticmethod
    def _renumber(session: Session, section_id: str) -> None:
        remaining = session.scalars(
            select(RoomItem)
            .where(RoomItem.section_id == section_id)
            .order_by(RoomItem.position, RoomItem.id)
        )
        for index, item in enumerate(remaining, start=1):
            item.position = index

    @staticmethod
    def _users(session: Session, user_ids: list[str]) -> dict[str, RoomUser]:
        wanted = {user_id for user_id in user_ids if user_id}
        names = {
            user.id: (user.display_name or "").strip() or None
            for user in session.scalars(select(AuthUser).where(AuthUser.id.in_(wanted)))
        }
        return {user_id: RoomUser(user_id=user_id, display_name=names.get(user_id)) for user_id in wanted}

    def _summary(
        self, session: Session, room: Room, actor: ActorContext, work_id: str | None
    ) -> RoomSummary:
        sections = self._sections(session, room.id)
        items = self._items(session, room.id)
        users = self._users(
            session,
            [room.created_by_user_id]
            + [section.assignee_user_id for section in sections if section.assignee_user_id]
            + [item.added_by_user_id for item in items],
        )
        contains = None
        if work_id is not None:
            match = next((item for item in items if item.work_id == work_id), None)
            if match is not None:
                contains = RoomContainedWork(
                    item_id=match.id,
                    section_id=match.section_id,
                    position=self._ordinal(sections, items, match),
                    added_by=users[match.added_by_user_id],
                )
        return RoomSummary(
            id=room.id,
            name=room.name,
            service_date=room.service_date,
            expires_at=_aware(room.expires_at),
            created_by=users[room.created_by_user_id],
            item_count=len(items),
            revision=room.revision,
            can_delete=actor.actor_id == room.created_by_user_id,
            sections=[
                RoomSectionSummary(
                    id=section.id,
                    name=section.name,
                    position=section.position,
                    assignee=users.get(section.assignee_user_id) if section.assignee_user_id else None,
                    item_count=sum(1 for item in items if item.section_id == section.id),
                )
                for section in sections
            ],
            contains_work=contains,
        )

    @staticmethod
    def _ordinal(sections: list[RoomSection], items: list[RoomItem], target: RoomItem) -> int:
        ordinal = 0
        for section in sections:
            for item in sorted((i for i in items if i.section_id == section.id), key=lambda i: (i.position, i.id)):
                ordinal += 1
                if item.id == target.id:
                    return ordinal
        return ordinal

    def _response(self, session: Session, room: Room, actor: ActorContext) -> RoomResponse:
        sections = self._sections(session, room.id)
        items = self._items(session, room.id)
        works = {
            work.id: work
            for work in session.scalars(
                select(Work).where(
                    Work.id.in_({item.work_id for item in items}), Work.deleted_at.is_(None)
                )
            )
        }
        # 作品已被删除时这首歌不再显示；排序号仍然连续。
        visible = [item for item in items if item.work_id in works]
        work_list = list(works.values())
        search_items = {
            entry.work_id: entry for entry in self._work_items(session, work_list)
        } if work_list else {}
        users = self._users(
            session,
            [room.created_by_user_id]
            + [section.assignee_user_id for section in sections if section.assignee_user_id]
            + [item.added_by_user_id for item in visible],
        )
        ordinal = 0
        section_responses = []
        for section in sections:
            section_items = sorted(
                (item for item in visible if item.section_id == section.id),
                key=lambda item: (item.position, item.id),
            )
            responses = []
            for index, item in enumerate(section_items, start=1):
                ordinal += 1
                responses.append(
                    RoomItemResponse(
                        id=item.id,
                        position=ordinal,
                        section_position=index,
                        work=search_items[item.work_id],
                        added_by=users[item.added_by_user_id],
                        added_at=_aware(item.added_at),
                        can_remove=actor.actor_id in {item.added_by_user_id, room.created_by_user_id},
                    )
                )
            section_responses.append(
                RoomSectionResponse(
                    id=section.id,
                    name=section.name,
                    position=section.position,
                    assignee=users.get(section.assignee_user_id) if section.assignee_user_id else None,
                    items=responses,
                )
            )
        return RoomResponse(
            id=room.id,
            name=room.name,
            service_date=room.service_date,
            expires_at=_aware(room.expires_at),
            created_by=users[room.created_by_user_id],
            item_count=ordinal,
            revision=room.revision,
            can_delete=actor.actor_id == room.created_by_user_id,
            sections=section_responses,
        )


def _aware(value: datetime) -> datetime:
    """SQLite 读回的时间没有时区信息，统一当作 UTC。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
