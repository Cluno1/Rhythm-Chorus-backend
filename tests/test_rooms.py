"""issue 124：房间——多人共同挑选、排序聚会诗歌。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_public_device_auth import admin_token, enroll, settings, signed_headers

from rhythm_metadata_api.application.room_service import room_expires_at, room_today
from rhythm_metadata_api.domain.v2.rooms import RoomCreate, RoomItemCreate, RoomOrderUpdate
from rhythm_metadata_api.infrastructure.db.models import Room, RoomItem, RoomSection, Work
from rhythm_metadata_api.public_main import (
    _public_read_allowed,
    _public_write_allowed,
    create_public_app,
)

SUNDAY = date(2026, 11, 1)


def invite(client: TestClient, admin: str, user_id: str, display_name: str) -> str:
    response = client.post(
        "/v2/admin/invites",
        headers={"Authorization": f"Bearer {admin}"},
        json={"userId": user_id, "displayName": display_name, "replaceExistingDevice": False},
    )
    assert response.status_code == 200, response.text
    return response.json()["inviteCode"]


class Device:
    """一台已登记设备：每次请求都现签名。"""

    def __init__(self, client: TestClient, admin: str, user_id: str, display_name: str) -> None:
        self.client = client
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credentials = enroll(client, invite(client, admin, user_id, display_name), self.key)

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        query: str = "",
        headers: dict[str, str] | None = None,
    ):
        raw = b"" if body is None else json.dumps(body).encode()
        signed = signed_headers(self.client, self.credentials, self.key, path, query, method, raw)
        url = f"{path}?{query}" if query else path
        extra = {"Content-Type": "application/json"} if body is not None else {}
        return self.client.request(method, url, content=raw or None, headers={**signed, **extra, **(headers or {})})


def add_works(client: TestClient, count: int) -> list[str]:
    engine = client.app.state.v2_container.engine
    ids = []
    with Session(engine) as session:
        for number in range(1, count + 1):
            work = Work(
                canonical_title=f"诗歌 {number}",
                status="active",
                number_meta=[{"ihope_num": number}],
                lyrics=f"第 {number} 首的歌词",
            )
            session.add(work)
            session.flush()
            ids.append(work.id)
        session.commit()
    return ids


@pytest.fixture
def public(tmp_path: Path):
    with TestClient(create_public_app(settings(tmp_path, max_active_devices=4))) as client:
        yield client


def pin_clock(client: TestClient, now: datetime) -> None:
    client.app.state.v2_container.rooms._now = lambda: now


def ordered(room: dict[str, Any]) -> list[tuple[int, str, str | None]]:
    return [
        (item["position"], item["work"]["title"], item["added_by"]["display_name"])
        for section in room["sections"]
        for item in section["items"]
    ]


def test_两人各加三首后房间按顺序列出六首并显示添加人(public: TestClient) -> None:
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    bob = Device(public, admin, "bob", "小红")
    works = add_works(public, 6)

    created = alice.call(
        "POST", "/v2/rooms", {"name": " 周日 聚会 ", "service_date": SUNDAY.isoformat()},
        headers={"Idempotency-Key": "room-1"},
    )
    assert created.status_code == 201, created.text
    room = created.json()
    assert room["name"] == "周日 聚会"
    assert room["created_by"] == {"user_id": "alice", "display_name": "小明"}
    assert room["can_delete"] is True
    # 第一期：一个无名默认段。
    assert [(s["name"], s["position"], s["assignee"]) for s in room["sections"]] == [(None, 1, None)]

    for index, work_id in enumerate(works[:3]):
        added = alice.call(
            "POST", f"/v2/rooms/{room['id']}/items", {"work_id": work_id},
            headers={"Idempotency-Key": f"a-{index}"},
        )
        assert added.status_code == 201, added.text
    for index, work_id in enumerate(works[3:]):
        added = bob.call(
            "POST", f"/v2/rooms/{room['id']}/items", {"work_id": work_id},
            headers={"Idempotency-Key": f"b-{index}"},
        )
        assert added.status_code == 201, added.text

    detail = bob.call("GET", f"/v2/rooms/{room['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.headers["ETag"] == '"rev-7"'
    assert ordered(detail.json()) == [
        (1, "诗歌 1", "小明"), (2, "诗歌 2", "小明"), (3, "诗歌 3", "小明"),
        (4, "诗歌 4", "小红"), (5, "诗歌 5", "小红"), (6, "诗歌 6", "小红"),
    ]
    first = detail.json()["sections"][0]["items"][0]
    # 歌曲行与序号搜索同一结构，客户端复用同一套行组件。
    assert first["work"]["numbers"] == [{"key": "ihope_num", "value": 1}]
    assert first["work"]["match_field"] == "room"
    # bob 不是创建者：看不到销毁、只能移除自己加的歌。
    assert detail.json()["can_delete"] is False
    assert [item["can_remove"] for item in detail.json()["sections"][0]["items"]] == [
        False, False, False, True, True, True
    ]

    listed = bob.call("GET", "/v2/rooms", query=f"work_id={works[4]}")
    assert listed.status_code == 200, listed.text
    summary = listed.json()["items"][0]
    assert (summary["item_count"], summary["service_date"]) == (6, "2026-11-01")
    assert summary["contains_work"]["position"] == 5
    assert summary["contains_work"]["added_by"]["display_name"] == "小红"


def test_所有人都能排序且旧版本号的排序被拒绝(public: TestClient) -> None:
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    bob = Device(public, admin, "bob", "小红")
    works = add_works(public, 3)
    room = alice.call(
        "POST", "/v2/rooms", {"name": "周日", "service_date": SUNDAY.isoformat()},
        headers={"Idempotency-Key": "room"},
    ).json()
    for index, work_id in enumerate(works):
        alice.call(
            "POST", f"/v2/rooms/{room['id']}/items", {"work_id": work_id},
            headers={"Idempotency-Key": f"add-{index}"},
        )
    current = bob.call("GET", f"/v2/rooms/{room['id']}").json()
    section = current["sections"][0]
    reversed_ids = [item["id"] for item in reversed(section["items"])]
    body = {"sections": [{"section_id": section["id"], "item_ids": reversed_ids}]}

    moved = bob.call(
        "PUT", f"/v2/rooms/{room['id']}/order", body, headers={"If-Match": f'"rev-{current["revision"]}"'}
    )
    assert moved.status_code == 200, moved.text
    assert [title for _, title, _ in ordered(moved.json())] == ["诗歌 3", "诗歌 2", "诗歌 1"]
    assert moved.json()["revision"] == current["revision"] + 1

    stale = alice.call(
        "PUT", f"/v2/rooms/{room['id']}/order", body, headers={"If-Match": f'"rev-{current["revision"]}"'}
    )
    assert stale.status_code == 412, stale.text
    assert stale.json()["current_etag"] == f'"rev-{current["revision"] + 1}"'

    latest = f'"rev-{moved.json()["revision"]}"'
    for bad in (
        {"sections": [{"section_id": section["id"], "item_ids": reversed_ids[:2]}]},
        {"sections": [{"section_id": section["id"], "item_ids": reversed_ids + reversed_ids[:1]}]},
        {"sections": [{"section_id": "missing", "item_ids": reversed_ids}]},
    ):
        rejected = alice.call("PUT", f"/v2/rooms/{room['id']}/order", bad, headers={"If-Match": latest})
        assert rejected.status_code == 422, rejected.text
    assert [title for _, title, _ in ordered(bob.call("GET", f"/v2/rooms/{room['id']}").json())] == [
        "诗歌 3", "诗歌 2", "诗歌 1"
    ]


def test_移除仅限添加人或创建者且排序号保持连续(public: TestClient) -> None:
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    bob = Device(public, admin, "bob", "小红")
    carol = Device(public, admin, "carol", "")
    works = add_works(public, 3)
    room = alice.call(
        "POST", "/v2/rooms", {"name": "周日", "service_date": SUNDAY.isoformat()},
        headers={"Idempotency-Key": "room"},
    ).json()
    bob.call("POST", f"/v2/rooms/{room['id']}/items", {"work_id": works[0]}, headers={"Idempotency-Key": "b0"})
    bob.call("POST", f"/v2/rooms/{room['id']}/items", {"work_id": works[1]}, headers={"Idempotency-Key": "b1"})
    carol.call("POST", f"/v2/rooms/{room['id']}/items", {"work_id": works[2]}, headers={"Idempotency-Key": "c2"})
    items = alice.call("GET", f"/v2/rooms/{room['id']}").json()["sections"][0]["items"]
    # 没有昵称的用户 display_name 为 null，由客户端显示兜底名称。
    assert items[2]["added_by"] == {"user_id": "carol", "display_name": None}

    refused = carol.call("DELETE", f"/v2/rooms/{room['id']}/items/{items[0]['id']}")
    assert refused.status_code == 403, refused.text
    by_creator = alice.call("DELETE", f"/v2/rooms/{room['id']}/items/{items[0]['id']}")
    assert by_creator.status_code == 200, by_creator.text
    by_adder = carol.call("DELETE", f"/v2/rooms/{room['id']}/items/{items[2]['id']}")
    assert by_adder.status_code == 200, by_adder.text
    assert ordered(by_adder.json()) == [(1, "诗歌 2", "小红")]
    assert by_adder.json()["sections"][0]["items"][0]["section_position"] == 1


def test_同一首歌不能重复添加且同一请求重放只加一次(public: TestClient) -> None:
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    bob = Device(public, admin, "bob", "小红")
    (work_id,) = add_works(public, 1)
    room = alice.call(
        "POST", "/v2/rooms", {"name": "周日", "service_date": SUNDAY.isoformat()},
        headers={"Idempotency-Key": "room"},
    ).json()
    path = f"/v2/rooms/{room['id']}/items"
    first = alice.call("POST", path, {"work_id": work_id}, headers={"Idempotency-Key": "same"})
    replay = alice.call("POST", path, {"work_id": work_id}, headers={"Idempotency-Key": "same"})
    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.headers["Idempotency-Replayed"] == "true"

    duplicate = bob.call("POST", path, {"work_id": work_id}, headers={"Idempotency-Key": "other"})
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["added_by"]["display_name"] == "小明"

    with Session(public.app.state.v2_container.engine) as session:
        # 写操作恰好执行一次：重放与重复都没有多出第二行。
        assert session.scalar(select(func.count()).select_from(RoomItem)) == 1
        assert session.get(Room, room["id"]).revision == 2


def test_只有创建者能销毁房间(public: TestClient) -> None:
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    bob = Device(public, admin, "bob", "小红")
    (work_id,) = add_works(public, 1)
    room = alice.call(
        "POST", "/v2/rooms", {"name": "周日", "service_date": SUNDAY.isoformat()},
        headers={"Idempotency-Key": "room"},
    ).json()
    bob.call("POST", f"/v2/rooms/{room['id']}/items", {"work_id": work_id}, headers={"Idempotency-Key": "b"})

    assert bob.call("DELETE", f"/v2/rooms/{room['id']}").status_code == 403
    assert alice.call("DELETE", f"/v2/rooms/{room['id']}").status_code == 204
    assert bob.call("GET", f"/v2/rooms/{room['id']}").status_code == 404
    assert bob.call("GET", "/v2/rooms").json() == {"items": []}
    with Session(public.app.state.v2_container.engine) as session:
        assert session.scalar(select(func.count()).select_from(RoomItem)) == 0
        assert session.scalar(select(func.count()).select_from(RoomSection)) == 0


def test_聚会当天全天可见次日零点起检索时懒清理(public: TestClient) -> None:
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    (work_id,) = add_works(public, 1)
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    room = alice.call(
        "POST", "/v2/rooms", {"name": "周日", "service_date": SUNDAY.isoformat()},
        headers={"Idempotency-Key": "room"},
    ).json()
    alice.call("POST", f"/v2/rooms/{room['id']}/items", {"work_id": work_id}, headers={"Idempotency-Key": "a"})
    # 11 月 1 日 23:59（北京时间）仍可见。
    pin_clock(public, datetime(2026, 11, 1, 15, 59, tzinfo=UTC))
    assert [item["id"] for item in alice.call("GET", "/v2/rooms").json()["items"]] == [room["id"]]
    engine = public.app.state.v2_container.engine
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Room)) == 1
    # 11 月 2 日 00:00（北京时间）起：检索时顺便删除，不靠定时任务。
    pin_clock(public, datetime(2026, 11, 1, 16, 0, tzinfo=UTC))
    assert alice.call("GET", f"/v2/rooms/{room['id']}").status_code == 404
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Room)) == 0
        assert session.scalar(select(func.count()).select_from(RoomSection)) == 0
        assert session.scalar(select(func.count()).select_from(RoomItem)) == 0


def test_聚会日期不能是过去也不能超过九十天(public: TestClient) -> None:
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    for index, service_date in enumerate((date(2026, 10, 24), date(2026, 10, 25) + timedelta(days=91))):
        rejected = alice.call(
            "POST", "/v2/rooms", {"name": "周日", "service_date": service_date.isoformat()},
            headers={"Idempotency-Key": f"bad-{index}"},
        )
        assert rejected.status_code == 422, rejected.text
    today = alice.call(
        "POST", "/v2/rooms", {"name": "今天", "service_date": "2026-10-25"},
        headers={"Idempotency-Key": "today"},
    )
    assert today.status_code == 201, today.text
    blank = alice.call(
        "POST", "/v2/rooms", {"name": "   ", "service_date": SUNDAY.isoformat()},
        headers={"Idempotency-Key": "blank"},
    )
    assert blank.status_code == 422


def test_过期时间按北京时间次日零点并以UTC存储() -> None:
    assert room_expires_at(SUNDAY) == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)
    assert room_today(datetime(2026, 10, 31, 16, 30, tzinfo=UTC)) == date(2026, 11, 1)


def test_房间接口的公网放行与写权限() -> None:
    assert _public_read_allowed("GET", "/v2/rooms")
    assert _public_read_allowed("GET", "/v2/rooms/abc")
    for method, path in (
        ("POST", "/v2/rooms"),
        ("DELETE", "/v2/rooms/abc"),
        ("POST", "/v2/rooms/abc/items"),
        ("DELETE", "/v2/rooms/abc/items/def"),
        ("PUT", "/v2/rooms/abc/order"),
    ):
        assert _public_write_allowed(method, path), (method, path)
    assert not _public_write_allowed("PATCH", "/v2/rooms/abc")


def test_段结构为模板预留且加歌可以指定段(public: TestClient) -> None:
    """第二期的模板会建出多段；这里直接造两段，验证接口按段返回、跨段排序可用。"""
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    works = add_works(public, 2)
    room = alice.call(
        "POST", "/v2/rooms", {"name": "周日", "service_date": SUNDAY.isoformat()},
        headers={"Idempotency-Key": "room"},
    ).json()
    with Session(public.app.state.v2_container.engine) as session:
        session.query(RoomSection).filter_by(room_id=room["id"]).update({"name": "开场"})
        session.add(RoomSection(room_id=room["id"], name="结束", position=2, assignee_user_id="alice"))
        session.commit()
    sections = alice.call("GET", f"/v2/rooms/{room['id']}").json()["sections"]
    assert [(s["name"], s["assignee"]) for s in sections] == [
        ("开场", None), ("结束", {"user_id": "alice", "display_name": "小明"})
    ]
    # 不指定段 → 最后一段；指定段 → 那一段。
    alice.call("POST", f"/v2/rooms/{room['id']}/items", {"work_id": works[0]}, headers={"Idempotency-Key": "x"})
    detail = alice.call(
        "POST", f"/v2/rooms/{room['id']}/items",
        {"work_id": works[1], "section_id": sections[0]["id"]}, headers={"Idempotency-Key": "y"},
    ).json()
    assert [(s["name"], [i["work"]["title"] for i in s["items"]]) for s in detail["sections"]] == [
        ("开场", ["诗歌 2"]), ("结束", ["诗歌 1"])
    ]
    assert ordered(detail)[0][0] == 1 and ordered(detail)[1][0] == 2
    # 跨段移动：把「诗歌 1」移到开场段末尾。
    item_ids = {i["work"]["title"]: i["id"] for s in detail["sections"] for i in s["items"]}
    moved = alice.call(
        "PUT", f"/v2/rooms/{room['id']}/order",
        {"sections": [
            {"section_id": sections[0]["id"], "item_ids": [item_ids["诗歌 2"], item_ids["诗歌 1"]]},
            {"section_id": sections[1]["id"], "item_ids": []},
        ]},
        headers={"If-Match": f'"rev-{detail["revision"]}"'},
    )
    assert moved.status_code == 200, moved.text
    assert [len(s["items"]) for s in moved.json()["sections"]] == [2, 0]


def test_请求模型拒绝空名称与空排序() -> None:
    with pytest.raises(ValueError):
        RoomCreate(name="  ", service_date=SUNDAY)
    with pytest.raises(ValueError):
        RoomOrderUpdate(sections=[])
    assert RoomItemCreate(work_id="w").section_id is None
