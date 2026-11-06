"""issue 166: lyrics adopted for local songs are shared by title, artist and duration."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from rhythm_metadata_api.application.device_auth import hash_admin_password
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.main import create_app

TOKEN = "issue166-test-bootstrap-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PATH = "/v2/music/device-lyrics"


def client_for(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(
        bootstrap_token=TOKEN,
        database_path=":memory:",
        v2_database_path=str(tmp_path / "catalog.sqlite3"),
        local_object_root=str(tmp_path / "objects"),
        public_token_secret="x" * 48,
        public_admin_password_hash=hash_admin_password("correct horse battery staple"),
    )))


def save(client: TestClient, lyrics: str, mode: str = "auto", **song):
    body = {"title": "Love Story", "artist": "Taylor Swift", "duration_ms": 235000,
            "lyrics_lrc": lyrics, "mode": mode, "provider": "LRCLIB", **song}
    return client.put(PATH, headers=AUTH, json=body)


def row_count(client: TestClient) -> int:
    with client.app.state.v2_container.device_song_lyrics.sessions() as session:
        return session.execute(
            __import__("sqlalchemy").text("SELECT count(*) FROM v2_device_song_lyrics")
        ).scalar_one()


def test_按规范化歌名歌手和三秒内时长认定同一首歌(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert save(client, "[00:01.00]We were both young").json()["written"] is True
        hit = client.get(PATH, headers=AUTH, params={
            "title": " love  story", "artist": "TAYLOR SWIFT", "duration_ms": 237900,
        })
        assert hit.status_code == 200, hit.text
        assert hit.json()["lyrics_lrc"] == "[00:01.00]We were both young"
        assert client.get(PATH, headers=AUTH, params={
            "title": "Love Story", "artist": "Taylor Swift", "duration_ms": 238500,
        }).status_code == 404
        assert client.get(PATH, headers=AUTH, params={
            "title": "Love Story", "artist": "Another Artist", "duration_ms": 235000,
        }).status_code == 404


def test_自动写入不改动已有记录(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        save(client, "[00:01.00]第一次")
        second = save(client, "[00:01.00]第二次").json()
        assert second["written"] is False
        assert second["lyrics"]["lyrics_lrc"] == "[00:01.00]第一次"
        assert row_count(client) == 1


def test_手动选择覆盖他人记录且不新增行(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        save(client, "[00:01.00]自动")
        save(client, "[00:01.00]甲手选", "manual")
        latest = save(client, "[00:01.00]乙手选", "manual", provider="CHINESE_LYRICS").json()
        assert latest["written"] is True
        assert latest["lyrics"]["lyrics_lrc"] == "[00:01.00]乙手选"
        assert latest["lyrics"]["mode"] == "manual" and latest["lyrics"]["provider"] == "CHINESE_LYRICS"
        assert row_count(client) == 1


def test_时长差太多是另一首歌_各自保存(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        save(client, "[00:01.00]录音室版")
        assert save(client, "[00:01.00]现场版", duration_ms=420000).json()["written"] is True
        assert row_count(client) == 2


def test_没有可识别歌手时拒绝保存(tmp_path: Path) -> None:
    with client_for(tmp_path) as client:
        assert save(client, "[00:01.00]x", artist=" - ").status_code == 422
        assert row_count(client) == 0
