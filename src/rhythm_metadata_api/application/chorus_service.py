from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import struct
import subprocess
import tempfile
import urllib.request
from collections.abc import AsyncIterable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.catalog_service import (
    ActorContext,
    StoredResponse,
    etag,
    is_expired,
    require_revision,
)
from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.domain.v2.chorus import (
    ChorusMixResolveRequest,
    ChorusMixResponse,
    ChorusModerationQueueItem,
    ChorusModerationQueueResponse,
    ChorusModerationRequest,
    ChorusModerationSettingsResponse,
    ChorusPartResponse,
    ChorusProjectCreate,
    ChorusProjectResponse,
    ChorusSyncAnchor,
    ChorusTimelineResponse,
    ChorusTrackAlignmentPatch,
    ChorusTrackCreate,
    ChorusTrackCreateResponse,
    ChorusTrackResponse,
    WorkChorusResponse,
)
from rhythm_metadata_api.domain.v2.errors import (
    IdempotencyConflict,
    V2Conflict,
    V2DomainError,
    V2NotFound,
)
from rhythm_metadata_api.domain.v2.schemas import AssetDeliveryResponse, UploadTarget
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    Asset,
    AssetLocation,
    AssetSource,
    AuthUser,
    ChangeEvent,
    ChangeEventWork,
    ChorusMixVariant,
    ChorusModerationSettings,
    ChorusProject,
    ChorusTimeline,
    ChorusTrack,
    IdempotencyKey,
    Part,
    Rendition,
    RenditionAsset,
    Score,
    ScoreRenditionSync,
    ScoreRevision,
    UploadSession,
    Work,
    new_id,
    utc_now,
)
from rhythm_metadata_api.infrastructure.storage.base import AssetStorage, UploadValidationError
from rhythm_metadata_api.infrastructure.storage.cos_presign import presign_cos_get, presign_cos_put

T = TypeVar("T")
LOGGER = logging.getLogger(__name__)
MIX_PROFILE = "chorus-aac-v1"
NORMALIZED_PROFILE = "chorus-stem-aac-v1"


def chorus_selection_hash(tracks: list[ChorusTrack]) -> str:
    payload = [
        {
            "id": track.id,
            "revision": track.revision,
            "gain_millibels": track.gain_millibels,
            "pan_milli": track.pan_milli,
            "alignment_offset_ms": track.alignment_offset_ms,
        }
        for track in sorted(tracks, key=lambda item: item.id)
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ChorusService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        storage: AssetStorage,
        settings: Settings,
    ) -> None:
        self.uow_factory = uow_factory
        self.storage = storage
        self.settings = settings

    def list_for_work(self, work_id: str, actor: ActorContext) -> WorkChorusResponse:
        with self.uow_factory() as uow:
            self._require_work(uow.session, work_id)
            projects = list(
                uow.session.scalars(
                    select(ChorusProject)
                    .where(
                        ChorusProject.work_id == work_id,
                        ChorusProject.deleted_at.is_(None),
                        ChorusProject.status.in_(("open", "closed")),
                    )
                    .order_by(ChorusProject.created_at, ChorusProject.id)
                )
            )
            return WorkChorusResponse(
                work_id=work_id,
                projects=[self._project_response(uow.session, item, actor) for item in projects],
            )

    def get_project(self, project_id: str, actor: ActorContext) -> ChorusProjectResponse:
        with self.uow_factory() as uow:
            return self._project_response(
                uow.session, self._require_project(uow.session, project_id), actor
            )

    def moderation_settings(self) -> ChorusModerationSettingsResponse:
        with self.uow_factory() as uow:
            item = uow.session.get(ChorusModerationSettings, "global")
            return ChorusModerationSettingsResponse(
                automatic_approval=item.automatic_approval if item else True,
                updated_by=item.updated_by if item else "system",
                updated_at=item.updated_at if item else None,
            )

    def update_moderation_settings(
        self, automatic_approval: bool, actor: ActorContext
    ) -> ChorusModerationSettingsResponse:
        with self.uow_factory() as uow:
            item = uow.session.get(ChorusModerationSettings, "global")
            if item is None:
                item = ChorusModerationSettings(
                    id="global",
                    automatic_approval=automatic_approval,
                    updated_by=actor.actor_id,
                )
                uow.session.add(item)
            else:
                item.automatic_approval = automatic_approval
                item.updated_by = actor.actor_id
                item.updated_at = utc_now()
            uow.session.flush()
            return ChorusModerationSettingsResponse(
                automatic_approval=item.automatic_approval,
                updated_by=item.updated_by,
                updated_at=item.updated_at,
            )

    def list_tracks_for_moderation(
        self, status: str, actor: ActorContext, limit: int = 100
    ) -> ChorusModerationQueueResponse:
        if status not in {"pending_review", "published", "rejected"}:
            raise V2DomainError("unsupported moderation status")
        with self.uow_factory() as uow:
            rows = list(
                uow.session.execute(
                    select(ChorusTrack, ChorusProject)
                    .join(ChorusProject, ChorusProject.id == ChorusTrack.chorus_project_id)
                    .where(
                        ChorusTrack.status == status,
                        ChorusTrack.deleted_at.is_(None),
                        ChorusProject.deleted_at.is_(None),
                    )
                    .order_by(ChorusTrack.updated_at.desc(), ChorusTrack.id)
                    .limit(limit)
                )
            )
            return ChorusModerationQueueResponse(
                items=[
                    ChorusModerationQueueItem(
                        work_id=project.work_id,
                        project_title=project.title,
                        track=self._track_response(uow.session, track, actor),
                    )
                    for track, project in rows
                ]
            )

    def create_project(
        self,
        work_id: str,
        request: ChorusProjectCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[ChorusProjectResponse, int, dict[str, str]]:
            work = self._require_work(session, work_id)
            arrangement = self._require_arrangement(session, request.arrangement_id)
            if arrangement.work_id != work.id:
                raise V2DomainError("arrangement does not belong to the Work")
            score_revision = self._require_score_revision(
                session, request.alignment_score_revision_id
            )
            score = session.get(Score, score_revision.score_id)
            if score is None or score.arrangement_id != arrangement.id:
                raise V2DomainError("alignment score revision does not belong to the arrangement")
            existing = session.scalar(
                select(ChorusProject).where(
                    ChorusProject.work_id == work.id,
                    ChorusProject.score_id == score.id,
                    ChorusProject.deleted_at.is_(None),
                )
            )
            if existing is not None:
                timeline = session.scalar(
                    select(ChorusTimeline).where(
                        ChorusTimeline.chorus_project_id == existing.id,
                        ChorusTimeline.score_revision_id == score_revision.id,
                        ChorusTimeline.deleted_at.is_(None),
                    )
                )
                if timeline is None:
                    session.add(
                        ChorusTimeline(
                            chorus_project_id=existing.id,
                            score_revision_id=score_revision.id,
                            timeline_hash=request.timeline_hash,
                        )
                    )
                    session.flush()
                    existing.revision += 1
                    existing.updated_at = utc_now()
                    self._append_event(
                        session,
                        work.id,
                        "chorus_project",
                        existing.id,
                        existing.revision,
                        "chorus_timeline.created",
                        actor,
                        {"score_revision_id": score_revision.id},
                    )
                return (
                    self._project_response(session, existing, actor),
                    200,
                    {
                        "Location": f"/v2/chorus-projects/{existing.id}",
                        "ETag": etag(existing.revision),
                    },
                )
            project = ChorusProject(
                work_id=work.id,
                arrangement_id=arrangement.id,
                score_id=score.id,
                alignment_score_revision_id=score_revision.id,
                timeline_hash=request.timeline_hash,
                title=request.title.strip(),
                status=request.status,
                created_by_user_id=actor.actor_id,
            )
            session.add(project)
            session.flush()
            session.add(
                ChorusTimeline(
                    chorus_project_id=project.id,
                    score_revision_id=score_revision.id,
                    timeline_hash=request.timeline_hash,
                )
            )
            session.flush()
            self._append_event(
                session,
                work.id,
                "chorus_project",
                project.id,
                project.revision,
                "chorus_project.created",
                actor,
            )
            return (
                self._project_response(session, project, actor),
                201,
                {"Location": f"/v2/chorus-projects/{project.id}", "ETag": etag(1)},
            )

        return self._idempotent(
            f"POST:/v2/works/{work_id}/chorus-projects",
            idempotency_key,
            request,
            actor,
            operation,
        )

    def create_track(
        self,
        project_id: str,
        request: ChorusTrackCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[ChorusTrackCreateResponse, int, dict[str, str]]:
            project = self._require_project(session, project_id)
            if project.status != "open":
                raise V2Conflict("chorus project is not open for contributions")
            if self.settings.environment != "development" and not self.settings.chorus_cos_bucket:
                raise V2Conflict("chorus COS storage is required outside development")
            timeline = self._timeline_for_request(
                session, project, request.chorus_timeline_id
            )
            self._ensure_actor_user(session, actor)
            if request.part_id is not None:
                part = session.get(Part, request.part_id)
                if (
                    part is None
                    or part.deleted_at is not None
                    or part.arrangement_id != project.arrangement_id
                ):
                    raise V2DomainError("part does not belong to the chorus arrangement")
            rendition = Rendition(
                arrangement_id=project.arrangement_id,
                label=request.display_label.strip(),
                kind="chorus_track",
                duration_ms=request.duration_ms,
            )
            session.add(rendition)
            session.flush()
            track = ChorusTrack(
                chorus_project_id=project.id,
                chorus_timeline_id=timeline.id,
                rendition_id=rendition.id,
                uploader_user_id=actor.actor_id,
                part_id=request.part_id,
                contribution_kind=request.contribution_kind,
                display_label=request.display_label.strip(),
                take_no=request.take_no,
                duration_ms=request.duration_ms,
                alignment_state="automatic" if request.initial_anchors else "pending",
            )
            session.add(track)
            session.flush()
            self._replace_anchors(
                session,
                timeline.score_revision_id,
                rendition.id,
                request.initial_anchors,
            )

            existing = session.scalar(
                select(Asset).where(
                    Asset.sha256 == request.sha256,
                    Asset.deleted_at.is_(None),
                    Asset.state == "ready",
                )
            )
            upload_target = None
            upload_status = "reused"
            if existing is not None:
                if (
                    existing.byte_size != request.byte_size
                    or not existing.detected_media_type.startswith("audio/")
                ):
                    raise V2Conflict("the existing Asset does not match the declared audio")
                session.add(
                    RenditionAsset(
                        rendition_id=rendition.id,
                        asset_id=existing.id,
                        role="master",
                        codec_profile="original",
                    )
                )
                session.add(
                    AssetSource(
                        asset_id=existing.id,
                        original_filename=request.original_filename,
                        source="chorus_upload_reuse",
                        source_ref=track.id,
                    )
                )
            else:
                expires_at = utc_now() + timedelta(seconds=self.settings.upload_session_ttl_seconds)
                upload = UploadSession(
                    expected_sha256=request.sha256,
                    expected_size=request.byte_size,
                    media_type=request.media_type,
                    original_filename=request.original_filename,
                    source="chorus_track",
                    source_ref=track.id,
                    expires_at=expires_at,
                )
                session.add(upload)
                session.flush()
                track.upload_session_id = upload.id
                upload_status = "upload_required"
                if self.settings.chorus_cos_bucket:
                    suffix = Path(request.original_filename).suffix.lower()
                    object_key = f"chorus/uploads/{actor.actor_id}/{upload.id}/original{suffix}"
                    upload.temporary_key = f"cos://{self.settings.chorus_cos_bucket}/{object_key}"
                    url, signed_expiry = presign_cos_put(
                        bucket=self.settings.chorus_cos_bucket,
                        region=self.settings.cos_region,
                        key=object_key,
                        secret_id=self.settings.cos_secret_id,
                        secret_key=self.settings.cos_secret_key,
                        expires_seconds=self.settings.cos_presign_expires_seconds,
                    )
                    upload_target = UploadTarget(id=upload.id, url=url, expires_at=signed_expiry)
                else:
                    upload_target = UploadTarget(
                        id=upload.id,
                        url=f"/v2/chorus-tracks/{track.id}/content",
                        expires_at=expires_at,
                    )
            self._append_event(
                session,
                project.work_id,
                "chorus_track",
                track.id,
                track.revision,
                "chorus_track.created",
                actor,
                {"project_id": project.id, "upload_status": upload_status},
            )
            session.flush()
            return (
                ChorusTrackCreateResponse(
                    track=self._track_response(session, track, actor),
                    upload_status=upload_status,
                    upload=upload_target,
                ),
                201,
                {"Location": f"/v2/chorus-tracks/{track.id}", "ETag": etag(1)},
            )

        return self._idempotent(
            f"POST:/v2/chorus-projects/{project_id}/tracks",
            idempotency_key,
            request,
            actor,
            operation,
        )

    async def write_track_content(
        self,
        track_id: str,
        chunks: AsyncIterable[bytes],
        actor: ActorContext,
    ) -> ChorusTrackResponse:
        with self.uow_factory() as uow:
            track = self._require_owned_track(uow.session, track_id, actor)
            upload = self._track_upload(uow.session, track)
            if upload.temporary_key and upload.temporary_key.startswith("cos://"):
                raise V2Conflict("this upload must be sent directly to its signed COS URL")
            if upload.state == "completed":
                return self._track_response(uow.session, track, actor)
            if upload.state not in {"created", "uploaded", "failed"}:
                raise V2Conflict(f"upload cannot receive content while {upload.state}")
            if is_expired(upload.expires_at):
                upload.state = "expired"
                raise V2Conflict("upload session has expired")
        temporary_key, actual_sha256, actual_size = await self.storage.write_upload(
            upload.id, chunks, min(self.settings.max_audio_bytes, 100 * 1024 * 1024)
        )
        with self.uow_factory() as uow:
            track = self._require_owned_track(uow.session, track_id, actor)
            upload = self._track_upload(uow.session, track)
            upload.temporary_key = temporary_key
            upload.actual_sha256 = actual_sha256
            upload.actual_size = actual_size
            upload.state = "uploaded"
            upload.updated_at = utc_now()
            return self._track_response(uow.session, track, actor)

    def complete_track(
        self,
        track_id: str,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        local_cos_key: str | None = None
        with self.uow_factory() as uow:
            track = self._require_owned_track(uow.session, track_id, actor)
            if self._master_asset_id(uow.session, track.rendition_id) is not None:
                return StoredResponse(
                    200,
                    self._track_response(uow.session, track, actor).model_dump(mode="json"),
                    {"ETag": etag(track.revision)},
                )
            upload = self._track_upload(uow.session, track)
            cos_location = self._parse_cos_temporary_key(upload.temporary_key)
            if cos_location is not None and upload.state != "uploaded":
                local_cos_key, actual_hash, actual_size = self._download_cos_upload(
                    upload.id,
                    cos_location[0],
                    cos_location[1],
                    min(self.settings.max_audio_bytes, 100 * 1024 * 1024),
                )
                upload.actual_sha256 = actual_hash
                upload.actual_size = actual_size
                upload.state = "uploaded"
                upload.updated_at = utc_now()

        request_payload = {"track_id": track_id}

        def operation(session: Session) -> tuple[ChorusTrackResponse, int, dict[str, str]]:
            track = self._require_owned_track(session, track_id, actor)
            if self._master_asset_id(session, track.rendition_id) is not None:
                return (
                    self._track_response(session, track, actor),
                    200,
                    {"ETag": etag(track.revision)},
                )
            upload = self._track_upload(session, track)
            cos_location = self._parse_cos_temporary_key(upload.temporary_key)
            inspection_key = local_cos_key if cos_location is not None else upload.temporary_key
            if upload.state != "uploaded" or not inspection_key:
                raise V2Conflict("track audio has not been uploaded")
            if upload.actual_sha256 != upload.expected_sha256:
                upload.state = "failed"
                raise UploadValidationError("uploaded SHA-256 does not match the declaration")
            if upload.actual_size != upload.expected_size:
                upload.state = "failed"
                raise UploadValidationError("uploaded size does not match the declaration")
            detected = self.storage.inspect_upload(
                inspection_key, upload.media_type, upload.original_filename
            )
            if not detected.startswith("audio/"):
                raise UploadValidationError("chorus track must contain audio")
            asset = session.scalar(select(Asset).where(Asset.sha256 == upload.expected_sha256))
            if asset is None:
                asset = Asset(
                    sha256=upload.expected_sha256,
                    byte_size=upload.expected_size,
                    detected_media_type=detected,
                    state="ready",
                )
                session.add(asset)
                session.flush()
                if cos_location is not None:
                    session.add(
                        AssetLocation(
                            asset_id=asset.id,
                            provider="cos",
                            storage_key=f"{cos_location[0]}/{cos_location[1]}",
                            state="available",
                        )
                    )
                    self.storage.discard(inspection_key)
                else:
                    storage_key = self.storage.promote(inspection_key, upload.expected_sha256)
                    session.add(
                        AssetLocation(
                            asset_id=asset.id,
                            provider="local",
                            storage_key=storage_key,
                            state="available",
                        )
                    )
            elif cos_location is not None:
                self.storage.discard(inspection_key)
            else:
                self.storage.discard(inspection_key)
            session.add(
                AssetSource(
                    asset_id=asset.id,
                    original_filename=upload.original_filename,
                    source="chorus_track",
                    source_ref=track.id,
                )
            )
            session.add(
                RenditionAsset(
                    rendition_id=track.rendition_id,
                    asset_id=asset.id,
                    role="master",
                    codec_profile="original",
                )
            )
            upload.completed_asset_id = asset.id
            upload.state = "completed"
            upload.updated_at = utc_now()
            track.status = "processing"
            track.revision += 1
            track.updated_at = utc_now()
            project = self._require_project(session, track.chorus_project_id)
            self._append_event(
                session,
                project.work_id,
                "chorus_track",
                track.id,
                track.revision,
                "chorus_track.upload_completed",
                actor,
            )
            session.flush()
            return self._track_response(session, track, actor), 200, {"ETag": etag(track.revision)}

        return self._idempotent(
            f"POST:/v2/chorus-tracks/{track_id}/complete",
            idempotency_key,
            request_payload,
            actor,
            operation,
        )

    def process_track(self, track_id: str) -> None:
        try:
            with self.uow_factory() as uow:
                track = self._require_track(uow.session, track_id)
                master_id = self._master_asset_id(uow.session, track.rendition_id)
                if master_id is None:
                    raise V2Conflict("track has no uploaded master Asset")
                asset = self._require_asset(uow.session, master_id)
                rendition_id = track.rendition_id
            with tempfile.TemporaryDirectory(prefix="rhythm-chorus-track-") as temp_root:
                root = Path(temp_root)
                source = root / "source.audio"
                normalized = root / "normalized.m4a"
                self._materialize_asset(asset, source)
                actual_duration_ms = self._probe_duration_ms(source)
                declared_duration_ms = track.duration_ms or actual_duration_ms
                tolerance_ms = max(1_000, round(actual_duration_ms * 0.02))
                if abs(actual_duration_ms - declared_duration_ms) > tolerance_ms:
                    raise UploadValidationError(
                        "decoded audio duration does not match the declared timeline"
                    )
                peaks = self._extract_waveform(source)
                self._run_ffmpeg(
                    [
                        "-i",
                        str(source),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "48000",
                        "-af",
                        "loudnorm=I=-18:TP=-1.5:LRA=11",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        str(normalized),
                    ]
                )
                with self.uow_factory() as uow:
                    track = self._require_track(uow.session, track_id)
                    normalized_asset = self._store_generated_asset(
                        uow.session,
                        normalized,
                        "audio/mp4",
                        f"chorus/tracks/{track.id}/normalized.m4a",
                    )
                    if (
                        uow.session.scalar(
                            select(RenditionAsset.id).where(
                                RenditionAsset.rendition_id == rendition_id,
                                RenditionAsset.role == "stream",
                            )
                        )
                        is None
                    ):
                        uow.session.add(
                            RenditionAsset(
                                rendition_id=rendition_id,
                                asset_id=normalized_asset.id,
                                role="stream",
                                codec_profile=NORMALIZED_PROFILE,
                            )
                        )
                    track.waveform_peaks = peaks
                    track.duration_ms = actual_duration_ms
                    rendition = uow.session.get(Rendition, track.rendition_id)
                    if rendition is not None:
                        rendition.duration_ms = actual_duration_ms
                    track.status = "pending_review"
                    if track.alignment_state == "pending":
                        track.alignment_state = "automatic"
                    track.revision += 1
                    track.updated_at = utc_now()
                    project = self._require_project(uow.session, track.chorus_project_id)
                    self._append_event(
                        uow.session,
                        project.work_id,
                        "chorus_track",
                        track.id,
                        track.revision,
                        "chorus_track.processed",
                        ActorContext(actor_id="chorus-worker"),
                    )
        except Exception as error:  # background job boundary
            LOGGER.exception("chorus track processing failed for %s", track_id)
            with self.uow_factory() as uow:
                track = uow.session.get(ChorusTrack, track_id)
                if track is not None and track.status == "processing":
                    track.status = "failed"
                    track.rejection_reason = str(error)[:2000]
                    track.revision += 1
                    track.updated_at = utc_now()

    def update_alignment(
        self,
        track_id: str,
        request: ChorusTrackAlignmentPatch,
        expected_revision: int,
        actor: ActorContext,
    ) -> ChorusTrackResponse:
        with self.uow_factory() as uow:
            track = self._require_owned_track(uow.session, track_id, actor)
            require_revision(track.revision, expected_revision)
            project = self._require_project(uow.session, track.chorus_project_id)
            timeline = self._require_timeline(uow.session, track.chorus_timeline_id)
            self._replace_anchors(
                uow.session,
                timeline.score_revision_id,
                track.rendition_id,
                request.anchors,
            )
            track.alignment_offset_ms = request.offset_ms
            track.alignment_state = "manual"
            track.revision += 1
            track.updated_at = utc_now()
            self._obsolete_timeline_mixes(uow.session, timeline.id)
            self._append_event(
                uow.session,
                project.work_id,
                "chorus_track",
                track.id,
                track.revision,
                "chorus_track.alignment_updated",
                actor,
            )
            return self._track_response(uow.session, track, actor)

    def submit_track(
        self,
        track_id: str,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[ChorusTrackResponse, int, dict[str, str]]:
            track = self._require_owned_track(session, track_id, actor)
            if track.status not in {"draft", "pending_review"}:
                raise V2Conflict(f"track cannot be submitted while {track.status}")
            if self._master_asset_id(session, track.rendition_id) is None:
                raise V2Conflict("track has no completed audio upload")
            moderation = session.get(ChorusModerationSettings, "global")
            automatic_approval = moderation.automatic_approval if moderation else True
            track.status = "published" if automatic_approval else "pending_review"
            track.rejection_reason = None
            if track.status == "published" and track.alignment_state == "manual":
                track.alignment_state = "verified"
            track.revision += 1
            track.updated_at = utc_now()
            project = self._require_project(session, track.chorus_project_id)
            self._append_event(
                session,
                project.work_id,
                "chorus_track",
                track.id,
                track.revision,
                (
                    "chorus_track.published_automatically"
                    if automatic_approval
                    else "chorus_track.submitted"
                ),
                actor,
            )
            return self._track_response(session, track, actor), 200, {"ETag": etag(track.revision)}

        return self._idempotent(
            f"POST:/v2/chorus-tracks/{track_id}/submit",
            idempotency_key,
            {"track_id": track_id},
            actor,
            operation,
        )

    def moderate_track(
        self,
        track_id: str,
        request: ChorusModerationRequest,
        expected_revision: int,
        actor: ActorContext,
    ) -> ChorusTrackResponse:
        with self.uow_factory() as uow:
            track = self._require_track(uow.session, track_id)
            require_revision(track.revision, expected_revision)
            if track.status not in {"pending_review", "published", "rejected"}:
                raise V2Conflict(f"track cannot be moderated while {track.status}")
            track.status = request.status
            track.rejection_reason = request.reason.strip() if request.reason else None
            track.gain_millibels = round(request.gain_db * 100)
            track.pan_milli = round(request.pan * 1000)
            if request.status == "published" and track.alignment_state == "manual":
                track.alignment_state = "verified"
            track.revision += 1
            track.updated_at = utc_now()
            project = self._require_project(uow.session, track.chorus_project_id)
            self._obsolete_timeline_mixes(uow.session, track.chorus_timeline_id)
            self._append_event(
                uow.session,
                project.work_id,
                "chorus_track",
                track.id,
                track.revision,
                f"chorus_track.{request.status}",
                actor,
            )
            return self._track_response(uow.session, track, actor)

    def withdraw_track(self, track_id: str, actor: ActorContext) -> ChorusTrackResponse:
        with self.uow_factory() as uow:
            track = self._require_owned_track(uow.session, track_id, actor)
            if track.status == "withdrawn":
                return self._track_response(uow.session, track, actor)
            track.status = "withdrawn"
            track.revision += 1
            track.updated_at = utc_now()
            project = self._require_project(uow.session, track.chorus_project_id)
            self._obsolete_timeline_mixes(uow.session, track.chorus_timeline_id)
            self._append_event(
                uow.session,
                project.work_id,
                "chorus_track",
                track.id,
                track.revision,
                "chorus_track.withdrawn",
                actor,
                tombstone=True,
            )
            return self._track_response(uow.session, track, actor)

    def resolve_mix(
        self,
        project_id: str,
        request: ChorusMixResolveRequest,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[ChorusMixResponse, int, dict[str, str]]:
            project = self._require_project(session, project_id)
            tracks = [self._require_track(session, track_id) for track_id in request.track_ids]
            if any(
                track.chorus_project_id != project.id or track.status != "published"
                for track in tracks
            ):
                raise V2DomainError("all selected tracks must be published in this project")
            timeline_ids = {track.chorus_timeline_id for track in tracks}
            if len(timeline_ids) != 1:
                raise V2DomainError("all selected tracks must use the same score revision")
            inferred_timeline_id = next(iter(timeline_ids))
            if (
                request.chorus_timeline_id is not None
                and request.chorus_timeline_id != inferred_timeline_id
            ):
                raise V2DomainError("selected tracks do not belong to the requested timeline")
            timeline = self._timeline_for_request(session, project, inferred_timeline_id)
            selection_hash = chorus_selection_hash(tracks)
            existing = session.scalar(
                select(ChorusMixVariant).where(
                    ChorusMixVariant.chorus_project_id == project.id,
                    ChorusMixVariant.chorus_timeline_id == timeline.id,
                    ChorusMixVariant.selection_hash == selection_hash,
                    ChorusMixVariant.mix_profile == MIX_PROFILE,
                )
            )
            if existing is not None:
                return (
                    self._mix_response(session, existing),
                    200,
                    {"Location": f"/v2/chorus-mixes/{existing.id}"},
                )
            mix = ChorusMixVariant(
                chorus_project_id=project.id,
                chorus_timeline_id=timeline.id,
                selection_hash=selection_hash,
                selected_track_ids=sorted(request.track_ids),
                selected_track_count=len(request.track_ids),
                mix_profile=MIX_PROFILE,
                state="queued",
            )
            session.add(mix)
            session.flush()
            return self._mix_response(session, mix), 202, {"Location": f"/v2/chorus-mixes/{mix.id}"}

        return self._idempotent(
            f"POST:/v2/chorus-projects/{project_id}/mixes:resolve",
            idempotency_key,
            request,
            actor,
            operation,
        )

    def resolve_default_mix(
        self, project_id: str, chorus_timeline_id: str | None = None
    ) -> str | None:
        with self.uow_factory() as uow:
            project = self._require_project(uow.session, project_id)
            timeline = self._timeline_for_request(uow.session, project, chorus_timeline_id)
            tracks = list(
                uow.session.scalars(
                    select(ChorusTrack).where(
                        ChorusTrack.chorus_project_id == project_id,
                        ChorusTrack.chorus_timeline_id == timeline.id,
                        ChorusTrack.status == "published",
                        ChorusTrack.deleted_at.is_(None),
                    )
                )
            )
        if not tracks:
            return None
        response = self.resolve_mix(
            project_id,
            ChorusMixResolveRequest(
                chorus_timeline_id=timeline.id,
                track_ids=[track.id for track in tracks],
            ),
            f"default:{chorus_selection_hash(tracks)}",
            ActorContext(actor_id="chorus-worker"),
        )
        return response.body["id"]

    def get_mix(self, mix_id: str) -> ChorusMixResponse:
        with self.uow_factory() as uow:
            mix = self._require_mix(uow.session, mix_id)
            return self._mix_response(uow.session, mix)

    def render_mix(self, mix_id: str) -> None:
        try:
            with self.uow_factory() as uow:
                mix = self._require_mix(uow.session, mix_id)
                if mix.state == "ready":
                    return
                mix.state = "processing"
                track_ids = list(mix.selected_track_ids)
            with tempfile.TemporaryDirectory(prefix="rhythm-chorus-mix-") as temp_root:
                root = Path(temp_root)
                specs: list[tuple[ChorusTrack, Asset, Path]] = []
                with self.uow_factory() as uow:
                    for index, track_id in enumerate(track_ids):
                        track = self._require_track(uow.session, track_id)
                        if track.status != "published":
                            raise V2Conflict("a selected track is no longer published")
                        if track.chorus_timeline_id != mix.chorus_timeline_id:
                            raise V2Conflict("a selected track belongs to another score revision")
                        asset_id = self._preferred_track_asset_id(uow.session, track.rendition_id)
                        if asset_id is None:
                            raise V2Conflict("a selected track has no playable Asset")
                        asset = self._require_asset(uow.session, asset_id)
                        uow.session.expunge(track)
                        uow.session.expunge(asset)
                        specs.append((track, asset, root / f"track-{index}.audio"))
                for _, asset, destination in specs:
                    self._materialize_asset(asset, destination)
                output = root / "mix.m4a"
                command: list[str] = []
                for _, _, path in specs:
                    command += ["-i", str(path)]
                filters: list[str] = []
                labels: list[str] = []
                for index, (track, _, _) in enumerate(specs):
                    label = f"a{index}"
                    chain = ["aresample=48000"]
                    if track.alignment_offset_ms >= 0:
                        delay = track.alignment_offset_ms
                        chain.append(f"adelay={delay}|{delay}")
                    else:
                        chain += [
                            f"atrim=start={-track.alignment_offset_ms / 1000:.3f}",
                            "asetpts=PTS-STARTPTS",
                        ]
                    chain.append(f"volume={track.gain_millibels / 100:.2f}dB")
                    pan = max(-1.0, min(1.0, track.pan_milli / 1000))
                    left = math.sqrt((1.0 - pan) / 2.0)
                    right = math.sqrt((1.0 + pan) / 2.0)
                    chain.append(f"pan=stereo|c0={left:.6f}*c0|c1={right:.6f}*c0")
                    filters.append(f"[{index}:a]{','.join(chain)}[{label}]")
                    labels.append(f"[{label}]")
                filters.append(
                    f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
                    "dropout_transition=0,alimiter=limit=0.95[out]"
                )
                command += [
                    "-filter_complex",
                    ";".join(filters),
                    "-map",
                    "[out]",
                    "-ac",
                    "2",
                    "-ar",
                    "48000",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    str(output),
                ]
                self._run_ffmpeg(command)
                with self.uow_factory() as uow:
                    mix = self._require_mix(uow.session, mix_id)
                    asset = self._store_generated_asset(
                        uow.session,
                        output,
                        "audio/mp4",
                        f"chorus/mixes/{mix.selection_hash}.m4a",
                    )
                    tracks = [self._require_track(uow.session, item) for item in track_ids]
                    mix.asset_id = asset.id
                    mix.duration_ms = (
                        max(
                            (
                                (track.duration_ms or 0) + max(track.alignment_offset_ms, 0)
                                for track in tracks
                            ),
                            default=0,
                        )
                        or None
                    )
                    mix.state = "ready"
                    mix.ready_at = utc_now()
                    mix.error_summary = None
        except Exception as error:  # background job boundary
            LOGGER.exception("chorus mix rendering failed for %s", mix_id)
            with self.uow_factory() as uow:
                mix = uow.session.get(ChorusMixVariant, mix_id)
                if mix is not None:
                    mix.state = "failed"
                    mix.error_summary = str(error)[:2000]

    def _extract_waveform(self, source: Path) -> list[float]:
        result = self._run_ffmpeg(
            ["-i", str(source), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
            capture_stdout=True,
        )
        sample_count = len(result.stdout) // 2
        if sample_count == 0:
            return []
        samples = struct.unpack(f"<{sample_count}h", result.stdout[: sample_count * 2])
        window = max(1, sample_count // 400)
        peaks = [
            round(max(abs(value) for value in samples[start : start + window]) / 32768, 4)
            for start in range(0, sample_count, window)
        ]
        return peaks[:400]

    def _probe_duration_ms(self, source: Path) -> int:
        ffmpeg = Path(self.settings.chorus_ffmpeg_path)
        ffprobe = str(ffmpeg.with_name("ffprobe")) if ffmpeg.parent != Path(".") else "ffprobe"
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            check=True,
            capture_output=True,
            timeout=self.settings.chorus_mix_timeout_seconds,
        )
        duration_ms = round(float(result.stdout.decode().strip()) * 1000)
        if duration_ms <= 0 or duration_ms > 15 * 60 * 1000:
            raise UploadValidationError("decoded audio duration is invalid")
        return duration_ms

    def _run_ffmpeg(
        self, arguments: list[str], *, capture_stdout: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                self.settings.chorus_ffmpeg_path,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *arguments,
            ],
            check=True,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=self.settings.chorus_mix_timeout_seconds,
        )

    def _materialize_asset(self, asset: Asset, destination: Path) -> None:
        with self.uow_factory() as uow:
            locations = list(
                uow.session.scalars(
                    select(AssetLocation).where(
                        AssetLocation.asset_id == asset.id,
                        AssetLocation.state == "available",
                    )
                )
            )
        local = next((item for item in locations if item.provider == "local"), None)
        if local is not None:
            shutil.copyfile(self.storage.resolve(local.storage_key), destination)
            return
        cos = next((item for item in locations if item.provider == "cos"), None)
        if cos is None:
            raise V2NotFound("Asset has no available storage location")
        bucket, _, key = cos.storage_key.partition("/")
        url, _ = presign_cos_get(
            bucket,
            self.settings.cos_region,
            key,
            self.settings.cos_secret_id,
            self.settings.cos_secret_key,
            self.settings.cos_presign_expires_seconds,
        )
        self._download_url(url, destination, min(asset.byte_size + 1, 100 * 1024 * 1024 + 1))

    def _store_generated_asset(
        self, session: Session, path: Path, media_type: str, cos_key: str
    ) -> Asset:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        size = path.stat().st_size
        existing = session.scalar(select(Asset).where(Asset.sha256 == sha256))
        if existing is not None:
            return existing
        asset = Asset(
            sha256=sha256,
            byte_size=size,
            detected_media_type=media_type,
            state="ready",
        )
        session.add(asset)
        session.flush()
        if self.settings.chorus_cos_bucket:
            url, _ = presign_cos_put(
                self.settings.chorus_cos_bucket,
                self.settings.cos_region,
                cos_key,
                self.settings.cos_secret_id,
                self.settings.cos_secret_key,
                self.settings.cos_presign_expires_seconds,
            )
            request = urllib.request.Request(
                url,
                data=path.read_bytes(),
                method="PUT",
                headers={"Content-Type": media_type},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                if response.status not in {200, 201}:
                    raise OSError(f"COS upload returned HTTP {response.status}")
            location = AssetLocation(
                asset_id=asset.id,
                provider="cos",
                storage_key=f"{self.settings.chorus_cos_bucket}/{cos_key}",
                state="available",
            )
        else:
            temporary_key = f".uploads/generated-{new_id()}.part"
            shutil.copyfile(path, self.storage.resolve(temporary_key))
            location = AssetLocation(
                asset_id=asset.id,
                provider="local",
                storage_key=self.storage.promote(temporary_key, sha256),
                state="available",
            )
        session.add(location)
        return asset

    def _download_cos_upload(
        self, upload_id: str, bucket: str, key: str, max_bytes: int
    ) -> tuple[str, str, int]:
        url, _ = presign_cos_get(
            bucket,
            self.settings.cos_region,
            key,
            self.settings.cos_secret_id,
            self.settings.cos_secret_key,
            self.settings.cos_presign_expires_seconds,
        )
        temporary_key = f".uploads/{upload_id}.cos.part"
        destination = self.storage.resolve(temporary_key)
        self._download_url(url, destination, max_bytes)
        digest = hashlib.sha256()
        with destination.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return temporary_key, digest.hexdigest(), destination.stat().st_size

    @staticmethod
    def _download_url(url: str, destination: Path, max_bytes: int) -> None:
        size = 0
        destination.unlink(missing_ok=True)
        try:
            with (
                urllib.request.urlopen(url, timeout=120) as response,
                destination.open("xb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadValidationError("COS object exceeds the configured limit")
                    output.write(chunk)
            if size == 0:
                raise UploadValidationError("COS object is empty")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def _parse_cos_temporary_key(value: str | None) -> tuple[str, str] | None:
        if not value or not value.startswith("cos://"):
            return None
        bucket, separator, key = value[6:].partition("/")
        if not bucket or not separator or not key:
            raise V2Conflict("COS upload target is invalid")
        return bucket, key

    def _project_response(
        self, session: Session, project: ChorusProject, actor: ActorContext
    ) -> ChorusProjectResponse:
        parts = list(
            session.scalars(
                select(Part)
                .where(Part.arrangement_id == project.arrangement_id, Part.deleted_at.is_(None))
                .order_by(Part.display_order, Part.id)
            )
        )
        tracks = list(
            session.scalars(
                select(ChorusTrack)
                .where(
                    ChorusTrack.chorus_project_id == project.id,
                    ChorusTrack.deleted_at.is_(None),
                )
                .order_by(ChorusTrack.created_at, ChorusTrack.id)
            )
        )
        visible = [
            track
            for track in tracks
            if track.status == "published"
            or (track.uploader_user_id == actor.actor_id and track.status != "withdrawn")
        ]
        timelines = list(
            session.scalars(
                select(ChorusTimeline)
                .join(ScoreRevision, ScoreRevision.id == ChorusTimeline.score_revision_id)
                .where(
                    ChorusTimeline.chorus_project_id == project.id,
                    ChorusTimeline.deleted_at.is_(None),
                )
                .order_by(ScoreRevision.revision_no.desc(), ChorusTimeline.id)
            )
        )
        return ChorusProjectResponse(
            id=project.id,
            work_id=project.work_id,
            arrangement_id=project.arrangement_id,
            score_id=project.score_id,
            alignment_score_revision_id=project.alignment_score_revision_id,
            timeline_hash=project.timeline_hash,
            title=project.title,
            status=project.status,
            revision=project.revision,
            parts=[
                ChorusPartResponse(
                    id=part.id,
                    code=part.code,
                    name=part.name,
                    display_order=part.display_order,
                )
                for part in parts
            ],
            timelines=[
                ChorusTimelineResponse(
                    id=timeline.id,
                    chorus_project_id=timeline.chorus_project_id,
                    score_revision_id=timeline.score_revision_id,
                    timeline_hash=timeline.timeline_hash,
                    revision=timeline.revision,
                    created_at=timeline.created_at,
                    updated_at=timeline.updated_at,
                )
                for timeline in timelines
            ],
            tracks=[self._track_response(session, track, actor) for track in visible],
            created_at=project.created_at,
            updated_at=project.updated_at,
        )

    def _track_response(
        self, session: Session, track: ChorusTrack, actor: ActorContext
    ) -> ChorusTrackResponse:
        user = session.get(AuthUser, track.uploader_user_id)
        anchors = list(
            session.scalars(
                select(ScoreRenditionSync)
                .join(
                    ChorusTimeline,
                    ChorusTimeline.score_revision_id == ScoreRenditionSync.score_revision_id,
                )
                .where(
                    ScoreRenditionSync.rendition_id == track.rendition_id,
                    ChorusTimeline.id == track.chorus_timeline_id,
                )
                .order_by(ScoreRenditionSync.anchor_order)
            )
        )
        return ChorusTrackResponse(
            id=track.id,
            chorus_project_id=track.chorus_project_id,
            chorus_timeline_id=track.chorus_timeline_id,
            rendition_id=track.rendition_id,
            uploader_display_name=(user.display_name if user else None) or "Sonorus user",
            owned_by_requester=track.uploader_user_id == actor.actor_id,
            part_id=track.part_id,
            contribution_kind=track.contribution_kind,
            display_label=track.display_label,
            take_no=track.take_no,
            alignment_state=track.alignment_state,
            alignment_offset_ms=track.alignment_offset_ms,
            status=track.status,
            gain_db=track.gain_millibels / 100,
            pan=track.pan_milli / 1000,
            duration_ms=track.duration_ms,
            waveform_peaks=list(track.waveform_peaks or []),
            rejection_reason=track.rejection_reason,
            revision=track.revision,
            anchors=[
                ChorusSyncAnchor(
                    anchor_order=item.anchor_order,
                    score_tick=item.score_tick,
                    media_ms=item.media_ms,
                    confidence=item.confidence_milli / 1000,
                    source=item.source,
                )
                for item in anchors
            ],
            created_at=track.created_at,
            updated_at=track.updated_at,
        )

    def _mix_response(self, session: Session, mix: ChorusMixVariant) -> ChorusMixResponse:
        delivery = None
        if mix.state == "ready" and mix.asset_id:
            delivery = self._asset_delivery_response(
                session, self._require_asset(session, mix.asset_id)
            )
        return ChorusMixResponse(
            id=mix.id,
            chorus_project_id=mix.chorus_project_id,
            chorus_timeline_id=mix.chorus_timeline_id,
            selection_hash=mix.selection_hash,
            selected_track_ids=list(mix.selected_track_ids),
            selected_track_count=mix.selected_track_count,
            mix_profile=mix.mix_profile,
            state=mix.state,
            duration_ms=mix.duration_ms,
            error_summary=mix.error_summary,
            delivery=delivery,
            created_at=mix.created_at,
            ready_at=mix.ready_at,
        )

    def _asset_delivery_response(self, session: Session, asset: Asset) -> AssetDeliveryResponse:
        locations = list(
            session.scalars(
                select(AssetLocation).where(
                    AssetLocation.asset_id == asset.id,
                    AssetLocation.state == "available",
                )
            )
        )
        cos = next((item for item in locations if item.provider == "cos"), None)
        if cos is not None and self.settings.cos_secret_id:
            bucket, _, key = cos.storage_key.partition("/")
            url, expires_at = presign_cos_get(
                bucket,
                self.settings.cos_region,
                key,
                self.settings.cos_secret_id,
                self.settings.cos_secret_key,
                self.settings.cos_presign_expires_seconds,
            )
            delivery = "signed_url"
        else:
            if not any(item.provider == "local" for item in locations):
                raise V2Conflict("mix Asset cannot be delivered")
            url = f"/v2/assets/{asset.id}/content"
            expires_at = None
            delivery = "authenticated_url"
        return AssetDeliveryResponse(
            asset_id=asset.id,
            media_type=asset.detected_media_type,
            byte_size=asset.byte_size,
            sha256=asset.sha256,
            delivery=delivery,
            url=url,
            cache_key=f"rhythm:asset:{asset.id}:{asset.sha256}",
            etag=f'"sha256:{asset.sha256}"',
            supports_range=True,
            expires_at=expires_at,
        )

    @staticmethod
    def _replace_anchors(
        session: Session,
        score_revision_id: str,
        rendition_id: str,
        anchors: list[ChorusSyncAnchor],
    ) -> None:
        session.execute(
            delete(ScoreRenditionSync).where(ScoreRenditionSync.rendition_id == rendition_id)
        )
        for item in anchors:
            session.add(
                ScoreRenditionSync(
                    score_revision_id=score_revision_id,
                    rendition_id=rendition_id,
                    anchor_order=item.anchor_order,
                    score_tick=item.score_tick,
                    media_ms=item.media_ms,
                    confidence_milli=round(item.confidence * 1000),
                    source=item.source,
                )
            )

    @staticmethod
    def _ensure_actor_user(session: Session, actor: ActorContext) -> None:
        if session.get(AuthUser, actor.actor_id) is None:
            session.add(AuthUser(id=actor.actor_id, display_name=actor.actor_id))
            session.flush()

    @staticmethod
    def _obsolete_timeline_mixes(session: Session, timeline_id: str) -> None:
        for mix in session.scalars(
            select(ChorusMixVariant).where(
                ChorusMixVariant.chorus_timeline_id == timeline_id,
                ChorusMixVariant.state.in_(("queued", "processing", "ready")),
            )
        ):
            mix.state = "obsolete"

    @staticmethod
    def _master_asset_id(session: Session, rendition_id: str) -> str | None:
        return session.scalar(
            select(RenditionAsset.asset_id).where(
                RenditionAsset.rendition_id == rendition_id,
                RenditionAsset.role == "master",
            )
        )

    @staticmethod
    def _preferred_track_asset_id(session: Session, rendition_id: str) -> str | None:
        stream = session.scalar(
            select(RenditionAsset.asset_id).where(
                RenditionAsset.rendition_id == rendition_id,
                RenditionAsset.role == "stream",
            )
        )
        return stream or ChorusService._master_asset_id(session, rendition_id)

    @staticmethod
    def _track_upload(session: Session, track: ChorusTrack) -> UploadSession:
        upload = (
            session.get(UploadSession, track.upload_session_id) if track.upload_session_id else None
        )
        if upload is None or upload.source != "chorus_track" or upload.source_ref != track.id:
            raise V2NotFound("track upload session not found")
        return upload

    @staticmethod
    def _require_work(session: Session, work_id: str) -> Work:
        item = session.get(Work, work_id)
        if item is None or item.deleted_at is not None:
            raise V2NotFound("work not found")
        return item

    @staticmethod
    def _require_arrangement(session: Session, arrangement_id: str) -> Arrangement:
        item = session.get(Arrangement, arrangement_id)
        if item is None or item.deleted_at is not None:
            raise V2NotFound("arrangement not found")
        return item

    @staticmethod
    def _require_score_revision(session: Session, revision_id: str) -> ScoreRevision:
        item = session.get(ScoreRevision, revision_id)
        if item is None:
            raise V2NotFound("score revision not found")
        return item

    @staticmethod
    def _require_project(session: Session, project_id: str) -> ChorusProject:
        item = session.get(ChorusProject, project_id)
        if item is None or item.deleted_at is not None:
            raise V2NotFound("chorus project not found")
        return item

    @staticmethod
    def _require_timeline(session: Session, timeline_id: str) -> ChorusTimeline:
        item = session.get(ChorusTimeline, timeline_id)
        if item is None or item.deleted_at is not None:
            raise V2NotFound("chorus timeline not found")
        return item

    @staticmethod
    def _timeline_for_request(
        session: Session,
        project: ChorusProject,
        timeline_id: str | None,
    ) -> ChorusTimeline:
        if timeline_id is None:
            item = session.scalar(
                select(ChorusTimeline).where(
                    ChorusTimeline.chorus_project_id == project.id,
                    ChorusTimeline.score_revision_id
                    == project.alignment_score_revision_id,
                    ChorusTimeline.deleted_at.is_(None),
                )
            )
        else:
            item = session.get(ChorusTimeline, timeline_id)
        if (
            item is None
            or item.deleted_at is not None
            or item.chorus_project_id != project.id
        ):
            raise V2DomainError("chorus timeline does not belong to the project")
        return item

    @staticmethod
    def _require_track(session: Session, track_id: str) -> ChorusTrack:
        item = session.get(ChorusTrack, track_id)
        if item is None or item.deleted_at is not None:
            raise V2NotFound("chorus track not found")
        return item

    def _require_owned_track(
        self, session: Session, track_id: str, actor: ActorContext
    ) -> ChorusTrack:
        item = self._require_track(session, track_id)
        if item.uploader_user_id != actor.actor_id:
            raise V2NotFound("chorus track not found")
        return item

    @staticmethod
    def _require_mix(session: Session, mix_id: str) -> ChorusMixVariant:
        item = session.get(ChorusMixVariant, mix_id)
        if item is None:
            raise V2NotFound("chorus mix not found")
        return item

    @staticmethod
    def _require_asset(session: Session, asset_id: str) -> Asset:
        item = session.get(Asset, asset_id)
        if item is None or item.deleted_at is not None or item.state != "ready":
            raise V2NotFound("asset not found")
        return item

    @staticmethod
    def _append_event(
        session: Session,
        work_id: str,
        entity_type: str,
        entity_id: str,
        entity_revision: int,
        operation: str,
        actor: ActorContext,
        payload: dict[str, Any] | None = None,
        tombstone: bool = False,
    ) -> None:
        event = ChangeEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            entity_revision=entity_revision,
            operation=operation,
            actor_id=actor.actor_id,
            device_id=actor.device_id,
            request_id=actor.request_id,
            payload_json=payload or {},
            tombstone=tombstone,
        )
        session.add(event)
        session.flush()
        session.add(ChangeEventWork(event_sequence=event.sequence, work_id=work_id))

    def _idempotent(
        self,
        scope: str,
        key: str,
        request: Any,
        actor: ActorContext,
        operation: Callable[[Session], tuple[T, int, dict[str, str]]],
    ) -> StoredResponse:
        if not key.strip() or len(key) > 300:
            raise V2DomainError("Idempotency-Key must contain 1 to 300 characters")
        payload = request.model_dump(mode="json") if hasattr(request, "model_dump") else request
        request_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        with self.uow_factory() as uow:
            session = uow.session
            existing = session.get(IdempotencyKey, (actor.actor_id, scope, key))
            if existing is not None and is_expired(existing.expires_at):
                session.delete(existing)
                session.flush()
                existing = None
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "the same Idempotency-Key was already used with a different request"
                    )
                return StoredResponse(
                    existing.status_code,
                    existing.response_json,
                    existing.response_headers_json,
                    replayed=True,
                )
            response, status_code, headers = operation(session)
            body = response.model_dump(mode="json")
            session.add(
                IdempotencyKey(
                    actor_id=actor.actor_id,
                    scope=scope,
                    key=key,
                    request_hash=request_hash,
                    status_code=status_code,
                    response_json=body,
                    response_headers_json=headers,
                    expires_at=utc_now() + timedelta(days=self.settings.idempotency_ttl_days),
                )
            )
            return StoredResponse(status_code, body, headers)
