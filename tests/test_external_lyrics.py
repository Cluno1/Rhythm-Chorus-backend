"""issue 127: external Chinese lyrics search through the internal Meting provider."""

from __future__ import annotations

from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from rhythm_metadata_api.application.external_lyrics import clean_lrc
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.main import create_app
from rhythm_metadata_api.public_main import _PUBLIC_READ_ROUTES

TOKEN = "test-private-catalog-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Device-ID": "android-test-device"}


def hit(
    source: str,
    external_id: str,
    title: str,
    artist: str,
    *,
    snippet: str | None = None,
    duration_ms: int = 253783,
) -> dict:
    return {
        "source": source,
        "id": external_id,
        "title": title,
        "artists": [artist],
        "album": "专辑",
        "duration_ms": duration_ms,
        "snippet": snippet,
    }


class FakeProvider:
    """Answers like lyrics_provider; records every call so tests can assert exact call counts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.failing: set[str] = set()
        self.search_items = {
            ("netease", "title"): [
                hit("netease", "2067068109", "旅人", "罗云熙"),
                hit("netease", "542718890", "旅人", "陈致逸"),
            ],
            ("netease", "lyrics"): [
                hit(
                    "netease",
                    "3339809937",
                    "不得不爱",
                    "Lambert凌杰",
                    snippet="你掌心的温度 我就会觉得幸福",
                )
            ],
            ("kugou", "title"): [
                hit("kugou", "8ae73bc9d344e00f3a083aa3abdeb3c2", "旅人", "罗云熙"),
                hit("kugou", "aaaa0000bbbb1111cccc2222dddd3333", "旅人 (DJ版)", "DJ先森"),
            ],
            ("tencent", "title"): [
                hit("tencent", "004BzmAS3gqK1T", "旅人", "王铮亮"),
                hit("tencent", "002DBLd50RMYKG", "旅人", "罗云熙"),
            ],
        }
        self.lyric = {
            "lyric": "[ti:旅人]\n[00:00.00] 作词 : 刘凤瑶\n[00:01.00]旅人 - 罗云熙\n[00:13.73]In your eyes\n"
            "[00:17.09]心正在通往 探索的征帆\n",
            "tlyric": "",
        }

        self.lyrics_by_id: dict[str, dict] = {}

    def __call__(self, url: str, timeout: float) -> dict:
        parts = urlsplit(url)
        params = {key: values[0] for key, values in parse_qs(parts.query).items()}
        self.calls.append((parts.path, params))
        if params["server"] in self.failing:
            raise URLError("provider down")
        if parts.path == "/search":
            return {"items": self.search_items.get((params["server"], params["type"]), [])}
        return self.lyrics_by_id.get(params["id"], self.lyric)


@pytest.fixture
def env(tmp_path: Path):
    settings = Settings(
        bootstrap_token=TOKEN,
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
        external_lyrics_provider_url="http://provider.test",
    )
    provider = FakeProvider()
    with TestClient(create_app(settings)) as client:
        client.app.state.v2_container.external_lyrics._fetch = provider
        yield client, provider


def search(client: TestClient, q: str, **params: object):
    return client.get("/v2/library/external-lyrics/search", headers=AUTH, params={"q": q, **params})


def test_标题命中排在歌词命中前面且跨平台同歌同歌手只保留一条(env):
    client, _ = env
    body = search(client, "旅人").json()
    assert body["type"] == "external_lyrics"
    assert [
        (item["source"], item["artists"][0], item["match_field"]) for item in body["items"]
    ] == [
        ("netease", "罗云熙", "title"),
        ("netease", "陈致逸", "title"),
        ("tencent", "王铮亮", "title"),
        ("netease", "Lambert凌杰", "lyrics"),
    ]
    assert body["items"][-1]["snippet"] == "你掌心的温度 我就会觉得幸福"
    assert body["items"][0]["snippet"] is None
    assert body["unavailable_sources"] == []


def test_相同搜索在缓存期内每个来源恰好请求一次(env):
    client, provider = env
    search(client, "旅人")
    search(client, "旅人")
    searches = [params for path, params in provider.calls if path == "/search"]
    assert sorted((p["server"], p["type"]) for p in searches) == [
        ("netease", "lyrics"),
        ("netease", "title"),
        ("tencent", "title"),
    ]


def test_单个平台失败时其余平台照常返回(env):
    client, provider = env
    provider.failing = {"tencent"}
    response = search(client, "旅人")
    assert response.status_code == 200
    assert response.json()["unavailable_sources"] == ["tencent"]
    assert {item["source"] for item in response.json()["items"]} == {"netease"}


def test_失败的来源不缓存恢复后立即可用(env):
    client, provider = env
    provider.failing = {"tencent"}
    search(client, "旅人")
    provider.failing = set()
    assert "tencent" in {item["source"] for item in search(client, "旅人").json()["items"]}


def test_全部平台失败返回503(env):
    client, provider = env
    provider.failing = {"netease", "tencent"}
    response = search(client, "旅人")
    assert response.status_code == 503
    assert response.json()["unavailable_sources"] == ["netease", "tencent"]


def test_空白搜索词被拒绝(env):
    client, _ = env
    assert search(client, "   ").status_code == 422


def test_未配置歌词服务时返回503(tmp_path: Path):
    settings = Settings(
        bootstrap_token=TOKEN,
        database_path=":memory:",
        v2_database_path=str(tmp_path / "c.sqlite3"),
        local_object_root=str(tmp_path / "o"),
    )
    with TestClient(create_app(settings)) as client:
        response = search(client, "旅人")
        assert response.status_code == 503
        # Must refuse before any request, not fail on a request to an empty URL.
        assert "not configured" in response.json()["detail"]


def test_单首歌词去掉标签署名与开头歌名行(env):
    client, provider = env
    response = client.get(
        "/v2/library/external-lyrics/netease/2067068109", headers=AUTH, params={"title": "旅人"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lyrics"] == "In your eyes\n心正在通往 探索的征帆"
    assert body["synced_lrc"].splitlines()[0] == "[00:13.73]In your eyes"
    assert body["translation"] is None
    client.get("/v2/library/external-lyrics/netease/2067068109", headers=AUTH)
    assert sum(1 for path, _ in provider.calls if path == "/lyric") == 1


def test_平台上没有歌词时返回404(env):
    client, provider = env
    provider.lyric = {"lyric": "[00:00.00] 作词 : 某人\n", "tlyric": ""}
    assert (
        client.get("/v2/library/external-lyrics/tencent/004BzmAS3gqK1T", headers=AUTH).status_code
        == 404
    )


def test_不支持的来源与非法ID被拒绝(env):
    client, provider = env
    assert client.get("/v2/library/external-lyrics/kuwo/123", headers=AUTH).status_code == 404
    assert client.get("/v2/library/external-lyrics/netease/a.b", headers=AUTH).status_code == 422
    assert provider.calls == []


def test_歌词清洗按时间排序并展开同一行的多个时间戳():
    synced, plain = clean_lrc("[00:20.00]第二句\n[00:10.00][00:30.00]重复句\n[by:x]\n词：王铮亮\n")
    assert plain == "重复句\n第二句\n重复句"
    assert synced.splitlines()[0] == "[00:10.00]重复句"


def test_公网网关放行两个只读接口():
    def allowed(path: str) -> bool:
        return any(
            method == "GET" and pattern.fullmatch(path) for method, pattern in _PUBLIC_READ_ROUTES
        )

    assert allowed("/v2/library/external-lyrics/search")
    assert allowed("/v2/library/external-lyrics/tencent/004BzmAS3gqK1T")
    assert not allowed("/v2/library/external-lyrics/kuwo/1")
    assert not allowed("/v2/library/external-lyrics/netease/../x")


def match(client: TestClient, **params: object):
    return client.get("/v2/library/external-lyrics/match", headers=AUTH, params=params)


def test_匹配接口最像的候选排第一且直接带歌词(env):
    client, _ = env
    response = match(client, title="旅人", artist="罗云熙", duration=254)
    assert response.status_code == 200, response.text
    first = response.json()["items"][0]
    assert (first["title"], first["artists"]) == ("旅人", ["罗云熙"])
    assert first["lyrics"] == "In your eyes\n心正在通往 探索的征帆"
    assert first["synced_lrc"].startswith("[00:13.73]")
    assert "match_field" not in first and "snippet" not in first


def test_匹配接口跳过平台上没有歌词的候选(env):
    client, provider = env
    provider.lyrics_by_id["2067068109"] = {"lyric": "[00:00.00] 作词 : 刘凤瑶\n", "tlyric": ""}
    ids = [
        item["external_id"] for item in match(client, title="旅人", artist="罗云熙").json()["items"]
    ]
    assert "2067068109" not in ids
    assert ids, "other candidates still have lyrics"


def test_匹配接口包含酷狗且单个平台失败仍返回(env):
    client, provider = env
    provider.failing = {"tencent"}
    body = match(client, title="旅人", artist="罗云熙", limit=10).json()
    assert body["unavailable_sources"] == ["tencent"]
    assert {item["source"] for item in body["items"]} == {"netease", "kugou"}
    assert ("kugou", "title") in {
        (p["server"], p["type"]) for path, p in provider.calls if path == "/search"
    }


def test_匹配接口每首候选的歌词恰好只取一次且受limit限制(env):
    client, provider = env
    match(client, title="旅人", artist="罗云熙", limit=2)
    match(client, title="旅人", artist="罗云熙", limit=2)
    fetched = [p["id"] for path, p in provider.calls if path == "/lyric"]
    assert len(fetched) == 2
    assert len(set(fetched)) == 2


def test_匹配接口全部平台失败返回503(env):
    client, provider = env
    provider.failing = {"netease", "tencent", "kugou"}
    assert match(client, title="旅人").status_code == 503


def test_公网网关放行匹配接口():
    assert any(
        method == "GET" and pattern.fullmatch("/v2/library/external-lyrics/match")
        for method, pattern in _PUBLIC_READ_ROUTES
    )


def test_匹配接口歌名歌手相同时时长更接近的排第一(env):
    client, provider = env
    provider.search_items = {
        ("netease", "title"): [
            hit("netease", "111", "旅人", "罗云熙", duration_ms=180000),
            hit("netease", "222", "旅人", "罗云熙", duration_ms=253783),
        ],
    }
    body = match(client, title="旅人", artist="罗云熙", duration=254).json()
    assert [item["external_id"] for item in body["items"]][:2] == ["222", "111"]
