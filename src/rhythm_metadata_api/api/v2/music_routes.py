"""Uniform read-only music API and administrator-managed source registry."""

from __future__ import annotations

import re
from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse

from rhythm_metadata_api.api.public_auth import AdminActor
from rhythm_metadata_api.api.v2.routes import Actor
from rhythm_metadata_api.application.music_adapters import (
    _ARTWORK_HOSTS,
    _opener,
    validate_public_url,
)
from rhythm_metadata_api.application.music_images import MusicImageArchiveError
from rhythm_metadata_api.application.music_sources import (
    MusicSongMetadataWrite,
    MusicSourceCreate,
    MusicSourcePatch,
    MusicSourceService,
)
from rhythm_metadata_api.domain.v2.errors import V2DomainError, V2Unavailable

router = APIRouter(prefix="/v2/music", tags=["music sources"])
admin_router = APIRouter(prefix="/v2/admin/music-sources", tags=["music source administration"])
_SINGLE_RANGE = re.compile(r"^bytes=\d*-\d*$")


def source_service(request: Request) -> MusicSourceService:
    return request.app.state.v2_container.music_sources


Sources = Annotated[MusicSourceService, Depends(source_service)]
Page = Annotated[int, Query(ge=1, le=1000)]


@admin_router.get("/adapters")
def adapters(service: Sources, _: AdminActor) -> dict:
    return service.adapters()


@admin_router.get("")
def admin_sources(service: Sources, _: AdminActor) -> dict:
    return service.list_sources()


@admin_router.get("/song-metadata")
def admin_song_metadata_list(
    service: Sources, _: AdminActor, page: Page = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    source: str | None = None, q: Annotated[str | None, Query(max_length=100)] = None,
) -> dict:
    return service.list_metadata(page=page, page_size=page_size, source_id=source, query=q)


@admin_router.get("/song-metadata/{source_id}/{song_id}")
def admin_song_metadata_detail(
    source_id: str, song_id: str, service: Sources, _: AdminActor,
) -> dict:
    return service.admin_metadata(source_id, song_id)


@admin_router.post("/song-metadata/{source_id}/{song_id}/archive-images")
def admin_archive_song_images(
    source_id: str, song_id: str, service: Sources, _: AdminActor,
) -> dict:
    return service.archive_metadata_images(source_id, song_id)


@admin_router.post("", status_code=201)
def create_source(body: MusicSourceCreate, service: Sources, _: AdminActor) -> dict:
    return service.create(body)


@admin_router.patch("/{source_id}")
def patch_source(source_id: str, body: MusicSourcePatch, service: Sources, _: AdminActor) -> dict:
    return service.patch(source_id, body)


@router.get("/sources")
def public_sources(service: Sources, _: Actor) -> dict:
    return {
        "items": [
            {key: item[key] for key in ("id", "name", "priority")}
            for item in service.list_sources(enabled_only=True)["items"]
        ]
    }


@router.get("/search")
def search(
    service: Sources, _: Actor, q: str, page: Page = 1, source: str | None = None
) -> dict:
    return service.search(q, page, source)


@router.get("/artwork")
def artwork(url: str, _: Actor) -> Response:
    if len(url) > 2000:
        raise V2DomainError("artwork URL is too long")
    validate_public_url(url, hosts=_ARTWORK_HOSTS)
    try:
        with _opener.open(UrlRequest(url, headers={"Accept": "image/*"}), timeout=8) as upstream:
            content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise V2Unavailable("artwork upstream returned non-image content")
            image = upstream.read(5_000_001)
    except (HTTPError, URLError, TimeoutError) as error:
        raise V2Unavailable("artwork upstream is unavailable") from error
    if len(image) > 5_000_000:
        raise V2Unavailable("artwork is too large")
    return Response(content=image, media_type=content_type, headers={"Cache-Control": "private, max-age=86400"})


@router.get("/images/{name}")
def music_image(name: str, service: Sources) -> Response:
    try:
        url = service.images.signed_url(name)
    except MusicImageArchiveError as error:
        raise V2Unavailable("music image is unavailable") from error
    return RedirectResponse(
        url, status_code=307,
        headers={"Cache-Control": "public, max-age=300", "Referrer-Policy": "no-referrer"},
    )


@router.get("/sources/{source_id}/recommend")
def recommend(source_id: str, service: Sources, _: Actor, page: Page = 1) -> dict:
    return service.recommend(source_id, page)


@router.get("/sources/{source_id}/playlists")
def playlists(source_id: str, service: Sources, _: Actor, page: Page = 1) -> dict:
    return service.playlists(source_id, page)


@router.get("/sources/{source_id}/playlists/{playlist_id}")
def playlist(
    source_id: str, playlist_id: str, service: Sources, _: Actor, page: Page = 1
) -> dict:
    return service.playlist(source_id, playlist_id, page)


@router.get("/sources/{source_id}/songs/{song_id}")
def song(source_id: str, song_id: str, service: Sources, _: Actor, refresh: bool = False) -> dict:
    return service.song(source_id, song_id, refresh)


@router.get("/sources/{source_id}/songs/{song_id}/metadata")
def song_metadata(source_id: str, song_id: str, service: Sources, _: Actor) -> dict:
    return service.metadata(source_id, song_id)


@router.put("/sources/{source_id}/songs/{song_id}/metadata")
def put_song_metadata(
    source_id: str, song_id: str, body: MusicSongMetadataWrite, service: Sources, _: Actor
) -> dict:
    return service.put_metadata(source_id, song_id, body)


@router.get("/sources/{source_id}/songs/{song_id}/lyrics")
def lyrics(source_id: str, song_id: str, service: Sources, _: Actor, refresh: bool = False) -> dict:
    return service.lyrics(source_id, song_id, refresh)


def _audio_response(
    source_id: str, song_id: str, service: MusicSourceService,
    quality: str, range_header: str | None, head: bool,
) -> Response:
    if range_header and not _SINGLE_RANGE.fullmatch(range_header):
        raise V2DomainError("only one byte range is supported")
    url = service.audio_url(source_id, song_id, quality)
    headers = {"Range": range_header} if range_header else {}
    request = UrlRequest(url, headers=headers, method="HEAD" if head else "GET")
    try:
        upstream = _opener.open(request, timeout=15)
    except HTTPError as error:
        if error.code == 416:
            return Response(status_code=416, headers={
                "Content-Range": error.headers.get("Content-Range", "bytes */*"),
                "Cache-Control": "no-store",
            })
        raise V2Unavailable("audio upstream rejected the request") from error
    except (URLError, TimeoutError) as error:
        raise V2Unavailable("audio upstream is unavailable") from error
    content_type = upstream.headers.get("Content-Type", "")
    if not content_type.lower().startswith(("audio/", "application/octet-stream")):
        upstream.close()
        raise V2Unavailable("audio upstream returned non-audio content")
    status = upstream.status
    if status not in {200, 206} or (range_header and status != 206):
        upstream.close()
        raise V2Unavailable("audio upstream did not honor the byte range")
    response_headers = {"Content-Type": content_type, "Cache-Control": "no-store"}
    for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if upstream.headers.get(header):
            response_headers[header] = upstream.headers[header]
    if head:
        upstream.close()
        return Response(status_code=status, headers=response_headers)

    def chunks():
        with upstream:
            while chunk := upstream.read(64 * 1024):
                yield chunk

    return StreamingResponse(chunks(), status_code=status, headers=response_headers)


@router.get("/sources/{source_id}/songs/{song_id}/stream")
def stream_song(
    source_id: str, song_id: str, service: Sources, _: Actor,
    quality: str = "mp3", range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    return _audio_response(source_id, song_id, service, quality, range_header, False)


@router.head("/sources/{source_id}/songs/{song_id}/stream")
def head_song(
    source_id: str, song_id: str, service: Sources, _: Actor,
    quality: str = "mp3", range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    return _audio_response(source_id, song_id, service, quality, range_header, True)
