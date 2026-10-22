from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rhythm_metadata_api.application import library_search
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.main import create_app

TOKEN = "test-private-catalog-token"
AUTH = {"Authorization": f"Bearer {TOKEN}", "X-Device-ID": "android-test-device"}


@pytest.fixture
def env(tmp_path: Path):
    db = tmp_path / "catalog.sqlite3"
    settings = Settings(
        bootstrap_token=TOKEN,
        database_path=":memory:",
        v2_database_path=str(db),
        local_object_root=str(tmp_path / "objects"),
    )
    with TestClient(create_app(settings)) as client:
        yield client, db


def create_work(client: TestClient, key: str, body: dict[str, object]) -> str:
    response = client.post("/v2/works", headers={**AUTH, "Idempotency-Key": key}, json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def search(client: TestClient, q: str, kind: str) -> dict:
    response = client.get("/v2/library/search", headers=AUTH, params={"q": q, "type": kind})
    assert response.status_code == 200, response.text
    return response.json()


def upload_musicxml(client: TestClient, key: str) -> str:
    content = b"<score-partwise version='4.0'><part-list/></score-partwise>"
    created = client.post(
        "/v2/uploads",
        headers={**AUTH, "Idempotency-Key": f"{key}-create"},
        json={
            "sha256": hashlib.sha256(content).hexdigest(),
            "byte_size": len(content),
            "media_type": "application/vnd.recordare.musicxml+xml",
            "original_filename": f"{key}.musicxml",
            "source": "test",
        },
    )
    assert created.status_code == 201, created.text
    upload_id = created.json()["upload"]["id"]
    written = client.put(
        f"/v2/uploads/{upload_id}/content",
        headers={**AUTH, "Content-Type": "application/octet-stream"},
        content=content,
    )
    assert written.status_code == 200, written.text
    completed = client.post(
        f"/v2/uploads/{upload_id}/complete",
        headers={**AUTH, "Idempotency-Key": f"{key}-complete"},
        json={},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["asset"]["id"]


def test_lyrics_number_and_fallback_search(env) -> None:
    client, db = env
    steadfast = create_work(
        client,
        "w1",
        {
            "canonical_title": "上主坚定的爱",
            "language": "zh-Hans",
            "lyrics": "上主坚定的爱永不止息，\n怜悯的心永无穷尽",
            "lyrics_translations": [{"language": "en", "lyrics": "The steadfast love of the Lord"}],
        },
    )
    simplified_only = create_work(
        client,
        "w2",
        {"canonical_title": "我心灵得安宁", "language": "zh-Hans", "lyrics": "当平安如江河临到我心"},
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "update v2_works set number_meta=? where id=?",
            (json.dumps([{"heavenSong_num": 352}, {"ihope_num": 256}]), simplified_only),
        )

    by_title = search(client, "上主", "lyrics")
    assert [item["work_id"] for item in by_title["items"]] == [steadfast]
    assert by_title["items"][0]["match_field"] == "title"
    assert by_title["fallback"] is None

    across_punctuation = search(client, "止息怜悯", "lyrics")
    assert across_punctuation["items"][0]["match_field"] == "lyrics"
    assert "止息" in across_punctuation["items"][0]["snippet"]

    english = search(client, "STEADFAST love", "lyrics")
    assert english["items"][0]["match_language"] == "en"

    traditional = search(client, "平安如江河臨到", "lyrics")
    assert [item["work_id"] for item in traditional["items"]] == [simplified_only]
    assert traditional["fallback"] == "opencc_t2s"

    number = search(client, "256", "number")
    item = number["items"][0]
    assert item["work_id"] == simplified_only
    assert item["matched_number"] == {"key": "ihope_num", "value": 256}
    assert [n["key"] for n in item["numbers"]] == ["ihope_num", "heavenSong_num"]
    assert item["default_score_id"] is None and item["songs"] == []
    assert search(client, "abc", "number")["items"] == []

    bundle = client.get(f"/v2/works/{simplified_only}/bundle", headers=AUTH).json()
    assert bundle["work"]["number_meta"] == [
        {"key": "ihope_num", "value": 256},
        {"key": "heavenSong_num", "value": 352},
    ]


def test_lyrics_search_matches_associated_song_title(env) -> None:
    client, _db = env
    work_id = create_work(
        client,
        "song-title-work",
        {
            "canonical_title": "詩篇六十三",
            "language": "zh-Hant",
            "lyrics": "我的心切慕你，詩篇六十三的歌詞",
        },
    )
    arrangement = client.post(
        f"/v2/works/{work_id}/arrangements",
        headers={**AUTH, "Idempotency-Key": "song-title-arrangement"},
        json={"name": "SATB"},
    )
    assert arrangement.status_code == 201, arrangement.text
    rendition = client.post(
        f"/v2/arrangements/{arrangement.json()['id']}/renditions",
        headers={**AUTH, "Idempotency-Key": "song-title-rendition"},
        json={"label": "诗篇63", "kind": "performance"},
    )
    assert rendition.status_code == 201, rendition.text

    result = search(client, "诗篇63", "lyrics")
    assert [item["work_id"] for item in result["items"]] == [work_id]
    assert result["items"][0]["match_field"] == "title"
    assert result["items"][0]["has_lyrics"] is True


def test_lyrics_search_matches_title_translations_and_shows_matched_alias(env) -> None:
    client, _db = env
    work_id = create_work(
        client,
        "title-translations-work",
        {
            "canonical_title": "祂的慈爱永远长存",
            "language": "zh-Hans",
            "title_translations": [
                {"language": "en", "title": "His Love Endures Forever"},
                {"language": "zh-Hant", "title": "祂的慈愛永遠長存"},
            ],
        },
    )
    other_work = create_work(
        client,
        "title-translations-other",
        {"canonical_title": "完全不相关的标题", "language": "zh-Hans"},
    )

    by_canonical = search(client, "祂的慈爱永远长存", "lyrics")
    assert [item["work_id"] for item in by_canonical["items"]] == [work_id]
    assert by_canonical["items"][0]["title"] == "祂的慈爱永远长存"
    assert by_canonical["items"][0]["match_field"] == "title"
    assert by_canonical["items"][0]["match_language"] is None

    by_english_alias = search(client, "His Love Endures", "lyrics")
    assert [item["work_id"] for item in by_english_alias["items"]] == [work_id]
    assert by_english_alias["items"][0]["title"] == "His Love Endures Forever"
    assert by_english_alias["items"][0]["match_field"] == "title"
    assert by_english_alias["items"][0]["match_language"] == "en"

    by_traditional_alias = search(client, "祂的慈愛永遠長存", "lyrics")
    assert [item["work_id"] for item in by_traditional_alias["items"]] == [work_id]
    assert by_traditional_alias["items"][0]["title"] == "祂的慈愛永遠長存"
    assert by_traditional_alias["items"][0]["match_language"] == "zh-Hant"

    assert other_work not in [item["work_id"] for item in by_english_alias["items"]]


def test_score_search_matches_song_and_score_fields_and_excludes_no_score_work(env) -> None:
    client, _db = env
    work_id = create_work(
        client,
        "score-search-work",
        {"canonical_title": "詩篇六十三", "language": "zh-Hant"},
    )
    no_score_work_id = create_work(
        client,
        "score-search-no-score-work",
        {"canonical_title": "诗篇无谱", "language": "zh-Hans"},
    )
    arrangement = client.post(
        f"/v2/works/{work_id}/arrangements",
        headers={**AUTH, "Idempotency-Key": "score-search-arrangement"},
        json={"name": "SATB"},
    )
    assert arrangement.status_code == 201, arrangement.text
    rendition = client.post(
        f"/v2/arrangements/{arrangement.json()['id']}/renditions",
        headers={**AUTH, "Idempotency-Key": "score-search-rendition"},
        json={"label": "诗篇63", "kind": "performance"},
    )
    assert rendition.status_code == 201, rendition.text
    asset_id = upload_musicxml(client, "score-search-musicxml")
    score = client.post(
        f"/v2/arrangements/{arrangement.json()['id']}/scores-with-revision",
        headers={**AUTH, "Idempotency-Key": "score-search-score"},
        json={
            "label": "精校谱",
            "origin": "external_import",
            "assets": [{"asset_id": asset_id, "role": "primary_musicxml"}],
            "publish": True,
            "preferred": True,
        },
    )
    assert score.status_code == 201, score.text
    score_id = score.json()["score"]["id"]

    by_song_title = search(client, "诗篇", "scores")
    assert [item["work_id"] for item in by_song_title["items"]] == [work_id]
    assert by_song_title["items"][0]["match_field"] == "title"
    assert by_song_title["items"][0]["default_score_id"] == score_id
    assert search(client, "精校谱", "scores")["items"][0]["match_field"] == "score"
    assert search(client, "SATB", "scores")["items"][0]["match_field"] == "score"

    converted = search(client, "诗篇六十三", "scores")
    assert [item["work_id"] for item in converted["items"]] == [work_id]
    assert converted["fallback"] == "opencc_s2t"
    assert search(client, "无谱", "scores")["items"] == []
    assert no_score_work_id not in {item["work_id"] for item in by_song_title["items"]}


def test_normalize_handles_compatibility_and_case() -> None:
    assert library_search.normalize("⾄ 水, Lord!") == "至水lord"
    assert library_search.parse_number_query(" 1 05 ") == 105
    assert library_search.number_items([{"x_num": "7"}, {"ihope_num": 1}]) == [
        {"key": "ihope_num", "value": 1},
        {"key": "x_num", "value": "7"},
    ]
