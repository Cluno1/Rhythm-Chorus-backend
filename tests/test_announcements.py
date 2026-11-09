"""issue 154: administrators publish announcements; devices only read what is published."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_public_device_auth import admin_token, settings
from test_rooms import Device

from rhythm_metadata_api.domain.v2.announcements import (
    AnnouncementWrite,
    body_excerpt,
    normalize_body,
)
from rhythm_metadata_api.domain.v2.errors import V2Unavailable
from rhythm_metadata_api.infrastructure.db.models import Announcement, AnnouncementImage
from rhythm_metadata_api.public_main import create_public_app

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def doc(*blocks: dict) -> dict:
    return {"type": "doc", "content": list(blocks)}


def paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def body_write(title: str = "新版本", body: dict | None = None, **extra) -> dict:
    return {"title": title, "body": body or doc(paragraph("正文内容")), **extra}


class FakeStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str]] = []

    def __call__(self, key: str, body: bytes, media_type: str) -> None:
        self.calls.append((key, body, media_type))


@pytest.fixture
def public(tmp_path: Path):
    configured = settings(tmp_path, max_active_devices=4).model_copy(update={
        "music_image_cos_bucket": "test-bucket-1250000000",
        "cos_secret_id": "test-id",
        "cos_secret_key": "test-key",
    })
    with TestClient(create_public_app(configured), follow_redirects=False) as client:
        yield client


@pytest.fixture
def storage(public: TestClient) -> FakeStorage:
    fake = FakeStorage()
    public.app.state.v2_container.announcements._put_object = fake
    return fake


def admin(public: TestClient) -> dict:
    return {"Authorization": f"Bearer {admin_token(public)}"}


def create(public: TestClient, auth: dict, **extra) -> dict:
    response = public.post("/v2/admin/announcements", json=body_write(**extra), headers=auth)
    assert response.status_code == 201, response.text
    return response.json()


def publish(public: TestClient, auth: dict, item: dict) -> dict:
    response = public.post(
        f"/v2/admin/announcements/{item['id']}/publish", json={"revision": item["revision"]}, headers=auth,
    )
    assert response.status_code == 200, response.text
    return response.json()


def row_count(public: TestClient, model) -> int:
    with Session(public.app.state.v2_container.engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def upload(public: TestClient, auth: dict, content: bytes = PNG, media_type: str = "image/png"):
    return public.post(
        "/v2/admin/announcement-images", content=content, headers={**auth, "Content-Type": media_type},
    )


def test_草稿对设备不可见_发布后可读(public: TestClient) -> None:
    auth = admin(public)
    device = Device(public, auth["Authorization"].removeprefix("Bearer "), "alice", "小明")
    draft = create(public, auth, summary="摘要", importance="important")
    assert device.call("GET", "/v2/announcements").json()["items"] == []
    assert device.call("GET", f"/v2/announcements/{draft['id']}").status_code == 404

    publish(public, auth, draft)

    listed = device.call("GET", "/v2/announcements").json()["items"]
    assert [item["id"] for item in listed] == [draft["id"]]
    assert listed[0]["importance"] == "important" and "body" not in listed[0]
    detail = device.call("GET", f"/v2/announcements/{draft['id']}")
    assert detail.status_code == 200
    assert detail.json()["body"] == doc(paragraph("正文内容"))


def test_未登录和普通设备不能读取设备接口以外的管理接口(public: TestClient) -> None:
    auth = admin(public)
    device = Device(public, auth["Authorization"].removeprefix("Bearer "), "alice", "小明")
    assert public.get("/v2/announcements").status_code == 401
    assert public.post("/v2/admin/announcements", json=body_write()).status_code == 401
    assert device.call("POST", "/v2/admin/announcements", body_write()).status_code in {401, 403}
    assert device.call("GET", "/v2/admin/announcements").status_code in {401, 403, 404}
    assert row_count(public, Announcement) == 0


def test_创建恰好写入一条(public: TestClient) -> None:
    create(public, admin(public))
    assert row_count(public, Announcement) == 1


def test_重复发布被版本号拦下且发布时间不变(public: TestClient) -> None:
    auth = admin(public)
    draft = create(public, auth)
    published = publish(public, auth, draft)
    stale = public.post(
        f"/v2/admin/announcements/{draft['id']}/publish", json={"revision": draft["revision"]}, headers=auth,
    )
    assert stale.status_code == 412
    current = public.get(f"/v2/admin/announcements/{draft['id']}", headers=auth).json()
    assert current["revision"] == published["revision"]
    assert current["published_at"] == published["published_at"]


def test_已发布公告不能再发布(public: TestClient) -> None:
    auth = admin(public)
    published = publish(public, auth, create(public, auth))
    again = public.post(
        f"/v2/admin/announcements/{published['id']}/publish", json={"revision": published["revision"]}, headers=auth,
    )
    assert again.status_code == 409


def test_空正文不能发布_已发布公告也不能改成空正文(public: TestClient) -> None:
    auth = admin(public)
    empty = create(public, auth, body=doc({"type": "paragraph"}))
    response = public.post(
        f"/v2/admin/announcements/{empty['id']}/publish", json={"revision": empty["revision"]}, headers=auth,
    )
    assert response.status_code == 422
    published = publish(public, auth, create(public, auth))
    patched = public.patch(
        f"/v2/admin/announcements/{published['id']}",
        json={**body_write(body=doc({"type": "paragraph"})), "revision": published["revision"]}, headers=auth,
    )
    assert patched.status_code == 422


def test_编辑已发布公告保留发布时间并递增版本(public: TestClient) -> None:
    auth = admin(public)
    published = publish(public, auth, create(public, auth))
    patched = public.patch(
        f"/v2/admin/announcements/{published['id']}",
        json={**body_write(title="改过的标题"), "revision": published["revision"]}, headers=auth,
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "改过的标题"
    assert patched.json()["published_at"] == published["published_at"]
    assert patched.json()["revision"] == published["revision"] + 1


def test_过期版本号的编辑被拒绝且内容不变(public: TestClient) -> None:
    auth = admin(public)
    draft = create(public, auth)
    public.patch(
        f"/v2/admin/announcements/{draft['id']}", json={**body_write(title="第一次"), "revision": 1}, headers=auth,
    )
    stale = public.patch(
        f"/v2/admin/announcements/{draft['id']}", json={**body_write(title="第二次"), "revision": 1}, headers=auth,
    )
    assert stale.status_code == 412
    assert public.get(f"/v2/admin/announcements/{draft['id']}", headers=auth).json()["title"] == "第一次"


def test_下架后设备不可见_重新发布保留首次发布时间(public: TestClient) -> None:
    auth = admin(public)
    device = Device(public, auth["Authorization"].removeprefix("Bearer "), "alice", "小明")
    published = publish(public, auth, create(public, auth))
    archived = public.post(
        f"/v2/admin/announcements/{published['id']}/archive", json={"revision": published["revision"]}, headers=auth,
    ).json()
    assert archived["status"] == "archived"
    assert device.call("GET", f"/v2/announcements/{published['id']}").status_code == 404
    assert device.call("GET", "/v2/announcements").json()["items"] == []
    again = publish(public, auth, archived)
    assert again["published_at"] == published["published_at"]


def test_已发布公告不能直接删除_草稿可以(public: TestClient) -> None:
    auth = admin(public)
    published = publish(public, auth, create(public, auth))
    refused = public.delete(
        f"/v2/admin/announcements/{published['id']}", params={"revision": published["revision"]}, headers=auth,
    )
    assert refused.status_code == 409
    assert row_count(public, Announcement) == 1
    draft = create(public, auth)
    deleted = public.delete(f"/v2/admin/announcements/{draft['id']}", params={"revision": 1}, headers=auth)
    assert deleted.status_code == 204
    assert row_count(public, Announcement) == 1


def test_设备列表分页且不重不漏(public: TestClient) -> None:
    auth = admin(public)
    device = Device(public, auth["Authorization"].removeprefix("Bearer "), "alice", "小明")
    service = public.app.state.v2_container.announcements
    base = datetime(2026, 11, 1, tzinfo=UTC)
    created = []
    for minute in (0, 1, 1, 2, 3):
        service._now = lambda minute=minute: base + timedelta(minutes=minute)
        created.append(publish(public, auth, create(public, auth))["id"])
    seen: list[str] = []
    cursor = ""
    for _ in range(5):
        page = device.call("GET", "/v2/announcements", query="limit=2" + (f"&cursor={cursor}" if cursor else "")).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(created) and len(seen) == 5
    assert seen[0] == created[-1]


def test_非法游标返回422(public: TestClient) -> None:
    auth = admin(public)
    device = Device(public, auth["Authorization"].removeprefix("Bearer "), "alice", "小明")
    assert device.call("GET", "/v2/announcements", query="cursor=not-a-cursor").status_code == 422


def test_列表未变化时返回304_发布后ETag变化(public: TestClient) -> None:
    auth = admin(public)
    device = Device(public, auth["Authorization"].removeprefix("Bearer "), "alice", "小明")
    first = device.call("GET", "/v2/announcements")
    tag = first.headers["ETag"]
    assert device.call("GET", "/v2/announcements", headers={"If-None-Match": tag}).status_code == 304
    publish(public, auth, create(public, auth))
    changed = device.call("GET", "/v2/announcements", headers={"If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["ETag"] != tag


@pytest.mark.parametrize("body", [
    {"type": "paragraph"},
    doc({"type": "script", "content": []}),
    doc({"type": "heading", "attrs": {"level": 4}, "content": [{"type": "text", "text": "x"}]}),
    doc({"type": "heading", "attrs": {"level": True}, "content": [{"type": "text", "text": "x"}]}),
    doc({"type": "codeBlock", "content": [{"type": "text", "text": "x"}]}),
    doc({"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [{"type": "code"}]}]}),
    doc({"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [
        {"type": "link", "attrs": {"href": "javascript:alert(1)"}},
    ]}]}),
    doc({"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": [
        {"type": "link", "attrs": {"href": "http://example.com"}},
    ]}]}),
    doc({"type": "image", "attrs": {"src": "https://evil.example/x.png"}}),
    doc({"type": "image", "attrs": {"src": 123}}),
    doc({"type": "image", "attrs": {"src": "/v2/announcements/images/../../secret.png"}}),
    doc({"type": "bulletList", "content": [paragraph("list children must be items")]}),
    doc({"type": "blockquote", "content": [{"type": "image", "attrs": {"src": "https://evil.example/x.png"}}]}),
])
def test_不在白名单内的内容被拒绝(body: dict) -> None:
    with pytest.raises(ValidationError):
        AnnouncementWrite(title="标题", body=body)


def test_过大的正文被拒绝() -> None:
    with pytest.raises(ValidationError):
        AnnouncementWrite(title="标题", body=doc(paragraph("字" * 100_000)))


def test_过深的嵌套被拒绝() -> None:
    node: dict = paragraph("x")
    for _ in range(20):
        node = {"type": "blockquote", "content": [node]}
    with pytest.raises(ValueError, match="too deeply"):
        normalize_body(doc(node))


IMAGE_NODE = {"type": "image", "attrs": {"src": f"/v2/announcements/images/{'a' * 64}.jpg"}}
HEADING_NODE = {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "x"}]}


@pytest.mark.parametrize("child", [IMAGE_NODE, HEADING_NODE, {"type": "horizontalRule"}])
@pytest.mark.parametrize("wrap", [
    lambda child: {"type": "blockquote", "content": [child]},
    lambda child: {"type": "bulletList", "content": [{"type": "listItem", "content": [paragraph("x"), child]}]},
])
def test_列表和引用内可以放图片标题和分隔线(wrap, child: dict) -> None:
    body = doc(wrap(child))
    assert normalize_body(body) == body


@pytest.mark.parametrize("level", [1, 2, 3])
def test_一到三级标题都保留层级(level: int) -> None:
    body = doc({"type": "heading", "attrs": {"level": level}, "content": [{"type": "text", "text": "x"}]})
    assert normalize_body(body) == body


def test_四层嵌套列表可以保存() -> None:
    node: dict = {"type": "bulletList", "content": [{"type": "listItem", "content": [paragraph("x")]}]}
    for _ in range(3):
        node = {"type": "bulletList", "content": [{"type": "listItem", "content": [paragraph("x"), node]}]}
    assert normalize_body(doc(node)) == doc(node)


def test_超长图片只丢弃超限的尺寸不拒绝保存() -> None:
    src = IMAGE_NODE["attrs"]["src"]
    normalized = normalize_body(doc({"type": "image", "attrs": {"src": src, "width": 1080, "height": 20_001}}))
    assert normalized == doc({"type": "image", "attrs": {"src": src, "width": 1080}})


@pytest.mark.parametrize("width", [0, -1, 1.5, True, "640"])
def test_无效的图片宽度仍被拒绝(width: object) -> None:
    src = IMAGE_NODE["attrs"]["src"]
    with pytest.raises(ValueError, match="image width is invalid"):
        normalize_body(doc({"type": "image", "attrs": {"src": src, "width": width}}))


def test_已知节点上的样式属性被丢弃() -> None:
    normalized = normalize_body(doc(
        {"type": "paragraph", "attrs": {"textAlign": "center", "style": "color:red"}, "content": [{
            "type": "text", "text": "x", "marks": [
                {"type": "link", "attrs": {"href": "https://example.com", "target": "_blank", "class": "evil"}},
            ],
        }]},
    ))
    assert normalized == doc({"type": "paragraph", "content": [{
        "type": "text", "text": "x", "marks": [{"type": "link", "attrs": {"href": "https://example.com"}}],
    }]})


def test_摘要为空时用正文开头() -> None:
    assert body_excerpt(doc(paragraph("  第一段 "), paragraph("第二段"))) == "第一段 第二段"


def test_图片上传相同内容只写一次存储(public: TestClient, storage: FakeStorage) -> None:
    auth = admin(public)
    first = upload(public, auth)
    second = upload(public, auth)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert len(storage.calls) == 1
    assert row_count(public, AnnouncementImage) == 1
    assert storage.calls[0][0].startswith("announcement-images/")


def test_存储失败时不记录图片(public: TestClient) -> None:
    def fail(_key: str, _body: bytes, _media_type: str) -> None:
        raise V2Unavailable("announcement image storage is unavailable")

    public.app.state.v2_container.announcements._put_object = fail
    assert upload(public, admin(public)).status_code == 503
    assert row_count(public, AnnouncementImage) == 0


def test_图片按实际字节识别类型不看声明(public: TestClient, storage: FakeStorage) -> None:
    response = upload(public, admin(public), PNG, "image/jpeg")
    assert response.status_code == 201
    assert response.json()["media_type"] == "image/png"
    assert response.json()["path"].endswith(".png")
    assert len(storage.calls) == 1


@pytest.mark.parametrize(("content", "media_type", "status"), [
    (b"<svg></svg>", "image/svg+xml", 422),
    (b"", "image/png", 422),
    (b"\x89PNG\r\n\x1a\n" + b"0" * 10_000_001, "image/png", 413),
])
def test_不合格的图片被拒绝且不写存储(
    public: TestClient, storage: FakeStorage, content: bytes, media_type: str, status: int,
) -> None:
    assert upload(public, admin(public), content, media_type).status_code == status
    assert storage.calls == []


def test_设备不能上传图片(public: TestClient, storage: FakeStorage) -> None:
    assert public.post(
        "/v2/admin/announcement-images", content=PNG, headers={"Content-Type": "image/png"},
    ).status_code == 401
    assert storage.calls == []


def test_未配置存储时上传明确失败(tmp_path: Path) -> None:
    with TestClient(create_public_app(settings(tmp_path / "other", max_active_devices=4))) as client:
        assert upload(client, admin(client)).status_code == 503


def test_正文只能引用已上传的图片_封面取第一张(public: TestClient, storage: FakeStorage) -> None:
    auth = admin(public)
    device = Device(public, auth["Authorization"].removeprefix("Bearer "), "alice", "小明")
    missing = {"type": "image", "attrs": {"src": "/v2/announcements/images/" + "a" * 64 + ".png"}}
    refused = public.post("/v2/admin/announcements", json=body_write(body=doc(missing)), headers=auth)
    assert refused.status_code == 422

    path = upload(public, auth).json()["path"]
    image = {"type": "image", "attrs": {"src": path, "alt": "封面", "width": 640, "height": 480}}
    publish(public, auth, create(public, auth, body=doc(paragraph("看图"), image)))
    item = device.call("GET", "/v2/announcements").json()["items"][0]
    assert item["cover_image"] == path


def test_图片地址免设备令牌重定向到签名地址_未知图片404(public: TestClient, storage: FakeStorage) -> None:
    path = upload(public, admin(public)).json()["path"]
    response = public.get(path)
    assert response.status_code == 307
    assert "test-bucket-1250000000" in response.headers["Location"]
    assert public.get("/v2/announcements/images/" + "a" * 64 + ".png").status_code == 404
