"""issue 125：已登记设备直接编辑作品某一语言的纯文本歌词。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_public_device_auth import admin_token, settings
from test_rooms import Device

from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.models import ChangeEvent, Work
from rhythm_metadata_api.main import create_app
from rhythm_metadata_api.public_main import _public_write_allowed, create_public_app

AUTH = {"Authorization": "Bearer test-private-catalog-token", "X-Device-ID": "lyrics-test"}


@pytest.fixture
def client(tmp_path: Path):
    private = Settings(
        bootstrap_token="test-private-catalog-token",
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
    )
    with TestClient(create_app(private)) as app:
        yield app


def create_work(client: TestClient, key: str, **fields: object) -> dict:
    response = client.post(
        "/v2/works",
        headers={**AUTH, "Idempotency-Key": key},
        json={"canonical_title": "神的国度", "language": "zh-Hans", **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


def put_lyrics(client: TestClient, work: dict, language: str, lyrics: str, revision: int | None = None):
    return client.put(
        f"/v2/works/{work['id']}/lyrics/{language}",
        headers={**AUTH, "If-Match": f'"rev-{revision if revision is not None else work["revision"]}"'},
        json={"lyrics": lyrics},
    )


def test_改默认语言的歌词只替换这一版且保留其他语言(client: TestClient) -> None:
    work = create_work(
        client, "default", lyrics="旧的中文", lyrics_language="zh-Hans",
        lyrics_translations=[{"language": "en", "lyrics": "Old English"}],
    )
    response = put_lyrics(client, work, "zh-hans", "  新的中文\r\n第二行  ")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lyrics"] == "新的中文\n第二行"
    assert body["lyrics_language"] == "zh-Hans"
    assert body["lyrics_translations"] == [{"language": "en", "lyrics": "Old English"}]
    assert body["revision"] == work["revision"] + 1
    assert response.headers["ETag"] == f'"rev-{work["revision"] + 1}"'


def test_改其他语言时替换已有译文或新增一种语言(client: TestClient) -> None:
    work = create_work(
        client, "translations", lyrics="中文", lyrics_language="zh-Hans",
        lyrics_translations=[{"language": "en", "lyrics": "Old English"}],
    )
    replaced = put_lyrics(client, work, "en", "New English").json()
    assert replaced["lyrics"] == "中文"
    assert replaced["lyrics_translations"] == [{"language": "en", "lyrics": "New English"}]
    added = put_lyrics(client, replaced, "zh-Hant", "繁體").json()
    assert added["lyrics_translations"] == [
        {"language": "en", "lyrics": "New English"},
        {"language": "zh-Hant", "lyrics": "繁體"},
    ]


def test_作品还没有歌词时以这次的语言作为默认歌词(client: TestClient) -> None:
    work = create_work(client, "empty")
    assert work["lyrics"] is None
    body = put_lyrics(client, work, "en", "First lyrics").json()
    assert (body["lyrics"], body["lyrics_language"], body["lyrics_translations"]) == ("First lyrics", "en", [])


def test_旧版本号被拒绝且不覆盖别人刚保存的歌词(client: TestClient) -> None:
    work = create_work(client, "stale", lyrics="原文", lyrics_language="zh-Hans")
    first = put_lyrics(client, work, "zh-Hans", "甲的修改")
    assert first.status_code == 200
    stale = put_lyrics(client, work, "zh-Hans", "乙基于旧版本的修改")
    assert stale.status_code == 412, stale.text
    assert stale.json()["current_etag"] == f'"rev-{work["revision"] + 1}"'
    current = client.get(f"/v2/works/{work['id']}", headers=AUTH).json()
    assert current["lyrics"] == "甲的修改"


def test_内容没变时不递增版本也不写同步事件(client: TestClient) -> None:
    work = create_work(client, "same", lyrics="一样", lyrics_language="zh-Hans")
    edited = put_lyrics(client, work, "zh-Hans", "一样\n")
    assert edited.status_code == 200
    assert edited.json()["revision"] == work["revision"]
    engine = client.app.state.v2_container.engine
    with Session(engine) as session:
        before = len(list(session.scalars(select(ChangeEvent).where(ChangeEvent.entity_id == work["id"]))))
    changed = put_lyrics(client, work, "zh-Hans", "不一样")
    assert changed.json()["revision"] == work["revision"] + 1
    with Session(engine) as session:
        events = list(session.scalars(select(ChangeEvent).where(ChangeEvent.entity_id == work["id"])))
        # 恰好多一条事件：客户端据此刷新缓存，重复保存同一内容不会刷屏。
        assert len(events) == before + 1
        assert events[-1].payload_json == {"fields": ["lyrics"], "language": "zh-Hans"}


def test_空歌词_非法语言_归档作品都被拒绝(client: TestClient) -> None:
    work = create_work(client, "invalid", lyrics="原文", lyrics_language="zh-Hans")
    assert put_lyrics(client, work, "zh-Hans", "   ").status_code == 422
    assert put_lyrics(client, work, "not a tag!", "x").status_code == 422
    extra = client.put(
        f"/v2/works/{work['id']}/lyrics/zh-Hans",
        headers={**AUTH, "If-Match": f'"rev-{work["revision"]}"'},
        json={"lyrics": "x", "canonical_title": "夹带改标题"},
    )
    assert extra.status_code == 422
    archived = create_work(client, "archived", lyrics="原文", status="archived")
    assert put_lyrics(client, archived, "zh-Hans", "新").status_code == 409


def test_公网只放行作品歌词窄接口且登记设备可以直接保存(tmp_path: Path) -> None:
    assert _public_write_allowed("PUT", "/v2/works/w1/lyrics/zh-Hans")
    assert not _public_write_allowed("PATCH", "/v2/works/w1")
    assert not _public_write_allowed("PUT", "/v2/works/w1/lyrics")
    with TestClient(create_public_app(settings(tmp_path))) as public:
        device = Device(public, admin_token(public), "alice", "小明")
        with Session(public.app.state.v2_container.engine) as session:
            work = Work(canonical_title="神的国度", status="active", lyrics="原文", lyrics_language="zh-Hans")
            session.add(work)
            session.commit()
            work_id, revision = work.id, work.revision
        saved = device.call(
            "PUT", f"/v2/works/{work_id}/lyrics/zh-Hans", {"lyrics": "手机上改的"},
            headers={"If-Match": f'"rev-{revision}"'},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["lyrics"] == "手机上改的"
        # 通用的作品修改接口仍不对设备开放。
        general = device.call(
            "PATCH", f"/v2/works/{work_id}", {"lyrics": "x"}, headers={"If-Match": '"rev-2"'}
        )
        assert general.status_code == 404
