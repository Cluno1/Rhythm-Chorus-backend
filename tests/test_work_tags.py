from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.main import create_app
from rhythm_metadata_api.public_main import _public_read_allowed

AUTH = {"Authorization": "Bearer test-private-catalog-token", "X-Device-ID": "tag-test"}


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        bootstrap_token="test-private-catalog-token",
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
    )
    with TestClient(create_app(settings)) as app:
        yield app


def create_work(client: TestClient, key: str, title: str, **fields: object) -> dict:
    response = client.post(
        "/v2/works",
        headers={**AUTH, "Idempotency-Key": key},
        json={"canonical_title": title, **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_tag_write_search_and_exact_paginated_works(client: TestClient) -> None:
    first = create_work(
        client, "tag-first", "A song", tags=[" 多声部 ", "安静", "DUET", "duet"],
        lyrics="第一句歌词",
    )
    second = create_work(client, "tag-second", "B song", tags=["多声部"])
    create_work(client, "tag-near", "C song", tags=["男女多声部"])
    create_work(client, "tag-archived", "D song", tags=["多声部"], status="archived")

    assert first["tags"] == ["多声部", "安静", "DUET"]
    bundle = client.get(f"/v2/works/{first['id']}/bundle", headers=AUTH)
    assert bundle.status_code == 200
    assert bundle.json()["work"]["tags"] == first["tags"]

    result = client.get(
        "/v2/library/search", headers=AUTH, params={"q": "声部", "type": "tags"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["items"] == []
    assert [(item["label"], item["work_count"]) for item in result.json()["tag_items"]] == [
        ("多声部", 2), ("男女多声部", 1)
    ]

    first_page = client.get(
        "/v2/library/tag-works", headers=AUTH, params={"tag": "多声部", "limit": 1}
    )
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["tag"]["work_count"] == 2
    assert [item["work_id"] for item in first_page.json()["items"]] == [first["id"]]
    assert first_page.json()["items"][0]["match_field"] == "tag"
    assert first_page.json()["items"][0]["has_lyrics"] is True
    assert first_page.json()["items"][0]["snippet"] is None
    second_page = client.get(
        "/v2/library/tag-works", headers=AUTH,
        params={"tag": "多声部", "limit": 1, "cursor": first_page.json()["next_cursor"]},
    )
    assert second_page.status_code == 200, second_page.text
    assert [item["work_id"] for item in second_page.json()["items"]] == [second["id"]]
    assert second_page.json()["next_cursor"] is None
    assert second_page.json()["items"][0]["has_lyrics"] is False

    assert client.get(
        "/v2/library/tag-works", headers=AUTH, params={"tag": "不存在"}
    ).json()["items"] == []
    assert client.get(
        "/v2/library/tag-works", headers=AUTH,
        params={"tag": "男女多声部", "cursor": first_page.json()["next_cursor"]},
    ).status_code == 422


def test_tag_patch_revisions_and_validation(client: TestClient) -> None:
    work = create_work(client, "tag-patch", "Tagged", tags=["温柔"])
    patched = client.patch(
        f"/v2/works/{work['id']}", headers={**AUTH, "If-Match": '"rev-1"'},
        json={"canonical_title": "Tagged again"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["tags"] == ["温柔"]
    stale = client.patch(
        f"/v2/works/{work['id']}", headers={**AUTH, "If-Match": '"rev-1"'},
        json={"tags": []},
    )
    assert stale.status_code == 412
    cleared = client.patch(
        f"/v2/works/{work['id']}", headers={**AUTH, "If-Match": '"rev-2"'},
        json={"tags": []},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["tags"] == []
    for tags in (None, ["  "], ["x" * 41], list(range(51))):
        invalid = client.patch(
            f"/v2/works/{work['id']}", headers={**AUTH, "If-Match": '"rev-3"'},
            json={"tags": tags},
        )
        assert invalid.status_code == 422, invalid.text


def test_tag_list_is_public_device_read_only() -> None:
    assert _public_read_allowed("GET", "/v2/library/tag-works")
    assert not _public_read_allowed("POST", "/v2/library/tag-works")


def test_casefold_expansion_keeps_long_tag_keys_openable(client: TestClient) -> None:
    label = "ß" * 40
    work = create_work(client, "tag-casefold", "Long key", tags=[label])
    response = client.get(
        "/v2/library/tag-works", headers=AUTH, params={"tag": "ss" * 40}
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["work_id"] == work["id"]


def test_library_tag_list_counts_active_works_and_orders_by_usage(client: TestClient) -> None:
    """issue 121：标签选择器的全库标签列表。"""
    assert client.get("/v2/library/tags", headers=AUTH).json() == {"items": []}
    create_work(client, "list-a", "A", tags=["赞美", "多声部"])
    create_work(client, "list-b", "B", tags=["赞美", "Duet"])
    create_work(client, "list-c", "C", tags=["赞美", "duet "])
    create_work(client, "list-archived", "D", tags=["只在归档里"], status="archived")

    response = client.get("/v2/library/tags", headers=AUTH)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    # 按使用作品数降序；大小写不同的同一标签合并计数；归档作品不计入。
    assert (items[0]["label"], items[0]["work_count"]) == ("赞美", 3)
    assert [item["tag_key"] for item in items] == ["赞美", "duet", "多声部"]
    assert {item["tag_key"]: item["work_count"] for item in items} == {
        "赞美": 3,
        "duet": 2,
        "多声部": 1,
    }
    limited = client.get("/v2/library/tags", headers=AUTH, params={"limit": 1}).json()["items"]
    assert [item["tag_key"] for item in limited] == ["赞美"]
    assert client.get("/v2/library/tags", headers=AUTH, params={"limit": 0}).status_code == 422


def test_library_tag_list_is_public_device_read_only() -> None:
    assert _public_read_allowed("GET", "/v2/library/tags")
    assert not _public_read_allowed("POST", "/v2/library/tags")

