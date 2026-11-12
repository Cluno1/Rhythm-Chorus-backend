"""Issue 189: capability isolation, all-source search, and immutable playback tickets."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from test_music_sources import AUTH, settings

from rhythm_metadata_api.application.music_adapters import (
    ADAPTERS,
    FeiniaoAdapter,
    MetingSearchAdapter,
    adapter_kinds,
)
from rhythm_metadata_api.application.music_playback import _same_recording
from rhythm_metadata_api.application.music_sources import (
    CapabilityConfig,
    MusicCapabilityPatch,
    MusicSourceConfig,
    MusicSourceCreate,
    MusicSourcePatch,
)
from rhythm_metadata_api.domain.v2.errors import V2DomainError
from rhythm_metadata_api.infrastructure.db import database as database_module
from rhythm_metadata_api.infrastructure.db.models import MusicSource
from rhythm_metadata_api.main import create_app
from rhythm_metadata_api.public_main import create_public_app


@pytest.fixture(autouse=True)
def disable_background_health(monkeypatch):
    from rhythm_metadata_api.application.music_health import MusicHealthScheduler
    monkeypatch.setattr(MusicHealthScheduler, "start", lambda self: None)
    monkeypatch.setattr(MusicHealthScheduler, "stop", lambda self: None)


def test_meting_search_queries_every_enabled_connection_on_the_same_platform(tmp_path, monkeypatch):
    calls = []
    assert next(item for item in adapter_kinds() if item["id"] == "meting_search")["supported_operations"] == ["song_search"]

    def fetch(url):
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        return {"items": [{"id": "123", "title": "安静", "artists": ["周杰伦"],
                           "album": "范特西", "duration_ms": 240000}]}

    monkeypatch.setitem(ADAPTERS, "meting_search", MetingSearchAdapter(fetch=fetch))
    configured = settings(tmp_path).model_copy(update={
        "external_lyrics_provider_url": "http://127.0.0.1:8787",
    })
    with TestClient(create_app(configured)) as client:
        service = client.app.state.v2_container.music_sources
        for source_id, priority in (("meting-a", 20), ("meting-b", 30)):
            service.create(MusicSourceCreate(
                id=source_id, name=source_id, adapter="meting_search",
                base_url="http://127.0.0.1:8787/", priority=priority, enabled=True,
                config=MusicSourceConfig(upstream_source="netease"),
            ))
        response = client.get("/v2/music/search", headers=AUTH, params={"q": "周杰伦 安静", "page": 2})
        assert response.status_code == 200
        assert [group["source_id"] for group in response.json()["sources"]] == ["meting-a", "meting-b"]
        assert [group["items"][0]["id"] for group in response.json()["sources"]] == [
            "meting-a:123", "meting-b:123",
        ]
        assert len(calls) == 2 and all(call["page"] == ["2"] for call in calls)
        assert client.get("/v2/music/sources/meting-a/songs/123/stream", headers=AUTH).status_code == 503


def test_search_deadline_returns_fast_source_and_closed_capability_is_not_called(tmp_path, monkeypatch):
    calls = []

    def fetch(url):
        host = urlsplit(url).hostname
        calls.append(host)
        if host == "slow.example.org":
            time.sleep(1.0)
        return {"code": 0, "data": {"songs": [
            {"rid": 1, "name": "安静", "artist": "周杰伦", "duration": 240},
        ], "hasMore": False}}

    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fetch))
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        service.create(MusicSourceCreate(
            id="slow", name="slow", adapter="feiniao", base_url="https://slow.example.org/",
            priority=10, enabled=True,
            config=MusicSourceConfig(capabilities={"song_search": CapabilityConfig(timeout_ms=500)}),
        ))
        service.create(MusicSourceCreate(
            id="fast", name="fast", adapter="feiniao", base_url="https://fast.example.org/",
            priority=20, enabled=True,
        ))
        start = time.monotonic()
        response = client.get("/v2/music/search?q=周杰伦", headers=AUTH)
        assert time.monotonic() - start < 0.9
        assert [item["source_id"] for item in response.json()["sources"]] == ["fast"]
        assert response.json()["unavailable_sources"] == ["slow"]
        service.patch("fast", MusicSourcePatch(
            revision=1,
            config=MusicSourceConfig(capabilities={"song_search": CapabilityConfig(enabled=False)}),
        ))
        assert client.get("/v2/music/search?q=周杰伦", headers=AUTH).status_code == 503
        assert calls.count("fast.example.org") == 1


class _AudioResponse(io.BytesIO):
    def __init__(self, content: bytes, *, start: int, end: int, etag: str):
        super().__init__(content[start:end + 1])
        self.status = 206
        self.headers = {
            "Content-Type": "audio/mpeg", "Content-Range": f"bytes {start}-{end}/{len(content)}",
            "Content-Length": str(end - start + 1), "ETag": etag,
        }


def test_ticket_pins_bytes_and_rejects_changed_entity(tmp_path, monkeypatch):
    audio = b"x" * 2000
    etag = ['"version-one"']

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://car-er.kuwo.cn/track.mp3"
            raw_range = request.headers.get("Range", "bytes=0-0")
            raw = raw_range.removeprefix("bytes=")
            start_text, end_text = raw.split("-", 1)
            return _AudioResponse(audio, start=int(start_text), end=int(end_text), etag=etag[0])

    def fetch(url):
        return {"code": 0, "data": [{"type": "mp3", "url": "http://car-er.kuwo.cn/track.mp3"}]}

    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fetch))
    monkeypatch.setattr("rhythm_metadata_api.application.music_adapters.socket.getaddrinfo",
                        lambda *args: [(None, None, None, None, ("1.1.1.1", 80))])
    opener = Opener()
    monkeypatch.setattr("rhythm_metadata_api.application.music_playback._opener", opener)
    monkeypatch.setattr("rhythm_metadata_api.api.v2.music_routes._opener", opener)
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        service.patch("feiniao", MusicSourcePatch(revision=1, enabled=True))
        prepared = client.post("/v2/music/playback/prepare", headers=AUTH, json={
            "source_id": "feiniao", "song_id": "42", "quality": "mp3",
        })
        assert prepared.status_code == 200, prepared.text
        descriptor = prepared.json()
        assert descriptor["origin_id"] == "feiniao:42"
        assert descriptor["content_length"] == len(audio)
        stream = client.get(descriptor["stream_url"], headers={**AUTH, "Range": "bytes=500-599"})
        assert stream.status_code == 206 and stream.content == audio[500:600]
        etag[0] = '"version-two"'
        changed = client.get(descriptor["stream_url"], headers={**AUTH, "Range": "bytes=600-699"})
        assert changed.status_code == 409


def test_cross_platform_match_rejects_live_and_short_preview():
    original = {"title": "安静", "artists": ["周杰伦"], "album": "范特西", "duration_ms": 240000}
    assert _same_recording(original, dict(original))
    assert not _same_recording(original, {**original, "title": "安静 Live"})
    assert not _same_recording(original, {**original, "duration_ms": 30000})


def test_archived_origin_keeps_its_id_and_uses_enabled_same_platform_resolver(tmp_path, monkeypatch):
    def fetch(url):
        return {"code": 0, "data": [{"type": "mp3", "url": "http://car-er.kuwo.cn/track.mp3"}]}

    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fetch))
    monkeypatch.setattr("rhythm_metadata_api.application.music_adapters.socket.getaddrinfo",
                        lambda *args: [(None, None, None, None, ("1.1.1.1", 80))])
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        service.create(MusicSourceCreate(
            id="backup", name="backup", adapter="feiniao",
            base_url="https://backup.example.org/", enabled=True,
            service_group_id="feiniao-service",
        ))
        archived = service.set_archived("feiniao", 1, True)
        assert archived["archived_at"] and not archived["enabled"]
        assert service.list_sources(enabled_only=True)["items"][0]["id"] == "backup"
        assert service.audio_url("feiniao", "42", "mp3") == "http://car-er.kuwo.cn/track.mp3"
        restored = service.set_archived("feiniao", 2, False)
        assert restored["id"] == "feiniao" and not restored["enabled"]


def test_capability_switch_isolated_from_search(tmp_path, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(parse_qs(urlsplit(url).query)["api"][0])
        return {"code": 0, "data": {"songs": [{"rid": 42, "name": "安静", "artist": "周杰伦"}]}}

    monkeypatch.setitem(ADAPTERS, "feiniao", FeiniaoAdapter(fetch=fetch))
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        source = service.patch("feiniao", MusicSourcePatch(
            revision=1, enabled=True,
            config=MusicSourceConfig(capabilities={"audio_resolve": CapabilityConfig(enabled=False)}),
        ))
        assert source["effective_capabilities"]["song_search"]["enabled"]
        assert not source["effective_capabilities"]["audio_resolve"]["enabled"]
        assert client.get("/v2/music/search?q=安静", headers=AUTH).status_code == 200
        assert client.post("/v2/music/playback/prepare", headers=AUTH, json={
            "source_id": "feiniao", "song_id": "42",
        }).status_code == 503
        assert calls == ["search_with_source"]


def test_search_and_playback_capabilities_can_be_changed_independently(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        service = client.app.state.v2_container.music_sources
        search = service.patch_capability("feiniao", "song_search", MusicCapabilityPatch(
            revision=1, enabled=False, priority=25, timeout_ms=1500,
        ))
        assert search["effective_capabilities"]["song_search"]["enabled"] is False
        assert search["effective_capabilities"]["song_search"]["priority"] == 25
        assert search["effective_capabilities"]["audio_resolve"]["enabled"] is True
        with service.sessions() as session:
            source = session.get(MusicSource, "feiniao")
            assert source is not None
            source.operation_health = {"song_search": {"state": "healthy", "latency_ms": 88}}
            session.commit()
        playback = service.patch_capability("feiniao", "audio_resolve", MusicCapabilityPatch(
            revision=2, enabled=False, priority=75,
        ))
        assert playback["effective_capabilities"]["audio_resolve"]["enabled"] is False
        assert playback["effective_capabilities"]["audio_resolve"]["priority"] == 75
        assert playback["effective_capabilities"]["song_search"]["priority"] == 25
        assert playback["operation_health"]["song_search"]["state"] == "healthy"
        with pytest.raises(V2DomainError):
            service.patch_capability("feiniao", "not-an-operation", MusicCapabilityPatch(
                revision=3, enabled=True,
            ))


def test_admin_search_and_playback_patch_routes_are_separate(tmp_path):
    with TestClient(create_public_app(settings(tmp_path))) as client:
        login = client.post("/v2/admin/session", json={
            "username": "admin", "password": "correct horse battery staple",
        })
        assert login.status_code == 200
        headers = {"Authorization": f"Bearer {login.json()['accessToken']}"}
        base = "/v2/admin/music-sources/feiniao/capabilities/"
        search = client.patch(base + "song_search", headers=headers, json={
            "revision": 1, "enabled": False, "priority": 10,
        })
        assert search.status_code == 200
        playback = client.patch(base + "audio_resolve", headers=headers, json={
            "revision": 2, "enabled": True, "priority": 20,
        })
        assert playback.status_code == 200
        capabilities = playback.json()["effective_capabilities"]
        assert capabilities["song_search"]["enabled"] is False
        assert capabilities["audio_resolve"]["enabled"] is True
        assert capabilities["song_search"]["priority"] == 10
        assert capabilities["audio_resolve"]["priority"] == 20
        assert client.patch(base + "song_search", headers=headers, json={
            "revision": 1, "enabled": True,
        }).status_code == 409


def test_upgrade_preserves_source_and_backfills_search_health(tmp_path):
    path = tmp_path / "upgrade.sqlite3"
    config = Config()
    config.set_main_option("script_location", str(Path(database_module.__file__).with_name("migrations")))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(config, "issue185health")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE v2_music_sources SET enabled=1, health_failed=1, "
                   "health_checked_at='2026-11-12 10:00:00', health_latency_ms=8200, "
                   "health_error='TimeoutError' WHERE id='feiniao'")
        db.commit()
    command.upgrade(config, "head")
    with sqlite3.connect(path) as db:
        old = db.execute("SELECT id, enabled, operation_health, service_group_id, archived_at "
                         "FROM v2_music_sources WHERE id='feiniao'").fetchone()
        assert old[:2] == ("feiniao", 1)
        assert json.loads(old[2])["song_search"]["state"] == "unhealthy"
        assert old[3:] == (None, None)
        assert db.execute("SELECT cross_platform_playback_enabled FROM v2_music_settings "
                          "WHERE id='global'").fetchone() == (0,)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='v2_music_playback_tickets'").fetchone()
