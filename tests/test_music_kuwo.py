"""issue 173/174/176: Kuwo direct adapter (LX Music's APIs) and same-catalog standby sources."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_music_sources import AUTH, fake_fetch, settings

from rhythm_metadata_api.application.music_adapters import (
    ADAPTERS,
    KUWO_AUDIO_HOSTS,
    KUWO_DEFAULT_RESOLVERS,
    FeiniaoAdapter,
    KuwoAdapter,
)
from rhythm_metadata_api.application.music_sources import (
    MusicSourceConfig,
    MusicSourceCreate,
    MusicSourcePatch,
    UrlResolver,
)
from rhythm_metadata_api.domain.v2.errors import V2NotFound, V2Unavailable
from rhythm_metadata_api.main import create_app

BASE = "http://search.kuwo.cn/"


@pytest.fixture
def public_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        "rhythm_metadata_api.application.music_adapters.socket.getaddrinfo",
        lambda *args: [(None, None, None, None, ("1.1.1.1", 80))],
    )

JSON_RESOLVER = {"name": "json", "kind": "json", "url_template": "https://resolver-a.example.org/kw/{id}"}
REDIRECT_RESOLVER = {"name": "redirect", "kind": "redirect", "url_template": "https://resolver-b.example.org/?id={id}"}


def kuwo_row(rid: str = "228908", name: str = "晴天", artist: str = "周杰伦", duration: str = "269") -> dict:
    return {"MUSICRID": f"MUSIC_{rid}", "SONGNAME": name, "ARTIST": artist, "ALBUM": "叶惠美",
            "DURATION": duration, "web_albumpic_short": "120/s3s94/93/211513640.jpg"}


class FakeKuwo:
    """Records every upstream call; answers search, song info and the two resolver kinds."""

    def __init__(self, *, total: int = 1, rows=None, lrclist=None, json_url: str | None = "http://car-er.kuwo.cn/a.mp3",
                 location: str | None = "http://car-lw.kuwo.cn/b.mp3", search_payload: dict | None = None,
                 info_failures: int = 0, search_glitches: int = 0):
        self.calls: list[tuple[str, dict]] = []
        self.total, self.json_url, self.location = total, json_url, location
        self.rows = [kuwo_row()] if rows is None else rows
        self.search_payload = search_payload
        self.info_failures, self.search_glitches = info_failures, search_glitches
        self.lrclist = [{"time": "12.5", "lineLyric": "故事的小黄花"}] if lrclist is None else lrclist

    def fetch(self, url: str, headers=None, *, strict_type: bool = True) -> dict:
        self.calls.append((url, dict(headers or {})))
        assert strict_type is False
        host = urlsplit(url).hostname
        if host == "search.kuwo.cn":
            if self.search_glitches:
                self.search_glitches -= 1
                return {"TOTAL": "5", "SHOW": "0", "abslist": []}
            return self.search_payload or {"TOTAL": str(self.total), "abslist": self.rows}
        if host == "m.kuwo.cn":
            if self.info_failures:
                self.info_failures -= 1
                return {"data": None, "msg": "音乐查询失败", "status": 301}
            return {"data": {"songinfo": {"pic": "http://img1.kwcdn.kuwo.cn/star/albumcover/240/a.jpg"},
                             "lrclist": self.lrclist}}
        if host == "resolver-a.example.org":
            if self.json_url is None:
                raise V2Unavailable("resolver down")
            return {"code": 0, "url": self.json_url}
        raise AssertionError(url)

    def locate(self, url: str, headers=None) -> str:
        self.calls.append((url, dict(headers or {})))
        if self.location is None:
            raise V2Unavailable("resolver down")
        return self.location

    def adapter(self, resolvers=(JSON_RESOLVER,)) -> KuwoAdapter:
        config = MusicSourceConfig.model_validate({**KuwoAdapter.default_config, "url_resolvers": list(resolvers)})
        return KuwoAdapter(fetch=self.fetch, locate=self.locate).configured(config.model_dump())

    def hosts(self) -> list[str]:
        return [urlsplit(url).hostname or "" for url, _ in self.calls]


# ---------- adapter ----------

def test_酷我搜索恰好请求一次并映射歌名歌手时长与封面() -> None:
    kuwo = FakeKuwo(total=31, rows=[{**kuwo_row(), "SONGNAME": "晴天 &amp; Live", "ARTIST": "周杰伦&amp;五月天"}])
    result = kuwo.adapter().search(BASE, "kuwo", "晴天", 1)
    song = result["items"][0]
    assert len(kuwo.calls) == 1
    params = parse_qs(urlsplit(kuwo.calls[0][0]).query)
    assert params["all"] == ["晴天"] and params["pn"] == ["0"] and params["rn"] == ["30"]
    assert song["id"] == "kuwo:228908" and song["external_id"] == "228908"
    assert song["title"] == "晴天 & Live" and song["artists"] == ["周杰伦、五月天"]
    assert song["duration_ms"] == 269000 and song["album"] == "叶惠美"
    assert "img1.kwcdn.kuwo.cn%2Fstar%2Falbumcover%2F500%2Fs3s94" in song["artwork_url"]
    assert song["artwork_url"].endswith("&source=kuwo")
    assert result["has_more"] is True


def test_酷我搜索没有专辑封面时用歌手图_都没有时为空() -> None:
    rows = [{**kuwo_row("1"), "web_albumpic_short": "", "web_artistpic_short": "120/s4s56/58/291211030.jpg"},
            {**kuwo_row("2"), "web_albumpic_short": "", "web_artistpic_short": ""}]
    items = FakeKuwo(rows=rows).adapter().search(BASE, "kuwo", "晴天", 1)["items"]
    assert "star%2Fstarheads%2F500%2Fs4s56" in items[0]["artwork_url"]
    assert items[1]["artwork_url"] is None


def test_酷我搜索最后一页没有下一页且非法ID被跳过() -> None:
    kuwo = FakeKuwo(total=30, rows=[kuwo_row(), {"MUSICRID": "MUSIC_a.b"}])
    result = kuwo.adapter().search(BASE, "kuwo", "晴天", 1)
    assert [item["external_id"] for item in result["items"]] == ["228908"]
    assert result["has_more"] is False


def test_酷我搜索响应结构变化时来源不可用() -> None:
    kuwo = FakeKuwo(search_payload={"TOTAL": "0"})
    with pytest.raises(V2Unavailable):
        kuwo.adapter().search(BASE, "kuwo", "晴天", 1)
    assert kuwo.hosts() == ["search.kuwo.cn"] * 3


def test_酷我歌词把秒数转成LRC时间轴() -> None:
    kuwo = FakeKuwo(lrclist=[{"time": "12.5", "lineLyric": "故事的小黄花"}, {"time": "75.004", "lineLyric": "从出生那年"},
                             {"time": "bad", "lineLyric": "丢弃"}])
    assert kuwo.adapter().lyrics(BASE, "kuwo", "228908")["lrc"] == "[00:12.50]故事的小黄花\n[01:15.00]从出生那年"


def test_酷我歌曲信息偶发查询失败时立即重试() -> None:
    kuwo = FakeKuwo(info_failures=2)
    assert kuwo.adapter().lyrics(BASE, "kuwo", "228908")["lrc"] == "[00:12.50]故事的小黄花"
    assert kuwo.hosts() == ["m.kuwo.cn"] * 3


def test_酷我歌曲信息连续三次失败就不再请求并返回不存在() -> None:
    kuwo = FakeKuwo(info_failures=5)
    with pytest.raises(V2NotFound):
        kuwo.adapter().lyrics(BASE, "kuwo", "228908")
    assert kuwo.hosts() == ["m.kuwo.cn"] * 3


def test_酷我搜索有总数却没展示时重试() -> None:
    kuwo = FakeKuwo(search_glitches=1)
    assert kuwo.adapter().search(BASE, "kuwo", "晴天", 1)["items"][0]["external_id"] == "228908"
    assert kuwo.hosts() == ["search.kuwo.cn"] * 2


def test_酷我没有歌词时返回不存在() -> None:
    with pytest.raises(V2NotFound):
        FakeKuwo(lrclist=[]).adapter().lyrics(BASE, "kuwo", "228908")


def test_取链第一个失败时用下一个且每个恰好请求一次(public_dns) -> None:
    kuwo = FakeKuwo(json_url=None)
    url = kuwo.adapter([JSON_RESOLVER, REDIRECT_RESOLVER]).audio_url(BASE, "228908", "mp3")
    assert url == "http://car-lw.kuwo.cn/b.mp3"
    assert kuwo.hosts() == ["resolver-a.example.org", "resolver-b.example.org"]
    assert kuwo.calls[0][0] == "https://resolver-a.example.org/kw/228908"


def test_取链返回白名单外域名时换下一个(public_dns) -> None:
    kuwo = FakeKuwo(json_url="http://evil.example.com/a.mp3")
    assert kuwo.adapter([JSON_RESOLVER, REDIRECT_RESOLVER]).audio_url(BASE, "228908", "auto") == "http://car-lw.kuwo.cn/b.mp3"


def test_全部取链失败时来源不可用(public_dns) -> None:
    with pytest.raises(V2Unavailable):
        FakeKuwo(json_url=None, location=None).adapter([JSON_RESOLVER, REDIRECT_RESOLVER]).audio_url(BASE, "1", "mp3")


def test_停用的取链接口不会被请求(public_dns) -> None:
    kuwo = FakeKuwo()
    kuwo.adapter([{**JSON_RESOLVER, "enabled": False}, REDIRECT_RESOLVER]).audio_url(BASE, "228908", "mp3")
    assert kuwo.hosts() == ["resolver-b.example.org"]


def test_取链请求带上配置的请求头(public_dns) -> None:
    kuwo = FakeKuwo()
    kuwo.adapter([{**JSON_RESOLVER, "headers": {"X-Request-Key": "share-v3"}}]).audio_url(BASE, "228908", "mp3")
    assert kuwo.calls[0][1] == {"X-Request-Key": "share-v3"}


def test_非mp3音质直接不存在且不请求取链() -> None:
    kuwo = FakeKuwo()
    with pytest.raises(V2NotFound):
        kuwo.adapter().audio_url(BASE, "228908", "flac")
    assert kuwo.calls == []


# ---------- configuration ----------

@pytest.mark.parametrize("template", [
    "https://1.2.3.4/kw/{id}",            # IP
    "https://resolver.example.org/kw/",   # no {id}
    "https://resolver.example.org/{id}/{q}",
    "ftp://resolver.example.org/{id}",
])
def test_取链模板必须是公网域名并且恰好含一个id占位(template) -> None:
    with pytest.raises(ValidationError):
        UrlResolver(name="x", kind="json", url_template=template)


def test_取链请求头不能换行() -> None:
    with pytest.raises(ValidationError):
        UrlResolver(name="x", kind="json", url_template="https://r.example.org/{id}", headers={"X-Key": "a\r\nb"})


def test_新建酷我实例默认带取链与酷我域名且显式配置优先(tmp_path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        default = service.create(MusicSourceCreate(id="kuwo", name="酷我", adapter="kuwo", base_url=BASE))
        assert [r["name"] for r in default["config"]["url_resolvers"]] == [r["name"] for r in KUWO_DEFAULT_RESOLVERS]
        assert default["config"]["audio_hosts"] == sorted(KUWO_AUDIO_HOSTS)
        assert default["config"]["lyric_filters"] == {"exact_lines": [], "by_tag_contains": []}
        explicit = service.create(MusicSourceCreate(
            id="kuwo-b", name="酷我B", adapter="kuwo", base_url=BASE,
            config=MusicSourceConfig(audio_hosts=["car-er.kuwo.cn"]),
        ))
        assert explicit["config"]["audio_hosts"] == ["car-er.kuwo.cn"]
        assert [r["name"] for r in explicit["config"]["url_resolvers"]] == [r["name"] for r in KUWO_DEFAULT_RESOLVERS]


def test_飞鸟实例的默认配置不受影响(tmp_path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        created = client.app.state.v2_container.music_sources.create(MusicSourceCreate(
            id="alt", name="飞鸟B", adapter="feiniao", base_url="https://example.org/"))
        assert created["config"]["url_resolvers"] == []
        assert created["config"]["upstream_source"] == "酷我音乐"


# ---------- standby between Feiniao and Kuwo ----------

class FeiniaoSwitch:
    def __init__(self) -> None:
        self.down = False
        self.calls: list[str] = []

    def fetch(self, url: str) -> dict:
        api = parse_qs(urlsplit(url).query)["api"][0]
        self.calls.append(api)
        if self.down:
            raise V2Unavailable("feiniao is down")
        return fake_fetch(url)


def standby_client(tmp_path, monkeypatch, *, with_kuwo: bool = True, kuwo: FakeKuwo | None = None):
    feiniao = FeiniaoSwitch()
    kuwo = kuwo or FakeKuwo()
    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=feiniao.fetch))
    monkeypatch.setitem(ADAPTERS, "kuwo", KuwoAdapter(fetch=kuwo.fetch, locate=kuwo.locate))
    client = TestClient(create_app(settings(tmp_path)))
    client.__enter__()
    service = client.app.state.v2_container.music_sources
    service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
    if with_kuwo:
        service.create(MusicSourceCreate(
            id="kuwo", name="酷我直连", adapter="kuwo", base_url=BASE, priority=150, enabled=True,
            config=MusicSourceConfig(url_resolvers=[JSON_RESOLVER]),
        ))
    return client, service, feiniao, kuwo


def search(client: TestClient) -> dict:
    response = client.get("/v2/music/search?q=旅人", headers=AUTH)
    assert response.status_code == 200
    return response.json()


def test_飞鸟可用时搜索只返回飞鸟且不请求酷我(tmp_path, monkeypatch) -> None:
    client, _, feiniao, kuwo = standby_client(tmp_path, monkeypatch)
    with client:
        body = search(client)
        assert [group["source_id"] for group in body["sources"]] == ["feiniao"]
        assert kuwo.calls == []
        assert feiniao.calls == ["search_with_source"]


def test_飞鸟不可用时搜索由酷我顶上且六十秒内不再请求飞鸟(tmp_path, monkeypatch) -> None:
    client, _, feiniao, kuwo = standby_client(tmp_path, monkeypatch)
    feiniao.down = True
    with client:
        first = search(client)
        second = search(client)
        assert [group["source_id"] for group in first["sources"]] == ["kuwo"]
        assert first["unavailable_sources"] == ["feiniao"]
        assert second["unavailable_sources"] == ["feiniao"]
        assert feiniao.calls == ["search_with_source"]
        assert kuwo.hosts() == ["search.kuwo.cn", "search.kuwo.cn"]


def test_飞鸟取链失败时按同一rid走酷我且缓存期内只解析一次(tmp_path, monkeypatch, public_dns) -> None:
    client, service, feiniao, kuwo = standby_client(tmp_path, monkeypatch)
    feiniao.down = True
    with client:
        assert service.audio_url("feiniao", "228908", "mp3") == "http://car-er.kuwo.cn/a.mp3"
        assert service.audio_url("feiniao", "228908", "mp3") == "http://car-er.kuwo.cn/a.mp3"
        assert kuwo.calls == [("https://resolver-a.example.org/kw/228908", {})]
        assert feiniao.calls == ["get_song_with_source"]


def test_飞鸟熔断期间新歌曲直接走酷我不再等飞鸟(tmp_path, monkeypatch, public_dns) -> None:
    client, service, feiniao, _ = standby_client(tmp_path, monkeypatch)
    feiniao.down = True
    with client:
        service.audio_url("feiniao", "1", "mp3")
        service.audio_url("feiniao", "2", "mp3")
        assert feiniao.calls == ["get_song_with_source"]


def test_飞鸟歌词失败时由酷我提供且ID仍是飞鸟的(tmp_path, monkeypatch) -> None:
    client, _, feiniao, _ = standby_client(tmp_path, monkeypatch)
    feiniao.down = True
    with client:
        response = client.get("/v2/music/sources/feiniao/songs/228908/lyrics", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == "feiniao:228908" and body["source_id"] == "feiniao"
        assert body["lrc"] == "[00:12.50]故事的小黄花"


def test_没有备用来源时飞鸟故障照旧报不可用(tmp_path, monkeypatch) -> None:
    client, service, feiniao, kuwo = standby_client(tmp_path, monkeypatch, with_kuwo=False)
    feiniao.down = True
    with client:
        with pytest.raises(V2Unavailable):
            service.audio_url("feiniao", "228908", "mp3")
        assert client.get("/v2/music/search?q=旅人", headers=AUTH).status_code == 503
        assert kuwo.calls == []


def test_网易实例故障不会拿酷我同ID顶替(tmp_path, monkeypatch) -> None:
    client, service, feiniao, kuwo = standby_client(tmp_path, monkeypatch)
    with client:
        service.create(MusicSourceCreate(
            id="netease", name="网易", adapter="feiniao", base_url="https://netease.example.org/",
            priority=200, enabled=True, config=MusicSourceConfig(upstream_source="网易云音乐"),
        ))
        feiniao.down = True
        with pytest.raises(V2Unavailable):
            service.lyrics("netease", "228908")
        assert kuwo.calls == []


# ---------- issue 174: Kuwo playlists and the playlist standby ----------

def hot_item(pid: str = "3678617986", digest: str = "8", total: str = "104") -> dict:
    return {"id": pid, "name": "古风 &amp; 戏腔", "img": "http://img1.kuwo.cn/star/userpl2015/a.jpg",
            "total": total, "digest": digest}


def pl_song(rid: str = "6304356") -> dict:
    return {"id": rid, "name": "牵丝戏", "artist": "银临&Aki阿杰", "album": "牵丝戏", "duration": "239",
            "albumpic": "http://img1.kuwo.cn/star/albumcover/120/6/48/1406239650.jpg"}


class FakeKuwoLists(FakeKuwo):
    """FakeKuwo plus the hot list (wapi), playlist search (ft=playlist) and playlist detail (pl.svc)."""

    def __init__(self, *, hot=None, hot_total: int = 1754, found=None, found_total: str = "45",
                 songs=None, song_total: str = "104", detail_failures: int = 0, **kwargs):
        super().__init__(**kwargs)
        self.hot = [hot_item()] if hot is None else hot
        self.hot_total, self.found, self.found_total = hot_total, found, found_total
        self.songs = [pl_song()] if songs is None else songs
        self.song_total, self.detail_failures = song_total, detail_failures

    def fetch(self, url: str, headers=None, *, strict_type: bool = True) -> dict:
        host = urlsplit(url).hostname
        params = parse_qs(urlsplit(url).query)
        if host == "wapi.kuwo.cn":
            self.calls.append((url, {}))
            return {"code": 200, "data": {"data": self.hot, "pn": 1, "rn": 36, "total": self.hot_total}}
        if host == "nplserver.kuwo.cn":
            self.calls.append((url, {}))
            if self.detail_failures:
                self.detail_failures -= 1
                return {"result": "error"}
            return {"result": "ok", "total": self.song_total, "musiclist": self.songs}
        if host == "search.kuwo.cn" and params.get("ft") == ["playlist"]:
            self.calls.append((url, {}))
            found = [{"playlistid": "2891238463", "name": "流行", "pic": "http://img1.kwcdn.kuwo.cn/a.jpg",
                      "songnum": "45"}] if self.found is None else self.found
            return {"TOTAL": self.found_total, "abslist": found}
        return super().fetch(url, headers, strict_type=strict_type)


def test_酷我热门歌单恰好请求一次_只留普通歌单并映射封面与歌曲数() -> None:
    kuwo = FakeKuwoLists(hot=[hot_item(), hot_item("99", digest="13"), hot_item("bad.id")])
    result = kuwo.adapter().playlists(BASE, "kuwo", 2)
    assert kuwo.hosts() == ["wapi.kuwo.cn"]
    params = parse_qs(urlsplit(kuwo.calls[0][0]).query)
    assert params["pn"] == ["2"] and params["rn"] == ["36"] and params["order"] == ["hot"]
    assert [item["id"] for item in result["items"]] == ["kuwo:3678617986"]
    item = result["items"][0]
    assert item["name"] == "古风 & 戏腔" and item["song_count"] == 104
    assert "img1.kuwo.cn%2Fstar%2Fuserpl2015" in item["artwork_url"]
    assert result["total_pages"] == 49 and result["has_more"] is True


def test_酷我热门歌单响应结构变化时来源不可用() -> None:
    kuwo = FakeKuwoLists()
    kuwo.fetch = lambda url, headers=None, *, strict_type=True: {"code": 500, "data": None}
    with pytest.raises(V2Unavailable):
        kuwo.adapter().playlists(BASE, "kuwo", 1)


def test_酷我歌单搜索用kt参数_页码从0开始且按20条判断下一页() -> None:
    kuwo = FakeKuwoLists(found_total="45")
    first = kuwo.adapter().search_playlists(BASE, "kuwo", "流行", 2)
    last = FakeKuwoLists(found_total="40").adapter().search_playlists(BASE, "kuwo", "流行", 2)
    params = parse_qs(urlsplit(kuwo.calls[0][0]).query)
    assert len(kuwo.calls) == 1
    assert params["client"] == ["kt"] and params["ft"] == ["playlist"] and params["pn"] == ["1"]
    assert params["rn"] == ["20"]
    assert first["items"][0]["id"] == "kuwo:2891238463" and first["items"][0]["song_count"] == 45
    assert first["has_more"] is True and last["has_more"] is False


def test_酷我歌单详情映射歌曲且封面升到500_页码从0开始() -> None:
    kuwo = FakeKuwoLists(song_total="41")
    result = kuwo.adapter().playlist(BASE, "kuwo", "3678617986", 2)
    params = parse_qs(urlsplit(kuwo.calls[0][0]).query)
    assert len(kuwo.calls) == 1
    assert params["pid"] == ["3678617986"] and params["pn"] == ["1"] and params["rn"] == ["20"]
    song = result["items"][0]
    assert result["id"] == "kuwo:3678617986" and song["id"] == "kuwo:6304356"
    assert song["artists"] == ["银临、Aki阿杰"] and song["duration_ms"] == 239000
    assert "albumcover%2F500%2F6%2F48" in song["artwork_url"]
    assert result["has_more"] is True
    assert FakeKuwoLists(song_total="40").adapter().playlist(BASE, "kuwo", "1", 2)["has_more"] is False


def test_酷我歌单详情偶发失败时重试_连续三次失败就不可用() -> None:
    flaky = FakeKuwoLists(detail_failures=2)
    assert flaky.adapter().playlist(BASE, "kuwo", "1", 1)["items"]
    assert len(flaky.calls) == 3
    broken = FakeKuwoLists(detail_failures=5)
    with pytest.raises(V2Unavailable):
        broken.adapter().playlist(BASE, "kuwo", "1", 1)
    assert len(broken.calls) == 3


class FeiniaoLists(FeiniaoSwitch):
    """Feiniao with a home playlist that passes the recommended filters, and an empty keyword search."""

    def fetch(self, url: str) -> dict:
        api = parse_qs(urlsplit(url).query)["api"][0]
        if api in {"playlist_home", "search_playlist"}:
            self.calls.append(api)
            if self.down:
                raise V2Unavailable("feiniao is down")
            if api == "search_playlist":
                return {"code": 200, "data": {"list": [{"id": "88", "name": "飞鸟搜到的", "total": "30"}],
                                              "total": "1"}}
            return {"code": 200, "pagination": {"total_pages": 1}, "data": [{
                "id": "77", "name": "飞鸟精选", "total_songs": "30",
                "img": "http://img1.kuwo.cn/star/userpl2015/b.jpg"}]}
        return super().fetch(url)


def lists_client(tmp_path, monkeypatch, kuwo: FakeKuwoLists | None = None):
    feiniao = FeiniaoLists()
    kuwo = kuwo or FakeKuwoLists()
    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=feiniao.fetch))
    client, service, _, kuwo = standby_client(tmp_path, monkeypatch, kuwo=kuwo)
    ADAPTERS["feiniao"] = FeiniaoAdapter(fetch=feiniao.fetch)
    return client, service, feiniao, kuwo


def recommended(client: TestClient) -> list[str]:
    response = client.get("/v2/music/recommended-albums?limit=10", headers=AUTH)
    assert response.status_code == 200
    return [item["id"] for item in response.json()["items"]]


def test_飞鸟可用时首页推荐歌单只用飞鸟且不请求酷我(tmp_path, monkeypatch) -> None:
    client, _, _, kuwo = lists_client(tmp_path, monkeypatch)
    with client:
        assert set(recommended(client)) == {"feiniao:77"}
        assert kuwo.calls == []


def test_飞鸟歌单不可用时推荐由酷我顶上且飞鸟只请求一页(tmp_path, monkeypatch) -> None:
    client, _, feiniao, kuwo = lists_client(tmp_path, monkeypatch)
    feiniao.down = True
    with client:
        kuwo_pool = {"kuwo:3678617986", "kuwo:2891238463"}  # hot list + the hourly keyword search
        assert set(recommended(client)) == kuwo_pool
        built = len(kuwo.calls)
        assert set(recommended(client)) == kuwo_pool
        assert len(kuwo.calls) == built  # the Kuwo pool is cached like Feiniao's
        assert feiniao.calls == ["playlist_home"]


def test_飞鸟熔断期间仍用它上次的推荐池且不重新请求(tmp_path, monkeypatch) -> None:
    client, service, feiniao, kuwo = lists_client(tmp_path, monkeypatch)
    with client:
        def ids() -> set[str]:
            return {item["id"] for item in service.recommended_albums(10)["items"]}
        assert ids() == {"feiniao:77"}
        service._recommended_pools["feiniao"] = (0.0, service._recommended_pools["feiniao"][1])  # expired
        feiniao.down = True
        service._mark_down("feiniao")
        assert ids() == {"feiniao:77"}
        assert feiniao.calls == ["playlist_home", "search_playlist"]
        assert kuwo.calls == []


def test_飞鸟歌单详情失败时由酷我按同pid同页提供且歌单ID仍是飞鸟的(tmp_path, monkeypatch) -> None:
    client, _, feiniao, kuwo = lists_client(tmp_path, monkeypatch)
    feiniao.down = True
    with client:
        response = client.get("/v2/music/sources/feiniao/playlists/77?page=2", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == "feiniao:77"
        assert [song["id"] for song in body["items"]] == ["kuwo:6304356"]
        assert len(kuwo.calls) == 1
        params = parse_qs(urlsplit(kuwo.calls[0][0]).query)
        assert params["pid"] == ["77"] and params["pn"] == ["1"]
        assert feiniao.calls == ["playlist_detail"]


def test_飞鸟可用时歌单搜索只返回飞鸟且不请求酷我(tmp_path, monkeypatch) -> None:
    client, _, feiniao, kuwo = lists_client(tmp_path, monkeypatch)
    with client:
        body = client.get("/v2/music/playlists/search?q=流行", headers=AUTH).json()
        assert [group["source_id"] for group in body["sources"]] == ["feiniao"]
        assert kuwo.calls == [] and feiniao.calls == ["search_playlist"]


def test_飞鸟歌单搜索不可用时由酷我顶上且六十秒内不再请求飞鸟(tmp_path, monkeypatch) -> None:
    client, _, feiniao, kuwo = lists_client(tmp_path, monkeypatch)
    feiniao.down = True
    with client:
        first = client.get("/v2/music/playlists/search?q=流行", headers=AUTH).json()
        second = client.get("/v2/music/playlists/search?q=流行", headers=AUTH).json()
        assert [group["source_id"] for group in first["sources"]] == ["kuwo"]
        assert first["unavailable_sources"] == ["feiniao"] == second["unavailable_sources"]
        assert feiniao.calls == ["search_playlist"]
        assert kuwo.hosts() == ["search.kuwo.cn", "search.kuwo.cn"]


# ---------- issue 176: metadata writes with the cover a client displays ----------

def kuwo_metadata_client(tmp_path, monkeypatch, kuwo: FakeKuwo | None = None):
    client, service, _, kuwo = standby_client(tmp_path, monkeypatch, kuwo=kuwo)
    return client, service, kuwo


def put_kuwo(client: TestClient, **fields) -> object:
    body = {"title": "那时雨", "artists": ["徐良"], "lyrics_lrc": "[00:01.00]石板桥下", **fields}
    return client.put("/v2/music/sources/kuwo/songs/3274963/metadata", headers=AUTH, json=body)


RAW_COVER = "http://img1.kwcdn.kuwo.cn/star/albumcover/500/s4s56/18/1580751311.jpg"
PROXY_COVER = "/v2/music/artwork?url=http%3A%2F%2Fimg1.kwcdn.kuwo.cn%2Fstar%2Falbumcover%2F500%2Fs4s56%2F18%2F1580751311.jpg&source=kuwo"


def test_白名单域名的http封面改写成代理路径且歌词照常保存(tmp_path, monkeypatch) -> None:
    client, _, _ = kuwo_metadata_client(tmp_path, monkeypatch)
    with client:
        response = put_kuwo(client, artwork_url=RAW_COVER)
        assert response.status_code == 200
        body = response.json()
        assert body["artwork_url"] == PROXY_COVER
        assert body["lyrics_lrc"] == "[00:01.00]石板桥下"


def test_带主机名的本站代理封面去掉主机名后保存(tmp_path, monkeypatch) -> None:
    client, _, _ = kuwo_metadata_client(tmp_path, monkeypatch)
    with client:
        response = put_kuwo(client, artwork_url="http://117.72.208.153:8010" + PROXY_COVER)
        assert response.status_code == 200
        assert response.json()["artwork_url"] == PROXY_COVER


def test_不合规封面只丢掉封面_已存封面保留且歌词照常写入(tmp_path, monkeypatch, caplog) -> None:
    client, _, _ = kuwo_metadata_client(tmp_path, monkeypatch)
    with client:
        assert put_kuwo(client, artwork_url=PROXY_COVER, lyrics_lrc=None).status_code == 200
        response = put_kuwo(client, artwork_url="http://evil.example.org/a.jpg", mode="manual")
        assert response.status_code == 200
        body = response.json()
        assert body["artwork_url"] == PROXY_COVER
        assert body["lyrics_lrc"] == "[00:01.00]石板桥下"
        assert "Music image dropped from metadata write" in caplog.text


def test_新记录的不合规封面存为空而不是报错(tmp_path, monkeypatch) -> None:
    client, _, _ = kuwo_metadata_client(tmp_path, monkeypatch)
    with client:
        response = put_kuwo(client, artwork_url="http://evil.example.org/a.jpg")
        assert response.status_code == 200
        assert response.json()["artwork_url"] is None


def test_酷我歌曲信息带非200状态时重试() -> None:
    kuwo = FakeKuwo()
    real = kuwo.fetch
    answers = [{"status": 301, "msg": "音乐查询失败", "data": {}}]

    def fetch(url, headers=None, *, strict_type=True):
        if urlsplit(url).hostname == "m.kuwo.cn" and answers:
            kuwo.calls.append((url, {}))
            return answers.pop()
        return real(url, headers, strict_type=strict_type)

    kuwo.fetch = fetch
    song = kuwo.adapter().song(BASE, "kuwo", "228908")
    assert kuwo.hosts() == ["m.kuwo.cn", "m.kuwo.cn"]
    assert "albumcover%2F500%2Fa.jpg" in song["artwork_url"]


def test_酷我歌曲信息三次都失败时详情照常返回只是没有封面() -> None:
    kuwo = FakeKuwo(info_failures=5)
    song = kuwo.adapter().song(BASE, "kuwo", "228908")
    assert song["id"] == "kuwo:228908" and song["artwork_url"] is None
    assert kuwo.hosts() == ["m.kuwo.cn"] * 3
