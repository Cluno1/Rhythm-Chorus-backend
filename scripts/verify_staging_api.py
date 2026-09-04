"""Verify an isolated issue 12 API, including real COS byte-range delivery."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from typing import Any
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--musicxml-asset-id", required=True)
    parser.add_argument("--rendition-id", required=True)
    args = parser.parse_args()
    token = os.environ["RHYTHM_BOOTSTRAP_TOKEN"]
    base_url = args.base_url.rstrip("/")

    health = get_json(f"{base_url}/healthz")
    albums = get_json(f"{base_url}/v2/library/albums", token)
    if len(albums["items"]) != 1:
        raise SystemExit("expected exactly one album")
    album = albums["items"][0]
    if album["key"] != "ihope" or album["song_count"] != 73:
        raise SystemExit("ihope album invariant failed")
    uuid.UUID(album["id"])
    detail = get_json(f"{base_url}/v2/library/albums/{album['id']}", token)
    if len(detail["songs"]) != 73:
        raise SystemExit("album detail must contain 73 songs")

    songs: list[dict[str, Any]] = []
    cursor = None
    while True:
        suffix = "?limit=50" + (f"&cursor={cursor}" if cursor else "")
        page = get_json(f"{base_url}/v2/library/songs{suffix}", token)
        songs.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    if len(songs) != 73 or any("(未标注)" in item["title"] for item in songs):
        raise SystemExit("songs projection invariant failed")
    for song in songs:
        for field in ("work_id", "arrangement_id", "rendition_id", "album_id"):
            uuid.UUID(song[field])

    musicxml = get_json(
        f"{base_url}/v2/assets/{args.musicxml_asset_id}/delivery", token
    )
    playback = get_json(
        f"{base_url}/v2/renditions/{args.rendition_id}/playback", token
    )
    for descriptor in (musicxml, playback):
        if descriptor["delivery"] != "signed_url" or not descriptor["expires_at"]:
            raise SystemExit("expected expiring signed COS delivery")
        range_request = Request(descriptor["url"], headers={"Range": "bytes=0-31"})
        with urlopen(range_request, timeout=20) as response:
            body = response.read()
            if response.status != 206 or len(body) != 32 or not response.headers.get(
                "Content-Range"
            ):
                raise SystemExit("signed COS byte range failed")

    print(
        json.dumps(
            {
                "health": health.get("status", "ok"),
                "albums": 1,
                "songs": len(songs),
                "album_detail_songs": len(detail["songs"]),
                "artists": sum(bool(item["artist"]) for item in songs),
                "lyrics": sum(bool(item["lyrics"]) for item in songs),
                "musicxml_delivery": "signed_url-range-206",
                "mp3_delivery": "signed_url-range-206",
            },
            ensure_ascii=False,
        )
    )


def get_json(url: str, token: str | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urlopen(Request(url, headers=headers), timeout=20) as response:
        return json.loads(response.read())


if __name__ == "__main__":
    main()
