"""Issue 140: source configuration and the client contract survive upstream shape changes."""

from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from rhythm_metadata_api.application import music_sources
from rhythm_metadata_api.application.device_auth import hash_admin_password
from rhythm_metadata_api.application.music_adapters import (
    _AUDIO_HOSTS,
    ADAPTERS,
    FEINIAO_LYRIC_FILTERS,
    FeiniaoAdapter,
    clean_lyrics,
    validate_public_url,
)
from rhythm_metadata_api.application.music_sources import (
    LyricFilters,
    MusicSongMetadataWrite,
    MusicSourceConfig,
    MusicSourceCreate,
    MusicSourcePatch,
)
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.domain.v2.errors import V2Conflict, V2DomainError, V2NotFound
from rhythm_metadata_api.main import create_app
from rhythm_metadata_api.public_main import _public_read_allowed, create_public_app

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
    cleaned = clean_lyrics(original, FEINIAO_LYRIC_FILTERS)
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


def test_source_rules_are_editable_and_isolated(tmp_path: Path, monkeypatch) -> None:
    requested_sources = []

    def fetch(url: str) -> dict:
        requested_sources.append(parse_qs(urlsplit(url).query)["source"][0])
        return {"data": [{"type": "mp3", "url": "https://audio.example.org/song.mp3"}]}

    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fetch))
    monkeypatch.setattr(
        "rhythm_metadata_api.application.music_adapters.socket.getaddrinfo",
        lambda *args: [(None, None, None, None, ("1.1.1.1", 443))],
    )
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
        with pytest.raises(V2DomainError):
            service.audio_url("feiniao", "42", "mp3")
        configured = service.patch("feiniao", MusicSourcePatch(revision=2, config=MusicSourceConfig(
            audio_hosts=["audio.example.org"], artwork_hosts=["images.example.org"],
            upstream_source="备用音源", audio_url_ttl_seconds=0,
        )))
        assert configured["config"]["audio_hosts"] == ["audio.example.org"]
        assert service.audio_url("feiniao", "42", "mp3") == "https://audio.example.org/song.mp3"
        assert requested_sources == ["酷我音乐", "备用音源"]
        assert service.artwork_hosts("feiniao") == frozenset({"images.example.org"})
        assert service.list_sources()["items"][0]["config"] == configured["config"]
        art_path = service._adapter(service._active("feiniao"))._artwork(
            "https://images.example.org/cover.jpg", "feiniao",
        )
        assert art_path and "source=feiniao" in art_path

        class FakeImage(io.BytesIO):
            def __init__(self):
                super().__init__(b"image")
                self.headers = {"Content-Type": "image/jpeg"}

        class FakeOpener:
            def open(self, request, timeout):
                assert request.full_url == "https://images.example.org/cover.jpg"
                return FakeImage()

        monkeypatch.setattr("rhythm_metadata_api.api.v2.music_routes._opener", FakeOpener())
        assert client.get(art_path, headers=AUTH).status_code == 200
        assert client.get(art_path.replace("&source=feiniao", ""), headers=AUTH).status_code == 200
        service.patch("feiniao", MusicSourcePatch(revision=3, config=MusicSourceConfig(
            audio_hosts=["audio.example.org"], artwork_hosts=["img1.kuwo.cn"],
            upstream_source="备用音源",
        )))
        assert client.get(art_path, headers=AUTH).status_code == 422
    for bad_host in ("localhost", "127.0.0.1", "*.example.org", "example.org:443", "https://example.org"):
        with pytest.raises(ValueError):
            MusicSourceConfig(audio_hosts=[bad_host])


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


def test_full_width_watermark_variant_matches_one_rule() -> None:
    cleaned = clean_lyrics("[00:00.000]官网：飞鸟.site\n[00:01.000]官网：飞鸟.site 之歌", FEINIAO_LYRIC_FILTERS)
    assert cleaned == "[00:01.000]官网：飞鸟.site 之歌"


def test_by_rule_never_touches_title_or_artist_tags() -> None:
    lrc = "[ti:飞鸟·轻音]\n[ar:飞鸟·轻音]\n[by:飞鸟·轻音v4]"
    assert clean_lyrics(lrc, FEINIAO_LYRIC_FILTERS) == "[ti:飞鸟·轻音]\n[ar:飞鸟·轻音]"


def test_rule_text_is_literal_not_a_pattern() -> None:
    assert clean_lyrics("[00:01.00]简单点\n[00:02.00].*", {"exact_lines": [".*"]}) == "[00:01.00]简单点"


def _enabled_client(tmp_path: Path, monkeypatch, fetch=fake_fetch):
    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fetch))
    client = TestClient(create_app(settings(tmp_path)))
    client.__enter__()
    service = client.app.state.v2_container.music_sources
    service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
    return client, service


def test_new_rule_applies_to_stored_and_upstream_lyrics_without_a_release(tmp_path, monkeypatch) -> None:
    client, service = _enabled_client(tmp_path, monkeypatch)
    with client:
        metadata = "/v2/music/sources/feiniao/songs/42/metadata"
        client.put(metadata, headers=AUTH, json={
            "title": "歌", "artists": ["人"], "lyrics_lrc": "[00:01.00]新水印\n[00:02.00]正文", "mode": "auto",
        })
        assert "新水印" in client.get(metadata, headers=AUTH).json()["lyrics_lrc"]
        service.patch("feiniao", MusicSourcePatch(revision=2, config=MusicSourceConfig(
            lyric_filters=LyricFilters(exact_lines=["新水印", "旅人"]),
        )))
        assert client.get(metadata, headers=AUTH).json()["lyrics_lrc"] == "[00:02.00]正文"
        upstream = client.get("/v2/music/sources/feiniao/songs/43/lyrics", headers=AUTH).json()
        assert upstream["lrc"] == ""


def test_manual_lyrics_are_never_filtered(tmp_path, monkeypatch) -> None:
    client, _ = _enabled_client(tmp_path, monkeypatch)
    with client:
        text = "[00:00.000]官网:飞鸟.site\n[00:01.00]人工校对"
        saved = client.put("/v2/music/sources/feiniao/songs/42/metadata", headers=AUTH, json={
            "title": "歌", "artists": ["人"], "lyrics_lrc": text, "mode": "manual",
        }).json()
        assert saved["lyrics_lrc"] == text
        assert client.get(
            "/v2/music/sources/feiniao/songs/42/lyrics", headers=AUTH
        ).json()["lrc"] == text


def test_config_patch_without_new_fields_keeps_existing_rules(tmp_path, monkeypatch) -> None:
    client, service = _enabled_client(tmp_path, monkeypatch)
    with client:
        service.patch("feiniao", MusicSourcePatch(revision=2, config=MusicSourceConfig(
            audio_hosts=["audio.example.org"], artwork_hosts=["img1.kuwo.cn"], upstream_source="酷我音乐",
            lyric_filters=LyricFilters(exact_lines=["自定义规则"]), audio_quality_order=["m4a"],
        )))
        # An older Web build strips fields it does not declare before calling the API.
        response = service.patch("feiniao", MusicSourcePatch.model_validate({"revision": 3, "config": {
            "audio_hosts": ["other.example.org"], "artwork_hosts": ["img1.kuwo.cn"],
            "upstream_source": "酷我音乐", "audio_url_ttl_seconds": 150,
        }}))
    assert response["config"]["audio_hosts"] == ["other.example.org"]
    assert response["config"]["lyric_filters"]["exact_lines"] == ["自定义规则"]
    assert response["config"]["audio_quality_order"] == ["m4a"]


def test_lyric_rule_limits_are_enforced() -> None:
    for bad in (["x"], ["  "], ["x" * 201], ["ab"] * 51):
        with pytest.raises(ValueError):
            LyricFilters(exact_lines=bad)
    assert LyricFilters(exact_lines=[" 水印 ", "水印"]).exact_lines == ["水印"]


def test_new_source_gets_adapter_rules_and_migrated_source_keeps_them(tmp_path, monkeypatch) -> None:
    client, service = _enabled_client(tmp_path, monkeypatch)
    with client:
        migrated = service.list_sources()["items"][0]["config"]
        assert migrated["lyric_filters"] == FEINIAO_LYRIC_FILTERS
        assert migrated["audio_quality_order"] == ["mp3", "m4a", "ogg"]
        created = service.create(MusicSourceCreate(
            id="backup", name="备用", adapter="feiniao", base_url="https://backup.example.org/",
        ))
        assert created["config"]["lyric_filters"] == FEINIAO_LYRIC_FILTERS
        assert service.adapters()["items"][0]["default_config"]["lyric_filters"] == FEINIAO_LYRIC_FILTERS


def test_lyric_filter_preview_reports_hits_and_writes_nothing(tmp_path, monkeypatch) -> None:
    client, service = _enabled_client(tmp_path, monkeypatch)
    with client:
        path = "/v2/music/sources/feiniao/songs/{}/metadata"
        body = {"title": "歌", "artists": ["人"], "lyrics_lrc": "[00:01.00]候选水印\n[00:02.00]正文"}
        client.put(path.format(42), headers=AUTH, json={**body, "mode": "auto"})
        client.put(path.format(43), headers=AUTH, json={**body, "mode": "manual"})
        before = client.get(path.format(42), headers=AUTH).json()
        result = service.preview_lyric_filters("feiniao", LyricFilters(exact_lines=["候选水印"]))
        assert result["scanned_songs"] == 1
        assert result["affected_songs"] == 1
        assert result["removed_lines"] == 1
        assert result["samples"] == [{"song_id": "42", "title": "歌", "line": "[00:01.00]候选水印"}]
        assert client.get(path.format(42), headers=AUTH).json() == before


def _formats(*types: str, host: str = "car-er.kuwo.cn"):
    calls = []

    def fetch(url: str) -> dict:
        calls.append(url)
        return {"data": [{"type": kind, "url": f"http://{host}/s.{kind}"} for kind in types]}

    fetch.calls = calls
    return fetch


@pytest.fixture
def public_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        "rhythm_metadata_api.application.music_adapters.socket.getaddrinfo",
        lambda *args: [(None, None, None, None, ("1.1.1.1", 80))],
    )


def test_auto_quality_prefers_mp3_so_existing_caches_stay_valid(tmp_path, monkeypatch, public_dns) -> None:
    client, service = _enabled_client(tmp_path, monkeypatch, _formats("flac", "m4a", "mp3"))
    with client:
        assert service.audio_url("feiniao", "42", "auto").endswith(".mp3")


def test_auto_quality_falls_back_in_configured_order_and_skips_flac(tmp_path, monkeypatch, public_dns) -> None:
    client, service = _enabled_client(tmp_path, monkeypatch, _formats("flac", "ogg", "m4a"))
    with client:
        assert service.audio_url("feiniao", "42", "auto").endswith(".m4a")
        service.patch("feiniao", MusicSourcePatch(revision=2, config=MusicSourceConfig(
            audio_quality_order=["flac", "mp3"],
        )))
        assert service.audio_url("feiniao", "42", "auto").endswith(".flac")


def test_auto_quality_without_allowed_format_is_not_found(tmp_path, monkeypatch, public_dns) -> None:
    client, _ = _enabled_client(tmp_path, monkeypatch, _formats("flac"))
    with client:
        response = client.get(
            "/v2/music/sources/feiniao/songs/42/stream?quality=auto", headers=AUTH,
        )
    assert response.status_code == 404


def test_explicit_mp3_never_falls_back(tmp_path, monkeypatch, public_dns) -> None:
    client, service = _enabled_client(tmp_path, monkeypatch, _formats("m4a"))
    with client, pytest.raises(V2NotFound):
        service.audio_url("feiniao", "42", "mp3")


def test_rejected_host_on_first_choice_does_not_fall_through(tmp_path, monkeypatch, public_dns) -> None:
    def fetch(url: str) -> dict:
        return {"data": [
            {"type": "mp3", "url": "http://car-new.kuwo.cn/s.mp3"},
            {"type": "m4a", "url": "http://car-er.kuwo.cn/s.m4a"},
        ]}

    client, service = _enabled_client(tmp_path, monkeypatch, fetch)
    with client, pytest.raises(V2DomainError):
        service.audio_url("feiniao", "42", "auto")


def test_auto_quality_resolves_upstream_exactly_once_while_cached(tmp_path, monkeypatch, public_dns) -> None:
    fetch = _formats("mp3")
    client, service = _enabled_client(tmp_path, monkeypatch, fetch)
    with client:
        service.audio_url("feiniao", "42", "auto")
        service.audio_url("feiniao", "42", "auto")
    assert len(fetch.calls) == 1


def test_unknown_quality_is_rejected(tmp_path, monkeypatch) -> None:
    client, service = _enabled_client(tmp_path, monkeypatch)
    with client, pytest.raises(V2DomainError):
        service.audio_url("feiniao", "42", "best")


def test_sparse_metadata_does_not_blank_upstream_search_fields(tmp_path: Path, monkeypatch) -> None:
    def fetch(url: str) -> dict:
        return {"code": 0, "data": {"songs": [{
            "rid": 42, "name": "上游歌", "artist": "歌手", "album": "上游专辑",
            "duration": 240, "pic": "https://img1.kuwo.cn/c.jpg",
        }], "hasMore": False}}

    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fetch))
    with TestClient(create_app(settings(tmp_path))) as client:
        client.app.state.v2_container.music_sources.patch(
            "feiniao", MusicSourcePatch(revision=1, enabled=True)
        )
        client.put("/v2/music/sources/feiniao/songs/42/metadata", headers=AUTH, json={
            "title": "存储歌名", "artists": ["歌手"], "mode": "auto",
        })
        item = client.get("/v2/music/search?q=x", headers=AUTH).json()["sources"][0]["items"][0]
    assert item["title"] == "存储歌名"
    assert item["album"] == "上游专辑"
    assert item["duration_ms"] == 240000
    assert "img1.kuwo.cn" in item["artwork_url"]


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
            self.calls = []

        def audio_url(self, base_url, song_id, quality):
            self.calls.append(song_id)
            return f"https://car-er.kuwo.cn/song-{len(self.calls)}.mp3"

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
        assert len(adapter.calls) == 1
        response = client.get(
            "/v2/music/sources/feiniao/songs/42/stream",
            headers={**AUTH, "Range": "bytes=100-100"},
        )
        assert response.status_code == 206 and response.content == b"x"
        assert len(adapter.calls) == 2
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
        client.app.state.v2_container.music_sources.patch(
            "feiniao", MusicSourcePatch(revision=1, enabled=True),
        )
        response = client.get("/v2/music/artwork?url=https%3A%2F%2Fimg1.kuwo.cn%2Fpic.jpg", headers=AUTH)
        assert response.status_code == 200
        assert response.content == b"fake-image"


def recommended_client(tmp_path: Path, monkeypatch, playlists: list[dict]):
    calls: list[int] = []
    state = {"fail": False}

    def fetch(url: str) -> dict:
        params = parse_qs(urlsplit(url).query)
        assert params["api"][0] == "playlist_home"
        calls.append(int(params["pn"][0]))
        if state["fail"]:
            raise V2DomainError("upstream failed")
        data = playlists if calls[-1] == 1 else []
        return {"code": 200, "data": data, "pagination": {"total_pages": 10}}

    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fetch))
    client = TestClient(create_app(settings(tmp_path)))
    client.__enter__()
    client.app.state.v2_container.music_sources.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
    return client, calls, state


def recommended_names(client: TestClient, limit: int = 50) -> set[str]:
    response = client.get(f"/v2/music/recommended-albums?limit={limit}", headers=AUTH)
    assert response.status_code == 200
    return {item["name"] for item in response.json()["items"]}


def playlist_row(pid: str, name: str = "歌单", songs: int = 50, img: str | None = "https://img1.kuwo.cn/a.jpg") -> dict:
    return {"id": pid, "name": name, "total_songs": songs, "img": img}


def test_推荐歌单只返回有名称有封面且歌曲数在范围内的(tmp_path, monkeypatch) -> None:
    client, _, _ = recommended_client(tmp_path, monkeypatch, [
        playlist_row("1", "合适"), playlist_row("2", "  "), playlist_row("3", "无封面", img=None),
        playlist_row("4", "太少", songs=9), playlist_row("5", "太多", songs=201),
        playlist_row("6", "下限", songs=10), playlist_row("7", "上限", songs=200),
    ])
    with client:
        assert recommended_names(client) == {"合适", "下限", "上限"}


def test_推荐歌单数量不超过_limit(tmp_path, monkeypatch) -> None:
    client, _, _ = recommended_client(
        tmp_path, monkeypatch, [playlist_row(str(i), f"歌单{i}") for i in range(1, 11)],
    )
    with client:
        assert len(recommended_names(client, limit=3)) == 3


def test_候选池有效期内恰好拉一轮上游(tmp_path, monkeypatch) -> None:
    client, calls, _ = recommended_client(tmp_path, monkeypatch, [playlist_row("1")])
    with client:
        recommended_names(client)
        recommended_names(client)
        assert len(calls) == 3
        assert calls[0] == 1


def test_刷新失败时继续返回旧候选池且五分钟内不重试(tmp_path, monkeypatch) -> None:
    now = {"value": 1000.0}
    monkeypatch.setattr(music_sources, "monotonic", lambda: now["value"])
    client, calls, state = recommended_client(tmp_path, monkeypatch, [playlist_row("1", "旧歌单")])
    with client:
        assert recommended_names(client) == {"旧歌单"}
        state["fail"] = True
        now["value"] += 3601
        assert recommended_names(client) == {"旧歌单"}
        assert len(calls) == 6
        now["value"] += 299
        assert recommended_names(client) == {"旧歌单"}
        assert len(calls) == 6


def test_没有缓存且上游失败时返回空列表(tmp_path, monkeypatch) -> None:
    client, _, state = recommended_client(tmp_path, monkeypatch, [playlist_row("1")])
    state["fail"] = True
    with client:
        assert recommended_names(client) == set()


def test_修改歌曲数范围立即生效且不重新拉上游(tmp_path, monkeypatch) -> None:
    client, calls, _ = recommended_client(tmp_path, monkeypatch, [playlist_row("1", "五十首", songs=50)])
    with client:
        assert recommended_names(client) == {"五十首"}
        service = client.app.state.v2_container.music_sources
        service.patch("feiniao", MusicSourcePatch(
            revision=2, config=MusicSourceConfig(recommended_min_songs=60),
        ))
        assert recommended_names(client) == set()
        assert len(calls) == 3


def test_歌曲数范围下限不能大于上限() -> None:
    with pytest.raises(ValueError):
        MusicSourceConfig(recommended_min_songs=20, recommended_max_songs=10)


def test_推荐歌单是公开网关的只读路由() -> None:
    assert _public_read_allowed("GET", "/v2/music/recommended-albums")
    assert not _public_read_allowed("POST", "/v2/music/recommended-albums")
