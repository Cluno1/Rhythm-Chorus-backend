from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.domain.v2.errors import (
    IdempotencyConflict,
    StaleRevision,
    V2Conflict,
    V2DomainError,
    V2NotFound,
)
from rhythm_metadata_api.domain.v2.schemas import (
    ArrangementBundle,
    ArrangementCreate,
    ArrangementPatch,
    ArrangementResponse,
    AssetResponse,
    ChangeResponse,
    ChangesResponse,
    ContributorCreate,
    ContributorResponse,
    PartInput,
    PartResponse,
    PlaybackResponse,
    RenditionAssetInput,
    RenditionAssetResponse,
    RenditionCreate,
    RenditionPatch,
    RenditionResponse,
    ScoreAssetResponse,
    ScoreCreate,
    ScorePatch,
    ScoreResponse,
    ScoreRevisionCreate,
    ScoreRevisionResponse,
    UploadCreate,
    UploadCreateResponse,
    UploadStatusResponse,
    UploadTarget,
    WorkAliasInput,
    WorkBundleResponse,
    WorkCreate,
    WorkCreditResponse,
    WorkPatch,
    WorkResolveCandidate,
    WorkResolveRequest,
    WorkResolveResponse,
    WorkResponse,
)
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    Asset,
    AssetLocation,
    AssetSource,
    ChangeEvent,
    ChangeEventWork,
    Contributor,
    IdempotencyKey,
    Part,
    Rendition,
    RenditionAsset,
    Score,
    ScoreRevision,
    ScoreRevisionAsset,
    UploadSession,
    Work,
    WorkAlias,
    WorkCredit,
    utc_now,
)
from rhythm_metadata_api.infrastructure.storage.base import AssetStorage, UploadValidationError

T = TypeVar("T")


@dataclass(frozen=True)
class ActorContext:
    actor_id: str = "owner"
    device_id: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]
    headers: dict[str, str]
    replayed: bool = False


class CatalogService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        storage: AssetStorage,
        settings: Settings,
    ) -> None:
        self.uow_factory = uow_factory
        self.storage = storage
        self.settings = settings

    def create_contributor(
        self,
        request: ContributorCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[ContributorResponse, int, dict[str, str]]:
            contributor = Contributor(
                display_name=request.display_name.strip(),
                sort_name=request.sort_name.strip() if request.sort_name else None,
            )
            session.add(contributor)
            session.flush()
            response = ContributorResponse(
                id=contributor.id,
                display_name=contributor.display_name,
                sort_name=contributor.sort_name,
                revision=contributor.revision,
            )
            return (
                response,
                201,
                {"Location": f"/v2/contributors/{contributor.id}", "ETag": etag(1)},
            )

        return self._idempotent("POST:/v2/contributors", idempotency_key, request, actor, operation)

    def get_contributor(self, contributor_id: str) -> ContributorResponse:
        with self.uow_factory() as uow:
            contributor = uow.session.get(Contributor, contributor_id)
            if contributor is None or contributor.deleted_at is not None:
                raise V2NotFound("contributor not found")
            return ContributorResponse(
                id=contributor.id,
                display_name=contributor.display_name,
                sort_name=contributor.sort_name,
                revision=contributor.revision,
            )

    def create_work(
        self,
        request: WorkCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[WorkResponse, int, dict[str, str]]:
            self._require_contributors(
                session, [credit.contributor_id for credit in request.credits]
            )
            work = Work(
                canonical_title=request.canonical_title.strip(),
                language=request.language,
                status=request.status,
            )
            session.add(work)
            session.flush()
            for alias in request.aliases:
                session.add(
                    WorkAlias(
                        work_id=work.id,
                        namespace=alias.namespace.strip().lower(),
                        external_id=alias.external_id.strip(),
                    )
                )
            for credit in request.credits:
                session.add(
                    WorkCredit(
                        work_id=work.id,
                        contributor_id=credit.contributor_id,
                        role=credit.role.strip().lower(),
                        position=credit.position,
                    )
                )
            session.flush()
            self._append_event(session, work.id, "work", work.id, 1, "work.created", actor)
            response = self._work_response(session, work)
            return response, 201, {"Location": f"/v2/works/{work.id}", "ETag": etag(1)}

        try:
            return self._idempotent("POST:/v2/works", idempotency_key, request, actor, operation)
        except IntegrityError as error:
            raise V2Conflict("a supplied alias or credit is already in use") from error

    def list_works(
        self, query: str | None, cursor: str | None, limit: int
    ) -> tuple[list[WorkResponse], str | None]:
        with self.uow_factory() as uow:
            statement = select(Work).where(Work.deleted_at.is_(None))
            if query:
                statement = statement.where(Work.canonical_title.ilike(f"%{query.strip()}%"))
            if cursor:
                statement = statement.where(Work.id > cursor)
            rows = list(uow.session.scalars(statement.order_by(Work.id).limit(limit + 1)))
            has_more = len(rows) > limit
            rows = rows[:limit]
            return [self._work_response(uow.session, item) for item in rows], (
                rows[-1].id if has_more and rows else None
            )

    def get_work(self, work_id: str) -> WorkResponse:
        with self.uow_factory() as uow:
            work = self._require_work(uow.session, work_id)
            return self._work_response(uow.session, work)

    def patch_work(
        self, work_id: str, request: WorkPatch, expected_revision: int, actor: ActorContext
    ) -> WorkResponse:
        with self.uow_factory() as uow:
            work = self._require_work(uow.session, work_id)
            require_revision(work.revision, expected_revision)
            changes = request.model_dump(exclude_unset=True)
            if not changes:
                return self._work_response(uow.session, work)
            for key, value in changes.items():
                setattr(work, key, value.strip() if isinstance(value, str) else value)
            work.revision += 1
            work.updated_at = utc_now()
            self._append_event(
                uow.session,
                work.id,
                "work",
                work.id,
                work.revision,
                "work.updated",
                actor,
                {"fields": sorted(changes)},
            )
            uow.session.flush()
            return self._work_response(uow.session, work)

    def resolve_work(self, request: WorkResolveRequest) -> WorkResolveResponse:
        with self.uow_factory() as uow:
            session = uow.session
            if request.work_id:
                work = session.get(Work, request.work_id)
                if work is not None and work.deleted_at is None:
                    return WorkResolveResponse(
                        result="exact",
                        matched_by="work_id",
                        work=self._work_response(session, work),
                    )
            for alias in request.aliases:
                row = session.scalar(
                    select(WorkAlias).where(
                        WorkAlias.namespace == alias.namespace.strip().lower(),
                        WorkAlias.external_id == alias.external_id.strip(),
                    )
                )
                if row is not None:
                    work = self._require_work(session, row.work_id)
                    return WorkResolveResponse(
                        result="exact",
                        matched_by=f"alias:{row.namespace}",
                        work=self._work_response(session, work),
                    )
            asset_work_ids = self._work_ids_for_asset_hashes(session, request.asset_sha256)
            if len(asset_work_ids) == 1:
                work = self._require_work(session, next(iter(asset_work_ids)))
                return WorkResolveResponse(
                    result="exact",
                    matched_by="asset_sha256",
                    work=self._work_response(session, work),
                )

            title = request.metadata.title.strip() if request.metadata.title else None
            if not title:
                return WorkResolveResponse(result="none")
            possible = list(
                session.scalars(
                    select(Work)
                    .where(Work.deleted_at.is_(None))
                    .order_by(Work.canonical_title)
                    .limit(100)
                )
            )
            candidates: list[WorkResolveCandidate] = []
            for work in possible:
                similarity = SequenceMatcher(
                    None, title.casefold(), work.canonical_title.casefold()
                ).ratio()
                if similarity >= 0.65:
                    candidates.append(
                        WorkResolveCandidate(
                            work_id=work.id,
                            canonical_title=work.canonical_title,
                            score=round(similarity, 3),
                            reasons=["normalized_title"],
                        )
                    )
            candidates.sort(key=lambda item: (-item.score, item.work_id))
            return WorkResolveResponse(
                result="candidates" if candidates else "none", candidates=candidates[:10]
            )

    def create_arrangement(
        self,
        work_id: str,
        request: ArrangementCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[ArrangementResponse, int, dict[str, str]]:
            self._require_work(session, work_id)
            if request.based_on_id:
                parent = self._require_arrangement(session, request.based_on_id)
                if parent.work_id != work_id:
                    raise V2DomainError("based_on arrangement must belong to the same work")
            arrangement = Arrangement(
                work_id=work_id,
                name=request.name.strip(),
                voicing=request.voicing,
                key_signature=request.key_signature,
                based_on_id=request.based_on_id,
            )
            session.add(arrangement)
            session.flush()
            seen_codes: set[str] = set()
            for item in request.parts:
                code = item.code.strip().upper()
                if code in seen_codes:
                    raise V2DomainError("part codes must be unique within an arrangement")
                seen_codes.add(code)
                session.add(self._new_part(arrangement.id, item, code))
            session.flush()
            self._append_event(
                session,
                work_id,
                "arrangement",
                arrangement.id,
                1,
                "arrangement.created",
                actor,
            )
            return (
                self._arrangement_response(session, arrangement),
                201,
                {"Location": f"/v2/arrangements/{arrangement.id}", "ETag": etag(1)},
            )

        try:
            return self._idempotent(
                f"POST:/v2/works/{work_id}/arrangements",
                idempotency_key,
                request,
                actor,
                operation,
            )
        except IntegrityError as error:
            raise V2Conflict("arrangement or part constraint failed") from error

    def get_arrangement(self, arrangement_id: str) -> ArrangementResponse:
        with self.uow_factory() as uow:
            arrangement = self._require_arrangement(uow.session, arrangement_id)
            return self._arrangement_response(uow.session, arrangement)

    def patch_arrangement(
        self,
        arrangement_id: str,
        request: ArrangementPatch,
        expected_revision: int,
        actor: ActorContext,
    ) -> ArrangementResponse:
        with self.uow_factory() as uow:
            arrangement = self._require_arrangement(uow.session, arrangement_id)
            require_revision(arrangement.revision, expected_revision)
            changes = request.model_dump(exclude_unset=True)
            preferred = changes.get("preferred_score_id")
            if preferred is not None:
                score = self._require_score(uow.session, preferred)
                if score.arrangement_id != arrangement.id:
                    raise V2DomainError("preferred score must belong to this arrangement")
            if not changes:
                return self._arrangement_response(uow.session, arrangement)
            for key, value in changes.items():
                setattr(arrangement, key, value.strip() if isinstance(value, str) else value)
            arrangement.revision += 1
            arrangement.updated_at = utc_now()
            self._append_event(
                uow.session,
                arrangement.work_id,
                "arrangement",
                arrangement.id,
                arrangement.revision,
                "arrangement.updated",
                actor,
                {"fields": sorted(changes)},
            )
            uow.session.flush()
            return self._arrangement_response(uow.session, arrangement)

    def add_part(
        self,
        arrangement_id: str,
        request: PartInput,
        expected_revision: int,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        request_payload = {
            "body": request.model_dump(mode="json"),
            "expected_revision": expected_revision,
        }

        def operation(session: Session) -> tuple[PartResponse, int, dict[str, str]]:
            arrangement = self._require_arrangement(session, arrangement_id)
            require_revision(arrangement.revision, expected_revision)
            part = self._new_part(arrangement.id, request, request.code.strip().upper())
            session.add(part)
            arrangement.revision += 1
            arrangement.updated_at = utc_now()
            try:
                session.flush()
            except IntegrityError as error:
                raise V2Conflict("part code is already used in this arrangement") from error
            self._append_event(
                session,
                arrangement.work_id,
                "arrangement",
                arrangement.id,
                arrangement.revision,
                "part.created",
                actor,
                {"part_id": part.id},
            )
            return self._part_response(part), 201, {"ETag": etag(arrangement.revision)}

        return self._idempotent(
            f"POST:/v2/arrangements/{arrangement_id}/parts",
            idempotency_key,
            request_payload,
            actor,
            operation,
        )

    def create_upload(
        self,
        request: UploadCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[UploadCreateResponse, int, dict[str, str]]:
            existing = session.scalar(
                select(Asset).where(Asset.sha256 == request.sha256, Asset.deleted_at.is_(None))
            )
            if existing is not None:
                self._add_asset_source(session, existing.id, request)
                return (
                    UploadCreateResponse(status="reused", asset=self._asset_response(existing)),
                    200,
                    {},
                )
            expires_at = utc_now() + timedelta(seconds=self.settings.upload_session_ttl_seconds)
            upload = UploadSession(
                expected_sha256=request.sha256,
                expected_size=request.byte_size,
                media_type=request.media_type,
                original_filename=request.original_filename,
                source=request.source,
                source_ref=request.source_ref,
                expires_at=expires_at,
            )
            session.add(upload)
            session.flush()
            response = UploadCreateResponse(
                status="upload_required",
                upload=UploadTarget(
                    id=upload.id,
                    url=f"/v2/uploads/{upload.id}/content",
                    expires_at=expires_at,
                ),
            )
            return response, 201, {"Location": f"/v2/uploads/{upload.id}"}

        return self._idempotent("POST:/v2/uploads", idempotency_key, request, actor, operation)

    async def write_upload(self, upload_id: str, chunks: Any) -> UploadStatusResponse:
        with self.uow_factory() as uow:
            upload = self._require_upload(uow.session, upload_id)
            if upload.state == "completed":
                return self._upload_response(uow.session, upload)
            if upload.state not in {"created", "uploaded", "failed"}:
                raise V2Conflict(f"upload cannot receive content while {upload.state}")
            if is_expired(upload.expires_at):
                upload.state = "expired"
                raise V2Conflict("upload session has expired")
            max_bytes = self._max_upload_bytes(upload.media_type, upload.original_filename)
        temporary_key, actual_sha256, actual_size = await self.storage.write_upload(
            upload_id, chunks, max_bytes
        )
        with self.uow_factory() as uow:
            upload = self._require_upload(uow.session, upload_id)
            upload.temporary_key = temporary_key
            upload.actual_sha256 = actual_sha256
            upload.actual_size = actual_size
            upload.state = "uploaded"
            upload.updated_at = utc_now()
            return self._upload_response(uow.session, upload)

    def complete_upload(
        self,
        upload_id: str,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        request_payload = {"upload_id": upload_id}

        def operation(session: Session) -> tuple[UploadStatusResponse, int, dict[str, str]]:
            upload = self._require_upload(session, upload_id)
            if upload.state == "completed":
                return self._upload_response(session, upload), 200, {}
            if upload.state != "uploaded" or not upload.temporary_key:
                raise V2Conflict("upload content has not been received")
            if upload.actual_sha256 != upload.expected_sha256:
                upload.state = "failed"
                raise UploadValidationError("uploaded SHA-256 does not match the declaration")
            if upload.actual_size != upload.expected_size:
                upload.state = "failed"
                raise UploadValidationError("uploaded size does not match the declaration")
            detected = self.storage.inspect_upload(
                upload.temporary_key, upload.media_type, upload.original_filename
            )
            asset = session.scalar(select(Asset).where(Asset.sha256 == upload.expected_sha256))
            if asset is None:
                storage_key = self.storage.promote(upload.temporary_key, upload.expected_sha256)
                asset = Asset(
                    sha256=upload.expected_sha256,
                    byte_size=upload.expected_size,
                    detected_media_type=detected,
                    state="ready",
                )
                session.add(asset)
                session.flush()
                session.add(
                    AssetLocation(
                        asset_id=asset.id,
                        provider="local",
                        storage_key=storage_key,
                        state="available",
                    )
                )
            else:
                self.storage.discard(upload.temporary_key)
            session.add(
                AssetSource(
                    asset_id=asset.id,
                    original_filename=upload.original_filename,
                    source=upload.source,
                    source_ref=upload.source_ref,
                )
            )
            upload.completed_asset_id = asset.id
            upload.state = "completed"
            upload.temporary_key = None
            upload.updated_at = utc_now()
            session.flush()
            return self._upload_response(session, upload), 200, {}

        return self._idempotent(
            f"POST:/v2/uploads/{upload_id}/complete",
            idempotency_key,
            request_payload,
            actor,
            operation,
        )

    def get_upload(self, upload_id: str) -> UploadStatusResponse:
        with self.uow_factory() as uow:
            return self._upload_response(uow.session, self._require_upload(uow.session, upload_id))

    def get_asset(self, asset_id: str) -> AssetResponse:
        with self.uow_factory() as uow:
            return self._asset_response(self._require_asset(uow.session, asset_id))

    def asset_content(self, asset_id: str) -> tuple[Path, Asset]:
        with self.uow_factory() as uow:
            asset = self._require_asset(uow.session, asset_id)
            location = uow.session.scalar(
                select(AssetLocation).where(
                    AssetLocation.asset_id == asset.id,
                    AssetLocation.provider == "local",
                    AssetLocation.state == "available",
                )
            )
            if location is None:
                raise V2NotFound("asset has no available local content")
            path = self.storage.resolve(location.storage_key)
            if not path.is_file():
                raise V2NotFound("asset bytes are unavailable")
            uow.session.expunge(asset)
            return path, asset

    def create_score(
        self,
        arrangement_id: str,
        request: ScoreCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[ScoreResponse, int, dict[str, str]]:
            arrangement = self._require_arrangement(session, arrangement_id)
            if request.derived_from_revision_id:
                source_revision = self._require_score_revision(
                    session, request.derived_from_revision_id
                )
                source_score = self._require_score(session, source_revision.score_id)
                if source_score.arrangement_id != arrangement.id:
                    raise V2DomainError("derived revision must belong to the same arrangement")
            score = Score(
                arrangement_id=arrangement.id,
                label=request.label.strip(),
                origin=request.origin,
                derived_from_revision_id=request.derived_from_revision_id,
            )
            session.add(score)
            session.flush()
            self._append_event(
                session,
                arrangement.work_id,
                "score",
                score.id,
                1,
                "score.created",
                actor,
            )
            return (
                self._score_response(score),
                201,
                {
                    "Location": f"/v2/scores/{score.id}",
                    "ETag": etag(1),
                },
            )

        return self._idempotent(
            f"POST:/v2/arrangements/{arrangement_id}/scores",
            idempotency_key,
            request,
            actor,
            operation,
        )

    def get_score(self, score_id: str) -> ScoreResponse:
        with self.uow_factory() as uow:
            return self._score_response(self._require_score(uow.session, score_id))

    def patch_score(
        self, score_id: str, request: ScorePatch, expected_revision: int, actor: ActorContext
    ) -> ScoreResponse:
        with self.uow_factory() as uow:
            score = self._require_score(uow.session, score_id)
            require_revision(score.revision, expected_revision)
            changes = request.model_dump(exclude_unset=True)
            published = changes.get("published_revision_id")
            if published is not None:
                revision = self._require_score_revision(uow.session, published)
                if revision.score_id != score.id:
                    raise V2DomainError("published revision must belong to this score")
            if not changes:
                return self._score_response(score)
            for key, value in changes.items():
                setattr(score, key, value.strip() if isinstance(value, str) else value)
            score.revision += 1
            score.updated_at = utc_now()
            work_id = self._work_id_for_arrangement(uow.session, score.arrangement_id)
            self._append_event(
                uow.session,
                work_id,
                "score",
                score.id,
                score.revision,
                "score.updated",
                actor,
                {"fields": sorted(changes)},
            )
            return self._score_response(score)

    def create_score_revision(
        self,
        score_id: str,
        request: ScoreRevisionCreate,
        expected_revision: int,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[ScoreRevisionResponse, int, dict[str, str]]:
            score = self._require_score(session, score_id)
            require_revision(score.revision, expected_revision)
            if score.head_revision_id is None:
                if request.based_on_revision_id is not None:
                    raise V2Conflict("the first revision cannot specify based_on_revision_id")
                revision_no = 1
            else:
                if request.based_on_revision_id != score.head_revision_id:
                    raise V2Conflict(
                        "based_on_revision_id must be the current score head; create a new Score to fork"
                    )
                revision_no = (
                    session.scalar(
                        select(func.max(ScoreRevision.revision_no)).where(
                            ScoreRevision.score_id == score.id
                        )
                    )
                    + 1
                )
            assets = self._validate_score_assets(session, request)
            revision = ScoreRevision(
                score_id=score.id,
                revision_no=revision_no,
                based_on_revision_id=request.based_on_revision_id,
                edit_message=request.edit_message,
                editor_id=actor.actor_id,
            )
            session.add(revision)
            session.flush()
            for item, _ in assets:
                session.add(
                    ScoreRevisionAsset(
                        score_revision_id=revision.id,
                        asset_id=item.asset_id,
                        role=item.role,
                    )
                )
            score.head_revision_id = revision.id
            score.revision += 1
            score.updated_at = utc_now()
            work_id = self._work_id_for_arrangement(session, score.arrangement_id)
            self._append_event(
                session,
                work_id,
                "score",
                score.id,
                score.revision,
                "score.revision_created",
                actor,
                {"score_revision_id": revision.id, "revision_no": revision_no},
            )
            session.flush()
            return (
                self._score_revision_response(session, revision),
                201,
                {
                    "Location": f"/v2/score-revisions/{revision.id}",
                    "ETag": etag(score.revision),
                },
            )

        return self._idempotent(
            f"POST:/v2/scores/{score_id}/revisions",
            idempotency_key,
            request,
            actor,
            operation,
        )

    def get_score_revision(self, revision_id: str) -> ScoreRevisionResponse:
        with self.uow_factory() as uow:
            return self._score_revision_response(
                uow.session, self._require_score_revision(uow.session, revision_id)
            )

    def create_rendition(
        self,
        arrangement_id: str,
        request: RenditionCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[RenditionResponse, int, dict[str, str]]:
            arrangement = self._require_arrangement(session, arrangement_id)
            rendition = Rendition(
                arrangement_id=arrangement.id,
                label=request.label.strip(),
                kind=request.kind.strip().lower(),
                ensemble=request.ensemble.strip() if request.ensemble else None,
                recorded_at=request.recorded_at,
                location=request.location,
                duration_ms=request.duration_ms,
            )
            session.add(rendition)
            session.flush()
            for item in request.assets:
                self._add_rendition_asset(session, rendition, item)
            self._append_event(
                session,
                arrangement.work_id,
                "rendition",
                rendition.id,
                1,
                "rendition.created",
                actor,
            )
            session.flush()
            return (
                self._rendition_response(session, rendition),
                201,
                {
                    "Location": f"/v2/renditions/{rendition.id}",
                    "ETag": etag(1),
                },
            )

        return self._idempotent(
            f"POST:/v2/arrangements/{arrangement_id}/renditions",
            idempotency_key,
            request,
            actor,
            operation,
        )

    def get_rendition(self, rendition_id: str) -> RenditionResponse:
        with self.uow_factory() as uow:
            return self._rendition_response(
                uow.session, self._require_rendition(uow.session, rendition_id)
            )

    def patch_rendition(
        self,
        rendition_id: str,
        request: RenditionPatch,
        expected_revision: int,
        actor: ActorContext,
    ) -> RenditionResponse:
        with self.uow_factory() as uow:
            rendition = self._require_rendition(uow.session, rendition_id)
            require_revision(rendition.revision, expected_revision)
            changes = request.model_dump(exclude_unset=True)
            if not changes:
                return self._rendition_response(uow.session, rendition)
            for key, value in changes.items():
                setattr(rendition, key, value.strip() if isinstance(value, str) else value)
            rendition.revision += 1
            rendition.updated_at = utc_now()
            work_id = self._work_id_for_arrangement(uow.session, rendition.arrangement_id)
            self._append_event(
                uow.session,
                work_id,
                "rendition",
                rendition.id,
                rendition.revision,
                "rendition.updated",
                actor,
                {"fields": sorted(changes)},
            )
            return self._rendition_response(uow.session, rendition)

    def add_rendition_asset(
        self,
        rendition_id: str,
        request: RenditionAssetInput,
        expected_revision: int,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        request_payload = {
            "body": request.model_dump(mode="json"),
            "expected_revision": expected_revision,
        }

        def operation(session: Session) -> tuple[RenditionResponse, int, dict[str, str]]:
            rendition = self._require_rendition(session, rendition_id)
            require_revision(rendition.revision, expected_revision)
            self._add_rendition_asset(session, rendition, request)
            rendition.revision += 1
            rendition.updated_at = utc_now()
            work_id = self._work_id_for_arrangement(session, rendition.arrangement_id)
            self._append_event(
                session,
                work_id,
                "rendition",
                rendition.id,
                rendition.revision,
                "rendition.asset_added",
                actor,
                {"asset_id": request.asset_id, "role": request.role},
            )
            session.flush()
            return (
                self._rendition_response(session, rendition),
                200,
                {"ETag": etag(rendition.revision)},
            )

        return self._idempotent(
            f"POST:/v2/renditions/{rendition_id}/assets",
            idempotency_key,
            request_payload,
            actor,
            operation,
        )

    def playback(self, rendition_id: str, prefer: str | None) -> PlaybackResponse:
        with self.uow_factory() as uow:
            rendition = self._require_rendition(uow.session, rendition_id)
            links = list(
                uow.session.scalars(
                    select(RenditionAsset).where(RenditionAsset.rendition_id == rendition.id)
                )
            )
            if not links:
                raise V2NotFound("rendition has no playable real-audio assets")
            priority = playback_role_priority(prefer)
            links.sort(key=lambda link: priority.index(link.role) if link.role in priority else 99)
            selected = next(
                (
                    (link, asset)
                    for link in links
                    if link.role in PLAYBACK_AUDIO_ROLES
                    for asset in [self._require_asset(uow.session, link.asset_id)]
                    if is_playable_audio_media_type(asset.detected_media_type)
                ),
                None,
            )
            if selected is None:
                raise V2NotFound("rendition has no playable real-audio assets")
            _, asset = selected
            if asset.state != "ready":
                raise V2Conflict("selected asset is not ready")
            location = uow.session.scalar(
                select(AssetLocation)
                .where(AssetLocation.asset_id == asset.id, AssetLocation.state == "available")
                .order_by(AssetLocation.provider)
            )
            if location is None:
                raise V2NotFound("selected asset has no available content")
            if location.provider != "local":
                raise V2Conflict("configured storage provider cannot issue a playback URL yet")
            return PlaybackResponse(
                rendition_id=rendition.id,
                asset_id=asset.id,
                media_type=asset.detected_media_type,
                byte_size=asset.byte_size,
                delivery="authenticated_url",
                url=f"/v2/assets/{asset.id}/content",
                cache_key=f"rhythm:asset:{asset.id}:{asset.sha256}",
                etag=f'"sha256:{asset.sha256}"',
                supports_range=True,
            )

    def work_bundle(self, work_id: str) -> WorkBundleResponse:
        with self.uow_factory() as uow:
            work = self._require_work(uow.session, work_id)
            arrangements = list(
                uow.session.scalars(
                    select(Arrangement)
                    .where(Arrangement.work_id == work.id, Arrangement.deleted_at.is_(None))
                    .order_by(Arrangement.created_at, Arrangement.id)
                )
            )
            bundles: list[ArrangementBundle] = []
            for arrangement in arrangements:
                arrangement_data = self._arrangement_response(uow.session, arrangement)
                scores = list(
                    uow.session.scalars(
                        select(Score).where(
                            Score.arrangement_id == arrangement.id, Score.deleted_at.is_(None)
                        )
                    )
                )
                renditions = list(
                    uow.session.scalars(
                        select(Rendition).where(
                            Rendition.arrangement_id == arrangement.id,
                            Rendition.deleted_at.is_(None),
                        )
                    )
                )
                bundles.append(
                    ArrangementBundle(
                        **arrangement_data.model_dump(),
                        scores=[self._score_response(item) for item in scores],
                        renditions=[
                            self._rendition_response(uow.session, item) for item in renditions
                        ],
                    )
                )
            version = (
                uow.session.scalar(
                    select(func.max(ChangeEventWork.event_sequence)).where(
                        ChangeEventWork.work_id == work.id
                    )
                )
                or 0
            )
            return WorkBundleResponse(
                work=self._work_response(uow.session, work),
                arrangements=bundles,
                bundle_version=version,
            )

    def changes(self, after: int, limit: int) -> ChangesResponse:
        with self.uow_factory() as uow:
            rows = list(
                uow.session.scalars(
                    select(ChangeEvent)
                    .where(ChangeEvent.sequence > after)
                    .order_by(ChangeEvent.sequence)
                    .limit(limit + 1)
                )
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            changes = []
            for event in rows:
                work_ids = list(
                    uow.session.scalars(
                        select(ChangeEventWork.work_id).where(
                            ChangeEventWork.event_sequence == event.sequence
                        )
                    )
                )
                changes.append(
                    ChangeResponse(
                        sequence=event.sequence,
                        entity_type=event.entity_type,
                        entity_id=event.entity_id,
                        entity_revision=event.entity_revision,
                        operation=event.operation,
                        work_ids=work_ids,
                        tombstone=event.tombstone,
                        created_at=event.created_at,
                    )
                )
            return ChangesResponse(
                changes=changes,
                next_cursor=rows[-1].sequence if rows else after,
                has_more=has_more,
            )

    def bundle_version(self, work_id: str) -> int:
        with self.uow_factory() as uow:
            self._require_work(uow.session, work_id)
            return (
                uow.session.scalar(
                    select(func.max(ChangeEventWork.event_sequence)).where(
                        ChangeEventWork.work_id == work_id
                    )
                )
                or 0
            )

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
                    status_code=existing.status_code,
                    body=existing.response_json,
                    headers=existing.response_headers_json,
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
            return StoredResponse(status_code=status_code, body=body, headers=headers)

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

    @staticmethod
    def _require_work(session: Session, work_id: str) -> Work:
        work = session.get(Work, work_id)
        if work is None or work.deleted_at is not None:
            raise V2NotFound("work not found")
        return work

    @staticmethod
    def _require_arrangement(session: Session, arrangement_id: str) -> Arrangement:
        item = session.get(Arrangement, arrangement_id)
        if item is None or item.deleted_at is not None:
            raise V2NotFound("arrangement not found")
        return item

    @staticmethod
    def _require_score(session: Session, score_id: str) -> Score:
        item = session.get(Score, score_id)
        if item is None or item.deleted_at is not None:
            raise V2NotFound("score not found")
        return item

    @staticmethod
    def _require_score_revision(session: Session, revision_id: str) -> ScoreRevision:
        item = session.get(ScoreRevision, revision_id)
        if item is None:
            raise V2NotFound("score revision not found")
        return item

    @staticmethod
    def _require_rendition(session: Session, rendition_id: str) -> Rendition:
        item = session.get(Rendition, rendition_id)
        if item is None or item.deleted_at is not None:
            raise V2NotFound("rendition not found")
        return item

    @staticmethod
    def _require_asset(session: Session, asset_id: str) -> Asset:
        item = session.get(Asset, asset_id)
        if item is None or item.deleted_at is not None:
            raise V2NotFound("asset not found")
        return item

    @staticmethod
    def _require_upload(session: Session, upload_id: str) -> UploadSession:
        item = session.get(UploadSession, upload_id)
        if item is None:
            raise V2NotFound("upload session not found")
        return item

    @staticmethod
    def _require_contributors(session: Session, contributor_ids: list[str]) -> None:
        for contributor_id in set(contributor_ids):
            item = session.get(Contributor, contributor_id)
            if item is None or item.deleted_at is not None:
                raise V2NotFound(f"contributor {contributor_id} not found")

    @staticmethod
    def _work_id_for_arrangement(session: Session, arrangement_id: str) -> str:
        work_id = session.scalar(
            select(Arrangement.work_id).where(Arrangement.id == arrangement_id)
        )
        if work_id is None:
            raise V2NotFound("arrangement not found")
        return work_id

    @staticmethod
    def _new_part(arrangement_id: str, request: PartInput, code: str) -> Part:
        return Part(
            arrangement_id=arrangement_id,
            code=code,
            name=request.name.strip(),
            display_order=request.display_order,
            midi_channel=request.midi_channel,
        )

    @staticmethod
    def _part_response(part: Part) -> PartResponse:
        return PartResponse(
            id=part.id,
            code=part.code,
            name=part.name,
            display_order=part.display_order,
            midi_channel=part.midi_channel,
        )

    def _work_response(self, session: Session, work: Work) -> WorkResponse:
        aliases = list(
            session.scalars(
                select(WorkAlias).where(WorkAlias.work_id == work.id).order_by(WorkAlias.id)
            )
        )
        credit_rows = session.execute(
            select(WorkCredit, Contributor)
            .join(Contributor, Contributor.id == WorkCredit.contributor_id)
            .where(WorkCredit.work_id == work.id)
            .order_by(WorkCredit.position, WorkCredit.id)
        ).all()
        return WorkResponse(
            id=work.id,
            canonical_title=work.canonical_title,
            language=work.language,
            status=work.status,
            revision=work.revision,
            aliases=[
                WorkAliasInput(namespace=item.namespace, external_id=item.external_id)
                for item in aliases
            ],
            credits=[
                WorkCreditResponse(
                    id=credit.id,
                    contributor_id=contributor.id,
                    display_name=contributor.display_name,
                    role=credit.role,
                    position=credit.position,
                )
                for credit, contributor in credit_rows
            ],
            created_at=work.created_at,
            updated_at=work.updated_at,
        )

    def _arrangement_response(
        self, session: Session, arrangement: Arrangement
    ) -> ArrangementResponse:
        parts = list(
            session.scalars(
                select(Part)
                .where(Part.arrangement_id == arrangement.id, Part.deleted_at.is_(None))
                .order_by(Part.display_order, Part.id)
            )
        )
        return ArrangementResponse(
            id=arrangement.id,
            work_id=arrangement.work_id,
            name=arrangement.name,
            voicing=arrangement.voicing,
            key_signature=arrangement.key_signature,
            based_on_id=arrangement.based_on_id,
            preferred_score_id=arrangement.preferred_score_id,
            revision=arrangement.revision,
            parts=[self._part_response(item) for item in parts],
        )

    @staticmethod
    def _asset_response(asset: Asset) -> AssetResponse:
        return AssetResponse(
            id=asset.id,
            sha256=asset.sha256,
            byte_size=asset.byte_size,
            media_type=asset.detected_media_type,
            state=asset.state,
        )

    @staticmethod
    def _score_response(score: Score) -> ScoreResponse:
        return ScoreResponse(
            id=score.id,
            arrangement_id=score.arrangement_id,
            label=score.label,
            origin=score.origin,
            derived_from_revision_id=score.derived_from_revision_id,
            head_revision_id=score.head_revision_id,
            published_revision_id=score.published_revision_id,
            revision=score.revision,
        )

    def _score_revision_response(
        self, session: Session, revision: ScoreRevision
    ) -> ScoreRevisionResponse:
        rows = session.execute(
            select(ScoreRevisionAsset, Asset)
            .join(Asset, Asset.id == ScoreRevisionAsset.asset_id)
            .where(ScoreRevisionAsset.score_revision_id == revision.id)
            .order_by(ScoreRevisionAsset.role, ScoreRevisionAsset.id)
        ).all()
        return ScoreRevisionResponse(
            id=revision.id,
            score_id=revision.score_id,
            revision_no=revision.revision_no,
            based_on_revision_id=revision.based_on_revision_id,
            edit_message=revision.edit_message,
            assets=[
                ScoreAssetResponse(
                    asset_id=asset.id,
                    role=link.role,
                    sha256=asset.sha256,
                    byte_size=asset.byte_size,
                    media_type=asset.detected_media_type,
                )
                for link, asset in rows
            ],
            created_at=revision.created_at,
        )

    def _rendition_response(self, session: Session, rendition: Rendition) -> RenditionResponse:
        rows = session.execute(
            select(RenditionAsset, Asset)
            .join(Asset, Asset.id == RenditionAsset.asset_id)
            .where(RenditionAsset.rendition_id == rendition.id)
            .order_by(RenditionAsset.role, RenditionAsset.id)
        ).all()
        return RenditionResponse(
            id=rendition.id,
            arrangement_id=rendition.arrangement_id,
            label=rendition.label,
            kind=rendition.kind,
            ensemble=rendition.ensemble,
            recorded_at=rendition.recorded_at,
            location=rendition.location,
            duration_ms=rendition.duration_ms,
            revision=rendition.revision,
            assets=[
                RenditionAssetResponse(
                    id=link.id,
                    asset_id=asset.id,
                    role=link.role,
                    part_id=link.part_id,
                    codec_profile=link.codec_profile,
                    sha256=asset.sha256,
                    byte_size=asset.byte_size,
                    media_type=asset.detected_media_type,
                )
                for link, asset in rows
            ],
        )

    def _upload_response(self, session: Session, upload: UploadSession) -> UploadStatusResponse:
        asset = session.get(Asset, upload.completed_asset_id) if upload.completed_asset_id else None
        return UploadStatusResponse(
            id=upload.id,
            state=upload.state,
            expected_sha256=upload.expected_sha256,
            expected_size=upload.expected_size,
            actual_sha256=upload.actual_sha256,
            actual_size=upload.actual_size,
            expires_at=upload.expires_at,
            asset=self._asset_response(asset) if asset else None,
        )

    def _validate_score_assets(
        self, session: Session, request: ScoreRevisionCreate
    ) -> list[tuple[Any, Asset]]:
        result = []
        for item in request.assets:
            asset = self._require_asset(session, item.asset_id)
            if asset.state != "ready":
                raise V2Conflict(f"asset {asset.id} is not ready")
            if item.role == "primary_musicxml" and asset.detected_media_type not in {
                "application/vnd.recordare.musicxml+xml",
                "application/vnd.recordare.musicxml",
            }:
                raise V2DomainError("primary_musicxml must reference a validated MusicXML asset")
            result.append((item, asset))
        return result

    def _add_rendition_asset(
        self, session: Session, rendition: Rendition, item: RenditionAssetInput
    ) -> RenditionAsset:
        asset = self._require_asset(session, item.asset_id)
        if asset.state != "ready":
            raise V2Conflict("rendition asset is not ready")
        if item.part_id:
            part = session.get(Part, item.part_id)
            if part is None or part.deleted_at is not None:
                raise V2NotFound("part not found")
            if part.arrangement_id != rendition.arrangement_id:
                raise V2DomainError("stem Part must belong to the Rendition arrangement")
        duplicate = session.scalar(
            select(RenditionAsset).where(
                RenditionAsset.rendition_id == rendition.id,
                RenditionAsset.asset_id == item.asset_id,
                RenditionAsset.role == item.role,
                RenditionAsset.part_id == item.part_id,
            )
        )
        if duplicate is not None:
            raise V2Conflict("the Asset is already linked to this Rendition with that role")
        link = RenditionAsset(
            rendition_id=rendition.id,
            asset_id=item.asset_id,
            role=item.role,
            part_id=item.part_id,
            codec_profile=item.codec_profile,
        )
        session.add(link)
        return link

    @staticmethod
    def _add_asset_source(session: Session, asset_id: str, request: UploadCreate) -> None:
        session.add(
            AssetSource(
                asset_id=asset_id,
                original_filename=request.original_filename,
                source=request.source,
                source_ref=request.source_ref,
            )
        )

    @staticmethod
    def _work_ids_for_asset_hashes(session: Session, hashes: list[str]) -> set[str]:
        if not hashes:
            return set()
        score_work_ids = session.scalars(
            select(Arrangement.work_id)
            .join(Score, Score.arrangement_id == Arrangement.id)
            .join(ScoreRevision, ScoreRevision.score_id == Score.id)
            .join(ScoreRevisionAsset, ScoreRevisionAsset.score_revision_id == ScoreRevision.id)
            .join(Asset, Asset.id == ScoreRevisionAsset.asset_id)
            .where(Asset.sha256.in_(hashes))
        )
        rendition_work_ids = session.scalars(
            select(Arrangement.work_id)
            .join(Rendition, Rendition.arrangement_id == Arrangement.id)
            .join(RenditionAsset, RenditionAsset.rendition_id == Rendition.id)
            .join(Asset, Asset.id == RenditionAsset.asset_id)
            .where(Asset.sha256.in_(hashes))
        )
        return set(score_work_ids) | set(rendition_work_ids)

    def _max_upload_bytes(self, media_type: str, filename: str | None) -> int:
        normalized = media_type.lower()
        suffix = Path(filename or "").suffix.lower()
        if normalized.startswith("image/"):
            return self.settings.max_artwork_bytes
        if normalized.startswith("text/") or suffix in {".lrc", ".txt", ".srt"}:
            return self.settings.max_lyrics_bytes
        if "musicxml" in normalized or suffix in {".musicxml", ".mxl", ".xml"}:
            return self.settings.max_musicxml_bytes
        if "midi" in normalized or suffix in {".mid", ".midi"}:
            return self.settings.max_midi_bytes
        if normalized.startswith("audio/"):
            return self.settings.max_audio_bytes
        raise V2DomainError("unsupported upload media type")


def etag(revision: int) -> str:
    return f'"rev-{revision}"'


def bundle_etag(work_id: str, version: int) -> str:
    return f'"bundle-{work_id}-seq-{version}"'


def parse_etag(value: str) -> int:
    normalized = value.strip()
    if normalized.startswith("W/"):
        raise V2DomainError("weak ETags are not accepted for writes")
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == '"':
        normalized = normalized[1:-1]
    if not normalized.startswith("rev-") or not normalized[4:].isdigit():
        raise V2DomainError('If-Match must use the form "rev-N"')
    return int(normalized[4:])


def require_revision(current: int, expected: int) -> None:
    if current != expected:
        raise StaleRevision(etag(expected), etag(current))


def is_expired(value: datetime) -> bool:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= utc_now()


def playback_role_priority(prefer: str | None) -> list[str]:
    requested = prefer if prefer in PLAYBACK_AUDIO_ROLES else "stream"
    return [requested] + [role for role in PLAYBACK_AUDIO_ROLES if role != requested]


PLAYBACK_AUDIO_ROLES = ("stream", "mix", "master")
PLAYABLE_AUDIO_MEDIA_TYPES = frozenset(
    {
        "audio/mpeg",
        "audio/mp3",
        "audio/mp4",
        "audio/m4a",
        "audio/x-m4a",
        "audio/aac",
        "audio/flac",
        "audio/x-flac",
        "audio/ogg",
        "application/ogg",
        "audio/opus",
        "audio/wav",
        "audio/wave",
        "audio/x-wav",
        "audio/vnd.wave",
    }
)


def is_playable_audio_media_type(media_type: str | None) -> bool:
    return bool(
        media_type
        and media_type.split(";", 1)[0].strip().lower() in PLAYABLE_AUDIO_MEDIA_TYPES
    )
