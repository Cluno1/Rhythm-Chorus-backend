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
from rhythm_metadata_api.application.device_song_lyrics import (
    DeviceSongLyricsService,
    DeviceSongLyricsWrite,
)
from rhythm_metadata_api.application.music_adapters import (
    _opener,
    validate_public_url,
)
from rhythm_metadata_api.application.music_images import MusicImageArchiveError
from rhythm_metadata_api.application.music_playback import MusicPlaybackService, PlaybackPrepare
from rhythm_metadata_api.application.music_sources import (
    LyricFilters,
    MusicCapabilityPatch,
    MusicSettingsPatch,
    MusicSongLyricsWrite,
    MusicSongMetadataWrite,
    MusicSourceCreate,
    MusicSourcePatch,
    MusicSourceRevision,
    MusicSourceService,
)
from rhythm_metadata_api.domain.v2.errors import V2Conflict, V2DomainError, V2Unavailable

router = APIRouter(prefix="/v2/music", tags=["music sources"])
admin_router = APIRouter(prefix="/v2/admin/music-sources", tags=["music source administration"])
# issue 181: its own prefix, so it can never collide with a source named "settings".
settings_router = APIRouter(prefix="/v2/admin/music-settings", tags=["music source administration"])
_SINGLE_RANGE = re.compile(r"^bytes=\d*-\d*$")


def source_service(request: Request) -> MusicSourceService:
    return request.app.state.v2_container.music_sources


Sources = Annotated[MusicSourceService, Depends(source_service)]


def device_lyrics_service(request: Request) -> DeviceSongLyricsService:
    return request.app.state.v2_container.device_song_lyrics


DeviceLyrics = Annotated[DeviceSongLyricsService, Depends(device_lyrics_service)]
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


@admin_router.post("/{source_id}/lyric-filters/preview")
def preview_lyric_filters(
    source_id: str, body: LyricFilters, service: Sources, _: AdminActor,
) -> dict:
    return service.preview_lyric_filters(source_id, body)


@admin_router.post("", status_code=201)
def create_source(body: MusicSourceCreate, service: Sources, _: AdminActor) -> dict:
    return service.create(body)


@admin_router.patch("/{source_id}")
def patch_source(source_id: str, body: MusicSourcePatch, service: Sources, _: AdminActor) -> dict:
    return service.patch(source_id, body)


@admin_router.patch("/{source_id}/capabilities/{operation}")
def patch_source_capability(
    source_id: str, operation: str, body: MusicCapabilityPatch, service: Sources, _: AdminActor,
) -> dict:
    return service.patch_capability(source_id, operation, body)


@admin_router.post("/{source_id}/archive")
def archive_source(source_id: str, body: MusicSourceRevision, service: Sources, _: AdminActor) -> dict:
    return service.set_archived(source_id, body.revision, True)


@admin_router.post("/{source_id}/restore")
def restore_source(source_id: str, body: MusicSourceRevision, service: Sources, _: AdminActor) -> dict:
    return service.set_archived(source_id, body.revision, False)


@settings_router.post("/health-check", status_code=202)
def trigger_music_health(request: Request, _: AdminActor) -> dict:
    scheduler = request.app.state.music_health_scheduler
    if not scheduler.trigger():
        from rhythm_metadata_api.domain.v2.errors import V2Conflict
        raise V2Conflict("music source health check is already running")
    return {"accepted": True}


@settings_router.get("")
def music_settings(service: Sources, _: AdminActor) -> dict:
    return service.settings()


@settings_router.patch("")
def patch_music_settings(body: MusicSettingsPatch, service: Sources, _: AdminActor) -> dict:
    return service.patch_settings(body)


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


@router.get("/playlists/search")
def search_playlists(
    service: Sources, _: Actor, q: Annotated[str, Query(max_length=100)],
    page: Page = 1, source: str | None = None,
) -> dict:
    return service.search_playlists(q, page, source)


@router.get("/artwork")
def artwork(url: str, service: Sources, _: Actor, source: str | None = None) -> Response:
    if len(url) > 2000:
        raise V2DomainError("artwork URL is too long")
    validate_public_url(url, hosts=service.artwork_hosts(source))
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


@router.get("/recommended-albums")
def recommended_albums(
    service: Sources, _: Actor, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> dict:
    return service.recommended_albums(limit)


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


@router.put("/sources/{source_id}/songs/{song_id}/lyrics")
def put_song_lyrics(
    source_id: str, song_id: str, body: MusicSongLyricsWrite, service: Sources, _: Actor
) -> dict:
    return service.put_lyrics(source_id, song_id, body)


@router.get("/device-lyrics")
def device_lyrics(
    service: DeviceLyrics, _: Actor,
    title: Annotated[str, Query(min_length=1, max_length=500)],
    artist: Annotated[str, Query(min_length=1, max_length=500)],
    duration_ms: Annotated[int | None, Query(ge=0, le=86_400_000)] = None,
) -> dict:
    return service.find(title, artist, duration_ms)


@router.put("/device-lyrics")
def put_device_lyrics(body: DeviceSongLyricsWrite, service: DeviceLyrics, _: Actor) -> dict:
    return service.save(body)


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
        if error.code in {401, 403, 404, 410}:
            service.invalidate_audio_url(source_id, song_id, quality, url)
            refreshed_url = service.audio_url(source_id, song_id, quality)
            try:
                upstream = _opener.open(
                    UrlRequest(refreshed_url, headers=headers, method="HEAD" if head else "GET"),
                    timeout=15,
                )
            except (HTTPError, URLError, TimeoutError) as retry_error:
                raise V2Unavailable("audio upstream rejected the refreshed URL") from retry_error
        elif error.code == 416:
            return Response(status_code=416, headers={
                "Content-Range": error.headers.get("Content-Range", "bytes */*"),
                "Cache-Control": "no-store",
            })
        else:
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


@router.post("/playback/prepare")
def prepare_playback(body: PlaybackPrepare, service: Sources, actor: Actor) -> dict:
    return MusicPlaybackService(service).prepare(body, actor)


def _ticket_audio_response(
    ticket_id: str, service: MusicSourceService, actor,
    range_header: str | None, head: bool,
) -> Response:
    if range_header and not _SINGLE_RANGE.fullmatch(range_header):
        raise V2DomainError("only one byte range is supported")
    ticket, url = MusicPlaybackService(service).resolve(ticket_id, actor)
    headers = {"Range": range_header} if range_header else {}
    if range_header and ticket.etag:
        headers["If-Range"] = ticket.etag
    try:
        upstream = _opener.open(
            UrlRequest(url, headers=headers, method="HEAD" if head else "GET"), timeout=15,
        )
    except HTTPError as error:
        if error.code == 416:
            return Response(status_code=416, headers={
                "Content-Range": f"bytes */{ticket.content_length}", "Cache-Control": "no-store",
            })
        raise V2Conflict("audio entity is unavailable; prepare a new playback ticket") from error
    except (URLError, TimeoutError) as error:
        raise V2Unavailable("audio upstream is unavailable") from error
    content_type = upstream.headers.get("Content-Type", "").split(";", 1)[0].lower()
    content_range = upstream.headers.get("Content-Range", "")
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
    requested_range_valid = not range_header or match is not None
    if range_header and match:
        requested = range_header.removeprefix("bytes=")
        requested_start, requested_end = requested.split("-", 1)
        actual_start, actual_end = int(match.group(1)), int(match.group(2))
        if requested_start:
            expected_end = min(int(requested_end), ticket.content_length - 1) if requested_end else ticket.content_length - 1
            requested_range_valid = actual_start == int(requested_start) and actual_end == expected_end
        else:
            expected_start = max(0, ticket.content_length - int(requested_end)) if requested_end else -1
            requested_range_valid = actual_start == expected_start and actual_end == ticket.content_length - 1
        requested_range_valid = requested_range_valid and upstream.headers.get("Content-Length") == str(actual_end - actual_start + 1)
    valid = (
        content_type == ticket.content_type
        and upstream.status in {200, 206}
        and (upstream.status == 206 if range_header else upstream.status == 200)
        and (upstream.status != 206 or (match is not None and int(match.group(3)) == ticket.content_length))
        and (upstream.status != 200 or upstream.headers.get("Content-Length") == str(ticket.content_length))
        and (not ticket.etag or upstream.headers.get("ETag") == ticket.etag)
        and requested_range_valid
    )
    if not valid:
        upstream.close()
        raise V2Conflict("audio entity changed; prepare a new playback ticket")
    response_headers = {"Content-Type": content_type, "Cache-Control": "no-store"}
    for header in ("Content-Length", "Content-Range", "Accept-Ranges", "ETag"):
        if upstream.headers.get(header):
            response_headers[header] = upstream.headers[header]
    if head:
        upstream.close()
        return Response(status_code=upstream.status, headers=response_headers)

    def chunks():
        with upstream:
            while chunk := upstream.read(64 * 1024):
                yield chunk

    return StreamingResponse(chunks(), status_code=upstream.status, headers=response_headers)


@router.get("/playback/{ticket_id}/stream")
def playback_stream(
    ticket_id: str, service: Sources, actor: Actor,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    return _ticket_audio_response(ticket_id, service, actor, range_header, False)


@router.head("/playback/{ticket_id}/stream")
def playback_stream_head(
    ticket_id: str, service: Sources, actor: Actor,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    return _ticket_audio_response(ticket_id, service, actor, range_header, True)
