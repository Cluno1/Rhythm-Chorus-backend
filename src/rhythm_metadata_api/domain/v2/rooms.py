"""issue 124：房间——几个人一起为同一次聚会挑选并排序要唱的诗歌。

接口按「段」返回（第一期每个房间只有一个无名默认段），以后加管理员模板、段名和段负责人
时只是多出数据，接口形状不变。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, field_validator

from rhythm_metadata_api.domain.v2.schemas import ApiModel, LibrarySearchItem


class RoomCreate(ApiModel):
    name: str = Field(min_length=1, max_length=60)
    service_date: date

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        stripped = " ".join(value.split())
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class RoomItemCreate(ApiModel):
    work_id: str = Field(min_length=1, max_length=36)
    # 不传时放进最后一段；第二期「我负责的段」会由客户端显式传入。
    section_id: str | None = Field(default=None, max_length=36)


class RoomOrderSection(ApiModel):
    section_id: str = Field(min_length=1, max_length=36)
    item_ids: list[str] = Field(max_length=500)


class RoomOrderUpdate(ApiModel):
    """房间里每一段的完整新顺序；同一个接口也能把歌跨段移动。"""

    sections: list[RoomOrderSection] = Field(min_length=1, max_length=50)


class RoomUser(ApiModel):
    user_id: str
    # 没设昵称时为 null，由客户端显示兜底名称。
    display_name: str | None = None


class RoomContainedWork(ApiModel):
    """列表带 work_id 查询时：这首歌已在房间里（「已添加 · 由 某某」）。"""

    item_id: str
    section_id: str
    position: int
    added_by: RoomUser


class RoomSectionSummary(ApiModel):
    id: str
    name: str | None = None
    position: int
    assignee: RoomUser | None = None
    item_count: int


class RoomSummary(ApiModel):
    id: str
    name: str
    service_date: date
    expires_at: datetime
    created_by: RoomUser
    item_count: int
    revision: int
    can_delete: bool
    sections: list[RoomSectionSummary]
    contains_work: RoomContainedWork | None = None


class RoomListResponse(ApiModel):
    items: list[RoomSummary]


class RoomItemResponse(ApiModel):
    id: str
    # 整个房间从 1 开始连续的排序号（段顺序 → 段内顺序）。
    position: int
    section_position: int
    work: LibrarySearchItem
    added_by: RoomUser
    added_at: datetime
    can_remove: bool


class RoomSectionResponse(ApiModel):
    id: str
    name: str | None = None
    position: int
    assignee: RoomUser | None = None
    items: list[RoomItemResponse]


class RoomResponse(ApiModel):
    id: str
    name: str
    service_date: date
    expires_at: datetime
    created_by: RoomUser
    item_count: int
    revision: int
    can_delete: bool
    sections: list[RoomSectionResponse]
