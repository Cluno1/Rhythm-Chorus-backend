"""issue 152: templates copy into rooms; section ownership is permissioned and revisioned."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_public_device_auth import admin_token, settings
from test_rooms import SUNDAY, Device, add_works, pin_clock

from rhythm_metadata_api.infrastructure.db.models import (
    RoomSection,
    RoomTemplate,
    RoomTemplateSection,
)
from rhythm_metadata_api.public_main import (
    _public_read_allowed,
    _public_write_allowed,
    create_public_app,
)


@pytest.fixture
def public(tmp_path: Path):
    with TestClient(create_public_app(settings(tmp_path, max_active_devices=4))) as client:
        yield client


def template_body(*, enabled: bool = True) -> dict:
    return {
        "name": "主日聚会",
        "enabled": enabled,
        "sections": [
            {"name": "开场", "assignee_user_id": "alice"},
            {"name": "结束", "assignee_user_id": "bob"},
        ],
    }


def test_admin_template_is_copied_once_and_disabled_templates_cannot_be_used(public: TestClient) -> None:
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    Device(public, admin, "bob", "小红")
    auth = {"Authorization": f"Bearer {admin}"}

    denied = alice.call("POST", "/v2/admin/room-templates", template_body())
    assert denied.status_code in {401, 403}
    created = public.post("/v2/admin/room-templates", headers=auth, json=template_body())
    assert created.status_code == 201, created.text
    template = created.json()
    assert [(entry["name"], entry["assignee_user_id"], entry["position"])
            for entry in template["sections"]] == [("开场", "alice", 1), ("结束", "bob", 2)]
    assert [entry["id"] for entry in alice.call("GET", "/v2/room-templates").json()["items"]] == [template["id"]]

    room = alice.call(
        "POST", "/v2/rooms",
        {"name": "本周", "service_date": SUNDAY.isoformat(), "template_id": template["id"]},
        headers={"Idempotency-Key": "from-template"},
    )
    assert room.status_code == 201, room.text
    room_id = room.json()["id"]
    assert [(entry["name"], entry["assignee"]["user_id"])
            for entry in room.json()["sections"]] == [("开场", "alice"), ("结束", "bob")]

    changed = public.patch(
        f"/v2/admin/room-templates/{template['id']}", headers=auth,
        json={**template_body(enabled=False), "revision": 1, "sections": [{"name": "新的模板段"}]},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2
    assert alice.call("GET", "/v2/room-templates").json()["items"] == []
    assert [(entry["name"], entry["assignee"]["user_id"])
            for entry in alice.call("GET", f"/v2/rooms/{room_id}").json()["sections"]] == [
                ("开场", "alice"), ("结束", "bob"),
            ]
    unavailable = alice.call(
        "POST", "/v2/rooms",
        {"name": "下周", "service_date": SUNDAY.isoformat(), "template_id": template["id"]},
        headers={"Idempotency-Key": "disabled-template"},
    )
    assert unavailable.status_code == 404
    stale = public.patch(
        f"/v2/admin/room-templates/{template['id']}", headers=auth,
        json={**template_body(), "revision": 1},
    )
    assert stale.status_code == 412
    with Session(public.app.state.v2_container.engine) as session:
        assert session.scalar(select(func.count()).select_from(RoomTemplate)) == 1
        assert session.scalar(select(func.count()).select_from(RoomTemplateSection)) == 1


def test_section_assignee_requires_creator_and_current_revision(public: TestClient) -> None:
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    bob = Device(public, admin, "bob", "小红")
    carol = Device(public, admin, "carol", "小丽")
    created = alice.call(
        "POST", "/v2/rooms", {"name": "本周", "service_date": SUNDAY.isoformat()},
        headers={"Idempotency-Key": "plain"},
    ).json()
    path = f"/v2/rooms/{created['id']}/sections/{created['sections'][0]['id']}"
    headers = {"If-Match": '"rev-1"'}
    refused = bob.call("PATCH", path, {"name": "开场", "assignee_user_id": "carol"}, headers=headers)
    assert refused.status_code == 403
    invalid = alice.call("PATCH", path, {"assignee_user_id": "unknown"}, headers=headers)
    assert invalid.status_code == 422
    changed = alice.call("PATCH", path, {"name": "开场", "assignee_user_id": "carol"}, headers=headers)
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2
    assert changed.json()["sections"][0]["assignee"]["display_name"] == "小丽"
    assert bob.call("PATCH", path, {"assignee_user_id": None}, headers=headers).status_code == 403
    assert alice.call("PATCH", path, {"assignee_user_id": None}, headers=headers).status_code == 412
    with Session(public.app.state.v2_container.engine) as session:
        assert session.scalar(select(func.count()).select_from(RoomSection)) == 1

    cleared = public.patch(
        f"/v2/admin/rooms/{created['id']}/sections/{created['sections'][0]['id']}",
        headers={"Authorization": f"Bearer {admin}", "If-Match": '"rev-2"'},
        json={"assignee_user_id": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["sections"][0]["assignee"] is None
    assert sorted(user["user_id"] for user in carol.call("GET", "/v2/room-users").json()["items"]) == [
        "alice", "bob", "carol",
    ]


def test_template_sections_are_real_add_targets_and_routes_are_narrow(public: TestClient) -> None:
    pin_clock(public, datetime(2026, 10, 25, 4, 0, tzinfo=UTC))
    admin = admin_token(public)
    alice = Device(public, admin, "alice", "小明")
    template = public.post(
        "/v2/admin/room-templates", headers={"Authorization": f"Bearer {admin}"},
        json={"name": "两段", "sections": [{"name": "一"}, {"name": "二"}]},
    ).json()
    room = alice.call(
        "POST", "/v2/rooms",
        {"name": "本周", "service_date": SUNDAY.isoformat(), "template_id": template["id"]},
        headers={"Idempotency-Key": "template"},
    ).json()
    work_id = add_works(public, 1)[0]
    added = alice.call(
        "POST", f"/v2/rooms/{room['id']}/items",
        {"work_id": work_id, "section_id": room["sections"][0]["id"]},
        headers={"Idempotency-Key": "add"},
    )
    assert added.status_code == 201, added.text
    assert [len(section["items"]) for section in added.json()["sections"]] == [1, 0]
    assert _public_read_allowed("GET", "/v2/room-templates")
    assert _public_read_allowed("GET", "/v2/room-users")
    assert _public_write_allowed("PATCH", f"/v2/rooms/{room['id']}/sections/{room['sections'][0]['id']}")
    assert not _public_write_allowed("PATCH", f"/v2/rooms/{room['id']}")
