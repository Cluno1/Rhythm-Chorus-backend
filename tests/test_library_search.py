from __future__ import annotations

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


def test_normalize_handles_compatibility_and_case() -> None:
    assert library_search.normalize("⾄ 水, Lord!") == "至水lord"
    assert library_search.parse_number_query(" 1 05 ") == 105
    assert library_search.number_items([{"x_num": "7"}, {"ihope_num": 1}]) == [
        {"key": "ihope_num", "value": 1},
        {"key": "x_num", "value": "7"},
    ]
