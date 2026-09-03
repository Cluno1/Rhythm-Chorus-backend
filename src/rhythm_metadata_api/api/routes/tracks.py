from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import FileResponse

from rhythm_metadata_api.core.auth import require_bootstrap_token
from rhythm_metadata_api.core.config import get_settings
from rhythm_metadata_api.domain.commands import (
    CandidateField,
    CandidateSyncCommand,
    OverrideCommand,
    OverrideField,
    TrackIdentity,
)
from rhythm_metadata_api.domain.metadata import MetadataCandidate, ResolutionPolicy, resolve_field
from rhythm_metadata_api.domain.schemas import (
    ArtifactKind,
    ArtifactResponse,
    CandidateSyncRequest,
    HistoryResponse,
    MatchTrackRequest,
    MetadataResponse,
    OverridePatchRequest,
    TrackResponse,
)
from rhythm_metadata_api.repositories.memory import RevisionConflict
from rhythm_metadata_api.repositories.sqlite import SqliteTrackRepository
from rhythm_metadata_api.storage import EmptyObjectError, LocalObjectStorage, ObjectTooLargeError

router = APIRouter(
    prefix="/tracks",
    tags=["tracks"],
    dependencies=[Depends(require_bootstrap_token)],
)
settings = get_settings()
repository = SqliteTrackRepository(settings.database_path)
object_storage = LocalObjectStorage(settings.local_object_root)


@router.post("/match", response_model=TrackResponse)
def match_track(request: MatchTrackRequest) -> TrackResponse:
    match = repository.match(
        TrackIdentity(
            audio_sha256=request.audio_sha256,
            file_size=request.file_size,
            duration_ms=request.duration_ms,
            title=request.title,
            artist=request.artist,
            album=request.album,
        )
    )
    return TrackResponse(id=match.id, revision=match.revision, matched_by=match.matched_by)


@router.get("/{track_id}/metadata", response_model=MetadataResponse)
def get_metadata(
    track_id: str, policy: ResolutionPolicy = ResolutionPolicy.LOCAL_FIRST
) -> MetadataResponse:
    track = repository.get(track_id)
    if track is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="track not found")

    resolved = {}
    conflicts = {}
    for field_name, raw_candidates in track.candidates.items():
        candidates = [MetadataCandidate.from_mapping(item) for item in raw_candidates]
        result = resolve_field(candidates, policy)
        if result.conflict:
            conflicts[field_name] = [candidate.to_mapping() for candidate in result.candidates]
        elif result.selected is not None:
            resolved[field_name] = result.selected.to_mapping()

    return MetadataResponse(
        track_id=track.id,
        revision=track.revision,
        resolved=resolved,
        conflicts=conflicts,
        candidates=track.candidates,
    )


@router.patch("/{track_id}/overrides", response_model=MetadataResponse)
def patch_overrides(
    track_id: str,
    request: OverridePatchRequest,
    if_match: Annotated[int, Header(alias="If-Match")],
) -> MetadataResponse:
    try:
        repository.apply_overrides(
            track_id,
            if_match,
            OverrideCommand(
                fields={
                    field_name: OverrideField(
                        value=value.value,
                        content_hash=value.content_hash,
                        source=value.source,
                        source_ref=value.source_ref,
                        pin=value.pin,
                    )
                    for field_name, value in request.fields.items()
                }
            ),
        )
    except KeyError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="track not found"
        ) from error
    except RevisionConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "revision conflict", "current_revision": error.current_revision},
        ) from error
    return get_metadata(track_id, request.policy)


@router.put("/{track_id}/candidates", response_model=MetadataResponse)
def put_candidates(
    track_id: str,
    request: CandidateSyncRequest,
    if_match: Annotated[int, Header(alias="If-Match")],
) -> MetadataResponse:
    try:
        repository.sync_candidates(
            track_id,
            if_match,
            CandidateSyncCommand(
                fields={
                    field_name: [
                        CandidateField(
                            value=value.value,
                            content_hash=value.content_hash,
                            source=value.source,
                            source_ref=value.source_ref,
                            user_edited=value.user_edited,
                            pinned=value.pinned,
                        )
                        for value in values
                    ]
                    for field_name, values in request.fields.items()
                }
            ),
        )
    except KeyError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="track not found"
        ) from error
    except RevisionConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "revision conflict", "current_revision": error.current_revision},
        ) from error
    return get_metadata(track_id, request.policy)


@router.get("/{track_id}/history", response_model=HistoryResponse)
def get_history(track_id: str, limit: int = 100) -> HistoryResponse:
    try:
        events = repository.history(track_id, max(1, min(limit, 500)))
    except KeyError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="track not found"
        ) from error
    return HistoryResponse(track_id=track_id, events=events)


@router.put("/{track_id}/artifacts/{kind}", response_model=ArtifactResponse)
async def put_artifact(
    track_id: str,
    kind: ArtifactKind,
    request: Request,
    if_match: Annotated[int, Header(alias="If-Match")],
) -> ArtifactResponse:
    max_bytes = {
        ArtifactKind.LYRICS: settings.max_lyrics_bytes,
        ArtifactKind.ARTWORK: settings.max_artwork_bytes,
        ArtifactKind.AUDIO: settings.max_audio_bytes,
        ArtifactKind.MUSICXML: settings.max_musicxml_bytes,
        ArtifactKind.MIDI: settings.max_midi_bytes,
    }[kind]
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="artifact too large"
        )

    if kind == ArtifactKind.LYRICS:
        content = await request.body()
        if not content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty artifact")
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="artifact too large"
            )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="lyrics must be UTF-8",
            ) from error
        storage_key, content_hash = object_storage.put(content)
        artifact_size = len(content)
    else:
        try:
            storage_key, content_hash, artifact_size = await object_storage.put_stream(
                request.stream(),
                max_bytes,
            )
        except EmptyObjectError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="empty artifact"
            ) from error
        except ObjectTooLargeError as error:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="artifact too large",
            ) from error
    try:
        result = repository.record_artifact(
            track_id,
            if_match,
            kind=kind.value,
            storage_key=storage_key,
            content_hash=content_hash,
            mime_type=request.headers.get("content-type", "application/octet-stream"),
            size=artifact_size,
        )
    except KeyError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="track not found"
        ) from error
    except RevisionConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "revision conflict", "current_revision": error.current_revision},
        ) from error
    return ArtifactResponse(**result)


@router.get("/{track_id}/artifacts/{kind}", response_class=FileResponse)
def get_artifact(track_id: str, kind: ArtifactKind) -> FileResponse:
    artifact = repository.get_artifact(track_id, kind.value)
    if artifact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found")
    path = object_storage.resolve(artifact["storage_key"])
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="artifact unavailable"
        )
    return FileResponse(
        path,
        media_type=artifact["mime_type"],
        headers={
            "ETag": f'"sha256:{artifact["content_hash"]}"',
            "X-Rhythm-Revision": str(artifact["revision"]),
        },
    )
