"""Issue 140: source configuration and the client contract survive upstream shape changes."""

from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from rhythm_metadata_api.application.device_auth import hash_admin_password
from rhythm_metadata_api.application.music_adapters import (
    ADAPTERS,
    _AUDIO_HOSTS,
    FeiniaoAdapter,
    clean_feiniao_lrc,
    validate_public_url,
)
from rhythm_metadata_api.application.music_sources import (
    MusicSongMetadataWrite,
    MusicSourceCreate,
    MusicSourcePatch,
)
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.domain.v2.errors import V2Conflict, V2DomainError
from rhythm_metadata_api.main import create_app
from rhythm_metadata_api.public_main import create_public_app

TOKEN = "issue140-test-bootstrap-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def settings(tmp_path: Path) -> Settings:
    return Settings(
        bootstrap_token=TOKEN,
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
        public_token_secret="x" * 48,
        public_admin_password_hash=hash_admin_password("correct horse battery staple"),
    )


def fake_fetch(url: str) -> dict:
    params = parse_qs(urlsplit(url).query)
    api = params["api"][0]
    if api == "search_with_source":
        return {"code": 0, "data": {"songs": [{
            "rid": 493285911, "name": "旅人", "artist": "王铮亮",
            "album": "旅人", "duration": 240,
        }], "hasMore": False}}
    if api == "recommend":
        return {"code": 200, "data": [{"rid": 42, "name": "推荐歌", "artist": "歌手"}],
                "pagination": {"total_pages": 2}}
    if api == "playlist_home":
        return {"code": 200, "data": [{"id": "77", "name": "精选", "total_songs": "2"}],
                "pagination": {"total_pages": 1}}
    if api == "playlist_detail":
        return {"code": 200, "data": [{"rid": 42, "name": "推荐歌", "artist": "歌手"}],
                "pagination": {"total_pages": 1}}
    if api == "get_song_with_source":
        return {"data": [{"url": "http://car-er.kuwo.cn/sample.mp3", "type": "mp3"}]}
    if api == "get_lyrics_with_source":
        return {"msg": "成功", "gc": "[00:01.00]旅人"}
    raise AssertionError(api)


def test_feiniao_lyrics_watermark_is_removed_without_dropping_song_credits() -> None:
    original = (
        "[ti:]\r\n[by:飞鸟·轻音v4.4.0]\r\n"
        "[00:00.000]该音源由 飞鸟·轻音 提供服务\r\n"
        "[00:00.000]官网:飞鸟.site\r\n"
        "[00:00.000]————下面是正歌部分————\r\n"
        "[00:00.000]演员 - 薛之谦\r\n"
        "[00:02.000]词：薛之谦\r\n"
        "[00:21.000]简单点\r\n"
    )
    cleaned = clean_feiniao_lrc(original)
    assert "飞鸟" not in cleaned
    assert "[00:00.000]演员 - 薛之谦" in cleaned
    assert "[00:02.000]词：薛之谦" in cleaned
    assert "[00:21.000]简单点" in cleaned


def test_admin_config_and_uniform_client_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fake_fetch))
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        # The migration inserts a disabled example; no external source is activated by deployment.
        sources = service.list_sources()["items"]
        assert len(sources) == 1 and sources[0]["id"] == "feiniao"
        assert not sources[0]["enabled"]
        assert client.get("/v2/music/sources", headers=AUTH).json() == {"items": []}
        assert client.get("/v2/music/search?q=旅人", headers=AUTH).json() == {
            "sources": [], "unavailable_sources": [],
        }
        source = service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
        assert source["revision"] == 2
        assert client.get("/v2/music/sources", headers=AUTH).json()["items"] == [
            {"id": "feiniao", "name": "飞鸟轻音", "priority": 100}
        ]
        result = client.get("/v2/music/search?q=旅人", headers=AUTH).json()
        song = result["sources"][0]["items"][0]
        assert song["id"] == "feiniao:493285911"
        assert song["duration_ms"] == 240000
        assert "url" not in song
        assert client.get("/v2/music/sources/feiniao/recommend?page=1", headers=AUTH).json()[
            "has_more"
        ]
        assert client.get("/v2/music/sources/feiniao/playlists", headers=AUTH).json()[
            "items"
        ][0]["song_count"] == 2
        assert client.get("/v2/music/sources/feiniao/songs/42/lyrics", headers=AUTH).json()[
            "lrc"
        ] == "[00:01.00]旅人"
        assert client.get("/v2/music/sources/feiniao/songs/42", headers=AUTH).json()[
            "qualities"
        ] == ["mp3"]


def test_add_and_disable_source_without_changing_client_routes(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        added = service.create(MusicSourceCreate(
            id="alternate", name="备用来源", adapter="feiniao",
            base_url="https://example.org/", priority=10, enabled=True,
        ))
        assert added["id"] == "alternate"
        assert [item["id"] for item in service.list_sources(enabled_only=True)["items"]] == [
            "alternate"
        ]
        assert client.get("/v2/music/sources/alternate/songs/bad.id", headers=AUTH).status_code == 422
        assert client.get("/v2/music/sources/feiniao/songs/42", headers=AUTH).status_code == 404
        assert service.patch("alternate", MusicSourcePatch(revision=1, enabled=False))["enabled"] is False
        with pytest.raises(V2Conflict):
            service.patch("alternate", MusicSourcePatch(revision=1, enabled=True))
        assert client.get("/v2/music/sources", headers=AUTH).json() == {"items": []}


def test_failed_source_does_not_hide_healthy_search_results(tmp_path: Path, monkeypatch) -> None:
    def fetch(url: str) -> dict:
        if url.startswith("https://example.org/"):
            raise V2DomainError("upstream resolved to a private address")
        return fake_fetch(url)

    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fetch))
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        service.create(MusicSourceCreate(
            id="alternate", name="故障来源", adapter="feiniao",
            base_url="https://example.org/", priority=10, enabled=True,
        ))
        service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
        result = client.get("/v2/music/search?q=旅人", headers=AUTH).json()
        assert result["unavailable_sources"] == ["alternate"]
        assert result["sources"][0]["source_id"] == "feiniao"


def test_public_gateway_requires_device_proof(tmp_path: Path) -> None:
    with TestClient(create_public_app(settings(tmp_path))) as client:
        assert client.get("/v2/music/sources").status_code == 401
        assert client.get("/v2/admin/music-sources").status_code == 401
        assert client.put("/v2/music/sources/feiniao/songs/42/metadata", json={
            "title": "歌曲", "artists": ["歌手"],
        }).status_code == 401


def test_shared_metadata_precedes_upstream_and_manual_precedes_auto(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fake_fetch))
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
        path = "/v2/music/sources/feiniao/songs/42/metadata"
        assert client.get(path, headers=AUTH).status_code == 404
        first = client.put(path, headers=AUTH, json={
            "title": "推荐歌", "artists": ["歌手"], "album": "旧专辑",
            "duration_ms": 120000,
            "artwork_url": "/v2/music/artwork?url=https%3A%2F%2Fimg1.kuwo.cn%2Fx.jpg",
            "artist_avatar_url": "https://img1.kuwo.cn/avatar.jpg",
            "lyrics_lrc": "[00:01.00]歌词", "qualities": ["mp3"], "mode": "auto",
        })
        assert first.status_code == 200, first.text
        ingested_at = first.json()["ingested_at"]
        assert client.get(path, headers=AUTH).json()["title"] == "推荐歌"
        assert client.get("/v2/music/sources/feiniao/songs/42", headers=AUTH).json()[
            "title"
        ] == "推荐歌"
        assert client.get("/v2/music/sources/feiniao/songs/42/lyrics", headers=AUTH).json()[
            "lrc"
        ] == "[00:01.00]歌词"
        assert client.get("/v2/music/sources/feiniao/recommend", headers=AUTH).json()[
            "items"
        ][0]["album"] == "旧专辑"
        manual = client.put(path, headers=AUTH, json={
            "title": "人工曲名", "artists": ["歌手"], "album": "新专辑", "mode": "manual",
        }).json()
        assert manual["title"] == "人工曲名"
        assert manual["ingested_at"] == ingested_at
        automatic = client.put(path, headers=AUTH, json={
            "title": "自动曲名", "artists": ["歌手"], "mode": "auto",
        }).json()
        assert automatic["title"] == "人工曲名"


def test_admin_session_manages_sources(tmp_path: Path) -> None:
    with TestClient(create_public_app(settings(tmp_path))) as client:
        response = client.post("/v2/admin/session", json={
            "username": "admin", "password": "correct horse battery staple",
        })
        assert response.status_code == 200
        headers = {"Authorization": f"Bearer {response.json()['accessToken']}"}
        assert client.get("/v2/admin/music-sources/adapters", headers=headers).json()[
            "items"
        ][0]["id"] == "feiniao"
        source = client.get("/v2/admin/music-sources", headers=headers).json()["items"][0]
        assert source["enabled"] is False
        response = client.patch("/v2/admin/music-sources/feiniao", headers=headers, json={
            "revision": source["revision"], "enabled": True,
        })
        assert response.status_code == 200
        assert response.json()["enabled"] is True


def test_admin_can_browse_song_metadata_without_exposing_lyrics_in_list(tmp_path: Path) -> None:
    with TestClient(create_public_app(settings(tmp_path))) as client:
        session = client.post("/v2/admin/session", json={
            "username": "admin", "password": "correct horse battery staple",
        }).json()
        admin_auth = {"Authorization": f"Bearer {session['accessToken']}"}
        service = client.app.state.v2_container.music_sources
        service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
        service.put_metadata("feiniao", "42", MusicSongMetadataWrite(
            title="演员", artists=["薛之谦"], album="专辑",
            lyrics_lrc="[00:01.00]歌词", mode="auto",
        ))

        listing = client.get(
            "/v2/admin/music-sources/song-metadata?q=薛之谦&page_size=1",
            headers=admin_auth,
        )
        assert listing.status_code == 200
        data = listing.json()
        assert data["total"] == 1 and data["page_size"] == 1
        assert data["items"][0]["title"] == "演员"
        assert data["items"][0]["has_lyrics"] is True
        assert "lyrics_lrc" not in data["items"][0]
        detail = client.get(
            "/v2/admin/music-sources/song-metadata/feiniao/42", headers=admin_auth,
        )
        assert detail.json()["lyrics_lrc"] == "[00:01.00]歌词"
        assert client.get("/v2/admin/music-sources/song-metadata").status_code == 401


def test_music_images_are_uploaded_to_cos_and_delivered_by_stable_url(
    tmp_path: Path, monkeypatch,
) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"test-image"
    requests = []

    class FakeResponse:
        status = 200

        def __init__(self):
            self.headers = {"Content-Type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size):
            return png

    class FakeOpener:
        def open(self, request, timeout):
            requests.append(request)
            return FakeResponse()

    monkeypatch.setattr("rhythm_metadata_api.application.music_images._opener", FakeOpener())
    monkeypatch.setattr(
        "rhythm_metadata_api.application.music_images.validate_public_url",
        lambda url, **_kwargs: url,
    )
    configured = settings(tmp_path).model_copy(update={
        "cos_secret_id": "test-id", "cos_secret_key": "test-key",
        "music_image_cos_bucket": "images-1250000000",
        "music_image_public_base_url": "https://music.example.org",
    })
    with TestClient(create_app(configured)) as client:
        service = client.app.state.v2_container.music_sources
        service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
        saved = client.put("/v2/music/sources/feiniao/songs/42/metadata", headers=AUTH, json={
            "title": "演员", "artists": ["薛之谦"],
            "artwork_url": "/v2/music/artwork?url=https%3A%2F%2Fimg1.kuwo.cn%2Fcover.png",
        })
        assert saved.status_code == 200
        data = saved.json()
        assert data["artwork_storage"] == "cos"
        assert data["artwork_url"].startswith("https://music.example.org/v2/music/images/")
        assert len(requests) == 2 and requests[1].get_method() == "PUT"
        image_name = data["artwork_url"].rsplit("/", 1)[1]
        delivery = client.get(f"/v2/music/images/{image_name}", follow_redirects=False)
        assert delivery.status_code == 307
        assert "images-1250000000.cos.ap-guangzhou.myqcloud.com" in delivery.headers["location"]
        malformed = client.put("/v2/music/sources/feiniao/songs/42/metadata", headers=AUTH, json={
            "title": "演员", "artists": ["薛之谦"],
            "artwork_url": "/v2/music/images/not-a-hash.jpg",
        })
        assert malformed.status_code == 422


def test_invalid_upstream_targets_are_rejected_without_network(monkeypatch) -> None:
    from rhythm_metadata_api.application.music_sources import validate_base_url
    for target in ("http://127.0.0.1/", "http://example.org:99999/", "http://localhost/"):
        try:
            validate_base_url(target)
        except V2DomainError:
            pass
        else:
            raise AssertionError(f"accepted {target}")
    try:
        validate_public_url("https://example.org/image.jpg", hosts=frozenset({"img1.kuwo.cn"}))
    except V2DomainError:
        pass
    else:
        raise AssertionError("accepted unknown artwork host")
    monkeypatch.setattr(
        "rhythm_metadata_api.application.music_adapters.socket.getaddrinfo",
        lambda *args: [(None, None, None, None, ("198.18.7.208", 80))],
    )
    assert validate_public_url("http://music.xn--q35am7h.site/")
    assert validate_public_url(
        "http://car-bj.kuwo.cn/sample.mp3",
        hosts=frozenset({"car-er.kuwo.cn", "car-bj.kuwo.cn"}),
    )
    assert validate_public_url(
        "http://car-lv.kuwo.cn/sample.mp3",
        hosts=_AUDIO_HOSTS,
    )
    with pytest.raises(V2DomainError):
        validate_public_url(
            "http://other.kuwo.cn/sample.mp3",
            hosts=frozenset({"car-er.kuwo.cn", "car-bj.kuwo.cn"}),
        )
    monkeypatch.setattr(
        "rhythm_metadata_api.application.music_adapters.socket.getaddrinfo",
        lambda *args: [(None, None, None, None, ("127.0.0.1", 80))],
    )
    with pytest.raises(V2DomainError):
        validate_public_url("http://music.xn--q35am7h.site/")


def test_range_stream_preserves_partial_content(tmp_path: Path, monkeypatch) -> None:
    class FakeAudio(io.BytesIO):
        status = 206

        def __init__(self, body: bytes) -> None:
            super().__init__(body)
            self.headers = {
                "Content-Type": "audio/mpeg", "Content-Range": "bytes 0-0/123",
                "Content-Length": "1",
            }

    class FakeOpener:
        def open(self, request, timeout):
            assert request.headers["Range"] == "bytes=0-0"
            return FakeAudio(b"x")

    monkeypatch.setattr("rhythm_metadata_api.api.v2.music_routes._opener", FakeOpener())
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
        monkeypatch.setattr(service, "audio_url", lambda *args: "http://car-er.kuwo.cn/test.mp3")
        response = client.get(
            "/v2/music/sources/feiniao/songs/42/stream",
            headers={**AUTH, "Range": "bytes=0-0"},
        )
        assert response.status_code == 206
        assert response.headers["Content-Range"] == "bytes 0-0/123"
        assert response.content == b"x"


def test_audio_url_is_reused_for_seek_and_refreshed_after_rejection(tmp_path: Path, monkeypatch) -> None:
    class CountingAdapter(FeiniaoAdapter):
        def __init__(self):
            super().__init__(fetch=fake_fetch)
            self.calls = 0

        def audio_url(self, base_url, song_id, quality):
            self.calls += 1
            return f"https://car-er.kuwo.cn/song-{self.calls}.mp3"

    adapter = CountingAdapter()
    monkeypatch.setitem(ADAPTERS, "feiniao", adapter)

    class FakeAudio(io.BytesIO):
        status = 206

        def __init__(self):
            super().__init__(b"x")
            self.headers = {
                "Content-Type": "audio/mpeg", "Content-Range": "bytes 100-100/123",
                "Content-Length": "1",
            }

    class FakeOpener:
        def open(self, request, timeout):
            if request.full_url.endswith("song-1.mp3"):
                from urllib.error import HTTPError
                raise HTTPError(request.full_url, 403, "expired", {}, None)
            return FakeAudio()

    monkeypatch.setattr("rhythm_metadata_api.api.v2.music_routes._opener", FakeOpener())
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
        assert service.audio_url("feiniao", "42", "mp3") == service.audio_url("feiniao", "42", "mp3")
        assert adapter.calls == 1
        response = client.get(
            "/v2/music/sources/feiniao/songs/42/stream",
            headers={**AUTH, "Range": "bytes=100-100"},
        )
        assert response.status_code == 206 and response.content == b"x"
        assert adapter.calls == 2
        assert service.audio_url("feiniao", "42", "mp3").endswith("song-2.mp3")


def test_artwork_is_relayed_only_from_known_image_hosts(tmp_path: Path, monkeypatch) -> None:
    class FakeImage(io.BytesIO):
        def __init__(self, body: bytes) -> None:
            super().__init__(body)
            self.headers = {"Content-Type": "image/jpeg"}

    class FakeOpener:
        def open(self, request, timeout):
            assert request.full_url.startswith("https://img1.kuwo.cn/")
            return FakeImage(b"fake-image")

    monkeypatch.setattr("rhythm_metadata_api.api.v2.music_routes._opener", FakeOpener())
    monkeypatch.setattr("rhythm_metadata_api.api.v2.music_routes.validate_public_url", lambda url, **kwargs: url)
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.get("/v2/music/artwork?url=https%3A%2F%2Fimg1.kuwo.cn%2Fpic.jpg", headers=AUTH)
        assert response.status_code == 200
        assert response.content == b"fake-image"
