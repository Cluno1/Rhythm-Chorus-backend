from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import and_, case, func, or_, select
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
from rhythm_metadata_api.domain.v2.lyrics import (
    merge_lyrics_sources,
    normalize_language_tag,
    normalize_lyrics_bundle,
)
from rhythm_metadata_api.domain.v2.schemas import (
    ArrangementBundle,
    ArrangementCreate,
    ArrangementPatch,
    ArrangementResponse,
    AssetDeliveryResponse,
    AssetResponse,
    ChangeResponse,
    ChangesResponse,
    ContributorCreate,
    ContributorResponse,
    EffectiveLyricSourcesResponse,
    LibraryAlbumDetailResponse,
    LibraryAlbumResponse,
    LibraryScoreOptionResponse,
    LibraryScoreWorkResponse,
    LibrarySongResponse,
    LyricLanguageFormat,
    LyricSourceDocumentCreate,
    LyricSourceDocumentResponse,
    LyricSourceImageResponse,
    LyricSourceLinkCreate,
    LyricSourcePageCreate,
    LyricSourcePageResponse,
    LyricsTranslation,
    PartInput,
    PartResponse,
    PlaybackResponse,
    RenditionAssetInput,
    RenditionAssetResponse,
    RenditionCreate,
    RenditionLyricReplace,
    RenditionLyricWriteResponse,
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
    ChorusProject,
    Contributor,
    IdempotencyKey,
    LyricSourceDocument,
    LyricSourceLink,
    LyricSourcePage,
    Part,
    Release,
    ReleaseItem,
    Rendition,
    RenditionAsset,
    RenditionCredit,
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
from rhythm_metadata_api.infrastructure.storage.cos_presign import presign_cos_get

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


_LYRICS_FIELDS = frozenset({"lyrics", "lyrics_language", "lyrics_translations"})
_LYRIC_FORMATS = frozenset({"plain", "lrc", "enhanced_lrc", "ttml", "word_by_word_json"})


def _detect_lyric_format(lyrics: str) -> str:
    stripped = lyrics.lstrip()
    if stripped.startswith("<") and ("<tt" in stripped[:500] or "ttml" in stripped[:500]):
        return "ttml"
    if stripped.startswith(("[", "{")) and ('"words"' in stripped or '"timestamp"' in stripped):
        return "word_by_word_json"
    if re.search(r"<\d{1,2}:\d{2}(?:\.\d{1,3})?>", lyrics):
        return "enhanced_lrc"
    if re.search(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?]", lyrics):
        return "lrc"
    return "plain"


def _rendition_format_map(rendition: Rendition) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_language, raw_format in (rendition.lyrics_formats or {}).items():
        try:
            language = normalize_language_tag(raw_language)
        except (TypeError, ValueError):
            continue
        if raw_format in _LYRIC_FORMATS:
            result[language.casefold()] = raw_format
    return result


def _translation_dicts(
    items: list[LyricsTranslation] | list[dict[str, str]],
) -> list[dict[str, str]]:
    return [
        item.model_dump() if isinstance(item, LyricsTranslation) else dict(item) for item in items
    ]


def _normalize_lyrics_or_error(
    lyrics: str | None,
    lyrics_language: str | None,
    lyrics_translations: list[LyricsTranslation] | list[dict[str, str]],
    *,
    fallback_language: str | None,
) -> tuple[str | None, str, list[dict[str, str]]]:
    try:
        return normalize_lyrics_bundle(
            lyrics,
            lyrics_language,
            _translation_dicts(lyrics_translations),
            fallback_language=fallback_language,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise V2DomainError(str(error)) from error


def _merge_lyrics_patch(
    entity: Work | Score | Rendition,
    changes: dict[str, Any],
    *,
    fallback_language: str | None,
) -> None:
    if not _LYRICS_FIELDS.intersection(changes):
        return
    lyrics, language, translations = _normalize_lyrics_or_error(
        changes.get("lyrics", entity.lyrics),
        changes.get("lyrics_language", entity.lyrics_language),
        changes.get("lyrics_translations", entity.lyrics_translations) or [],
        fallback_language=fallback_language,
    )
    changes.update(
        lyrics=lyrics,
        lyrics_language=language,
        lyrics_translations=translations,
    )


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
            lyrics, lyrics_language, lyrics_translations = _normalize_lyrics_or_error(
                request.lyrics,
                request.lyrics_language,
                request.lyrics_translations,
                fallback_language=request.language,
            )
            work = Work(
                canonical_title=request.canonical_title.strip(),
                language=request.language,
                status=request.status,
                lyrics=lyrics,
                lyrics_language=lyrics_language,
                lyrics_translations=lyrics_translations,
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
            _merge_lyrics_patch(
                work,
                changes,
                fallback_language=changes.get("language", work.language),
            )
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

    def asset_delivery(self, asset_id: str) -> AssetDeliveryResponse:
        """Return a short-lived COS URL or the authenticated local fallback."""
        with self.uow_factory() as uow:
            asset = self._require_asset(uow.session, asset_id)
            return self._asset_delivery_response(uow.session, asset)

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

    def create_lyric_source_document(
        self,
        request: LyricSourceDocumentCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(
            session: Session,
        ) -> tuple[LyricSourceDocumentResponse, int, dict[str, str]]:
            if request.document_asset_id is not None:
                asset = self._require_asset(session, request.document_asset_id)
                if asset.state != "ready":
                    raise V2Conflict("lyric source document asset is not ready")
                if request.source_kind == "pdf" and asset.detected_media_type != "application/pdf":
                    raise V2DomainError(
                        "PDF lyric source documents require an application/pdf asset"
                    )
            document = LyricSourceDocument(
                title=request.title.strip(),
                source_kind=request.source_kind,
                edition=request.edition.strip() if request.edition else None,
                publisher=request.publisher.strip() if request.publisher else None,
                published_year=request.published_year,
                document_asset_id=request.document_asset_id,
                source_ref=request.source_ref.strip() if request.source_ref else None,
                rights_note=request.rights_note.strip() if request.rights_note else None,
            )
            session.add(document)
            session.flush()
            return (
                self._lyric_source_document_response(session, document),
                201,
                {"Location": f"/v2/lyric-source-documents/{document.id}"},
            )

        return self._idempotent(
            "POST:/v2/lyric-source-documents",
            idempotency_key,
            request,
            actor,
            operation,
        )

    def get_lyric_source_document(self, document_id: str) -> LyricSourceDocumentResponse:
        with self.uow_factory() as uow:
            document = self._require_lyric_source_document(uow.session, document_id)
            return self._lyric_source_document_response(uow.session, document)

    def add_lyric_source_page(
        self,
        document_id: str,
        request: LyricSourcePageCreate,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        def operation(session: Session) -> tuple[LyricSourcePageResponse, int, dict[str, str]]:
            document = self._require_lyric_source_document(session, document_id)
            asset = self._require_asset(session, request.image_asset_id)
            if asset.state != "ready":
                raise V2Conflict("lyric source page image asset is not ready")
            if not asset.detected_media_type.startswith("image/"):
                raise V2DomainError("lyric source pages require an image asset")
            existing = session.scalar(
                select(LyricSourcePage).where(
                    LyricSourcePage.document_id == document.id,
                    LyricSourcePage.physical_page_number == request.physical_page_number,
                )
            )
            if existing is not None:
                raise V2Conflict("the document already has this physical page")
            page = LyricSourcePage(
                document_id=document.id,
                physical_page_number=request.physical_page_number,
                image_asset_id=asset.id,
                width_px=request.width_px,
                height_px=request.height_px,
                render_dpi=request.render_dpi,
                display_label=request.display_label.strip() if request.display_label else None,
            )
            session.add(page)
            session.flush()
            return (
                self._lyric_source_page_response(page),
                201,
                {"Location": f"/v2/lyric-source-documents/{document.id}/pages/{page.id}"},
            )

        return self._idempotent(
            f"POST:/v2/lyric-source-documents/{document_id}/pages",
            idempotency_key,
            request,
            actor,
            operation,
        )

    def attach_work_lyric_source_page(
        self,
        work_id: str,
        request: LyricSourceLinkCreate,
        expected_revision: int,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        return self._attach_lyric_source_page(
            "work", work_id, request, expected_revision, idempotency_key, actor
        )

    def attach_score_lyric_source_page(
        self,
        score_id: str,
        request: LyricSourceLinkCreate,
        expected_revision: int,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        return self._attach_lyric_source_page(
            "score", score_id, request, expected_revision, idempotency_key, actor
        )

    def attach_rendition_lyric_source_page(
        self,
        rendition_id: str,
        request: LyricSourceLinkCreate,
        expected_revision: int,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        return self._attach_lyric_source_page(
            "rendition", rendition_id, request, expected_revision, idempotency_key, actor
        )

    def effective_lyric_sources(self, rendition_id: str) -> EffectiveLyricSourcesResponse:
        with self.uow_factory() as uow:
            rendition = self._require_rendition(uow.session, rendition_id)
            arrangement = self._require_arrangement(uow.session, rendition.arrangement_id)
            self._require_work(uow.session, arrangement.work_id)
            owners: list[tuple[str, str]] = [("rendition", rendition.id)]
            if arrangement.preferred_score_id is not None:
                score = uow.session.get(Score, arrangement.preferred_score_id)
                if score is not None and score.deleted_at is None:
                    owners.append(("score", score.id))
            owners.append(("work", arrangement.work_id))
            items: list[LyricSourceImageResponse] = []
            seen_pages: set[str] = set()
            for owner_type, owner_id in owners:
                for item in self._lyric_source_images_for_owner(uow.session, owner_type, owner_id):
                    if item.source_page_id not in seen_pages:
                        seen_pages.add(item.source_page_id)
                        items.append(item)
            return EffectiveLyricSourcesResponse(rendition_id=rendition.id, items=items)

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
            work_language = session.scalar(
                select(Work.language).where(Work.id == arrangement.work_id)
            )
            lyrics, lyrics_language, lyrics_translations = _normalize_lyrics_or_error(
                request.lyrics,
                request.lyrics_language,
                request.lyrics_translations,
                fallback_language=work_language,
            )
            score = Score(
                arrangement_id=arrangement.id,
                label=request.label.strip(),
                origin=request.origin,
                derived_from_revision_id=request.derived_from_revision_id,
                lyrics=lyrics,
                lyrics_language=lyrics_language,
                lyrics_translations=lyrics_translations,
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
                self._score_response(session, score),
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
            return self._score_response(uow.session, self._require_score(uow.session, score_id))

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
                return self._score_response(uow.session, score)
            work_language = uow.session.scalar(
                select(Work.language)
                .join(Arrangement, Arrangement.work_id == Work.id)
                .where(Arrangement.id == score.arrangement_id)
            )
            _merge_lyrics_patch(score, changes, fallback_language=work_language)
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
            if published is not None:
                self._open_chorus_project_for_published_revision(
                    uow.session,
                    work_id=work_id,
                    score=score,
                    score_revision=revision,
                    actor=actor,
                )
            return self._score_response(uow.session, score)

    def _open_chorus_project_for_published_revision(
        self,
        session: Session,
        *,
        work_id: str,
        score: Score,
        score_revision: ScoreRevision,
        actor: ActorContext,
    ) -> None:
        template = session.scalar(
            select(ChorusProject)
            .where(
                ChorusProject.work_id == work_id,
                ChorusProject.status == "open",
                ChorusProject.deleted_at.is_(None),
            )
            .order_by(ChorusProject.created_at, ChorusProject.id)
        )
        if template is None:
            return
        existing = session.scalar(
            select(ChorusProject.id).where(
                ChorusProject.work_id == work_id,
                ChorusProject.alignment_score_revision_id == score_revision.id,
                ChorusProject.deleted_at.is_(None),
            )
        )
        if existing is not None:
            return
        timeline_hash = session.scalar(
            select(Asset.sha256)
            .join(ScoreRevisionAsset, ScoreRevisionAsset.asset_id == Asset.id)
            .where(
                ScoreRevisionAsset.score_revision_id == score_revision.id,
                ScoreRevisionAsset.role == "primary_musicxml",
                Asset.state == "ready",
                Asset.deleted_at.is_(None),
            )
        )
        if timeline_hash is None:
            raise V2Conflict("published score revision has no ready primary MusicXML")
        project = ChorusProject(
            work_id=work_id,
            arrangement_id=score.arrangement_id,
            alignment_score_revision_id=score_revision.id,
            timeline_hash=timeline_hash,
            title=template.title,
            status="open",
            created_by_user_id=actor.actor_id,
        )
        session.add(project)
        session.flush()
        self._append_event(
            session,
            work_id,
            "chorus_project",
            project.id,
            project.revision,
            "chorus_project.created",
            actor,
            {"source": "score_revision_published", "score_revision_id": score_revision.id},
        )

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
            work_language = session.scalar(
                select(Work.language).where(Work.id == arrangement.work_id)
            )
            lyrics, lyrics_language, lyrics_translations = _normalize_lyrics_or_error(
                request.lyrics,
                request.lyrics_language,
                request.lyrics_translations,
                fallback_language=work_language,
            )
            rendition = Rendition(
                arrangement_id=arrangement.id,
                label=request.label.strip(),
                kind=request.kind.strip().lower(),
                ensemble=request.ensemble.strip() if request.ensemble else None,
                recorded_at=request.recorded_at,
                location=request.location,
                duration_ms=request.duration_ms,
                lyrics=lyrics,
                lyrics_language=lyrics_language,
                lyrics_translations=lyrics_translations,
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
            work_language = uow.session.scalar(
                select(Work.language)
                .join(Arrangement, Arrangement.work_id == Work.id)
                .where(Arrangement.id == rendition.arrangement_id)
            )
            _merge_lyrics_patch(rendition, changes, fallback_language=work_language)
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

    def replace_rendition_lyrics(
        self,
        rendition_id: str,
        language: str,
        request: RenditionLyricReplace,
        expected_revision: int,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        try:
            normalized_language = normalize_language_tag(language)
        except ValueError as error:
            raise V2DomainError(str(error)) from error

        request_payload = {
            "language": normalized_language,
            "body": request.model_dump(mode="json"),
            "expected_revision": expected_revision,
        }

        def operation(
            session: Session,
        ) -> tuple[RenditionLyricWriteResponse, int, dict[str, str]]:
            rendition = self._require_rendition(session, rendition_id)
            require_revision(rendition.revision, expected_revision)

            primary_lyrics = rendition.lyrics
            primary_language = rendition.lyrics_language
            translations = [dict(item) for item in (rendition.lyrics_translations or [])]
            if primary_lyrics is None:
                primary_lyrics = request.lyrics
                primary_language = normalized_language
                translations = []
            elif primary_language.casefold() == normalized_language.casefold():
                primary_lyrics = request.lyrics
            else:
                replacement = {
                    "language": normalized_language,
                    "lyrics": request.lyrics,
                }
                matching_index = next(
                    (
                        index
                        for index, item in enumerate(translations)
                        if normalize_language_tag(item["language"]).casefold()
                        == normalized_language.casefold()
                    ),
                    None,
                )
                if matching_index is None:
                    translations.append(replacement)
                else:
                    translations[matching_index] = replacement

            lyrics, lyrics_language, lyrics_translations = _normalize_lyrics_or_error(
                primary_lyrics,
                primary_language,
                translations,
                fallback_language=None,
            )
            rendition.lyrics = lyrics
            rendition.lyrics_language = lyrics_language
            rendition.lyrics_translations = lyrics_translations
            formats = _rendition_format_map(rendition)
            formats[normalized_language.casefold()] = request.format
            rendition.lyrics_formats = {
                item_language: formats.get(
                    item_language.casefold(),
                    _detect_lyric_format(item_lyrics),
                )
                for item_language, item_lyrics in [
                    (lyrics_language, lyrics or ""),
                    *[
                        (item["language"], item["lyrics"])
                        for item in lyrics_translations
                    ],
                ]
            }
            rendition.revision += 1
            rendition.updated_at = utc_now()
            work_id = self._work_id_for_arrangement(session, rendition.arrangement_id)
            self._append_event(
                session,
                work_id,
                "rendition",
                rendition.id,
                rendition.revision,
                "rendition.lyrics_replaced",
                actor,
                {"language": normalized_language, "format": request.format},
            )
            response = RenditionLyricWriteResponse(
                rendition_id=rendition.id,
                revision=rendition.revision,
                language=normalized_language,
                lyrics=request.lyrics,
                format=request.format,
                lyrics_language=lyrics_language,
                lyrics_translations=lyrics_translations,
                lyrics_formats=[
                    LyricLanguageFormat(language=item_language, format=item_format)
                    for item_language, item_format in rendition.lyrics_formats.items()
                ],
            )
            return response, 200, {"ETag": etag(rendition.revision)}

        return self._idempotent(
            f"PUT:/v2/renditions/{rendition_id}/lyrics/{normalized_language}",
            idempotency_key,
            request_payload,
            actor,
            operation,
        )

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
            delivery = self._asset_delivery_response(uow.session, asset)
            playback_fields = delivery.model_dump(exclude={"sha256"})
            return PlaybackResponse(
                rendition_id=rendition.id,
                **playback_fields,
            )

    def list_library_songs(
        self, cursor: str | None, limit: int
    ) -> tuple[list[LibrarySongResponse], str | None]:
        with self.uow_factory() as uow:
            statement = self._library_song_statement()
            if cursor:
                cursor_release, cursor_order, cursor_id = decode_song_cursor(cursor)
                statement = statement.where(
                    or_(
                        Release.key > cursor_release,
                        and_(
                            Release.key == cursor_release,
                            ReleaseItem.display_order > cursor_order,
                        ),
                        and_(
                            Release.key == cursor_release,
                            ReleaseItem.display_order == cursor_order,
                            ReleaseItem.id > cursor_id,
                        ),
                    )
                )
            rows = uow.session.execute(
                statement.order_by(Release.key, ReleaseItem.display_order, ReleaseItem.id).limit(
                    limit + 1
                )
            ).all()
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = None
            if has_more and rows:
                item, release, *_ = rows[-1]
                next_cursor = encode_song_cursor(release.key, item.display_order, item.id)
            return [self._library_song_response(uow.session, *row) for row in rows], next_cursor

    def list_library_albums(
        self, cursor: str | None, limit: int
    ) -> tuple[list[LibraryAlbumResponse], str | None]:
        with self.uow_factory() as uow:
            statement = select(Release).where(Release.deleted_at.is_(None))
            if cursor:
                statement = statement.where(Release.id > cursor)
            rows = list(uow.session.scalars(statement.order_by(Release.id).limit(limit + 1)))
            has_more = len(rows) > limit
            rows = rows[:limit]
            return [self._library_album_response(uow.session, row) for row in rows], (
                rows[-1].id if has_more and rows else None
            )

    def list_library_score_works(
        self, cursor: str | None, limit: int
    ) -> tuple[list[LibraryScoreWorkResponse], str | None]:
        deliverable_providers = ["local"]
        if self.settings.cos_secret_id and self.settings.cos_secret_key:
            deliverable_providers.append("cos")
        musicxml_types = {
            "application/vnd.recordare.musicxml+xml",
            "application/vnd.recordare.musicxml",
            "application/xml",
            "text/xml",
        }
        with self.uow_factory() as uow:
            eligible_asset = (
                select(ScoreRevisionAsset.asset_id)
                .join(Asset, Asset.id == ScoreRevisionAsset.asset_id)
                .where(
                    ScoreRevisionAsset.score_revision_id == Score.published_revision_id,
                    ScoreRevisionAsset.role == "primary_musicxml",
                    Asset.state == "ready",
                    Asset.deleted_at.is_(None),
                    Asset.detected_media_type.in_(musicxml_types),
                    select(AssetLocation.id)
                    .where(
                        AssetLocation.asset_id == Asset.id,
                        AssetLocation.state == "available",
                        AssetLocation.provider.in_(deliverable_providers),
                    )
                    .exists(),
                )
                .exists()
            )
            work_statement = (
                select(Work)
                .where(
                    Work.deleted_at.is_(None),
                    Work.status == "active",
                    select(Score.id)
                    .join(Arrangement, Arrangement.id == Score.arrangement_id)
                    .where(
                        Arrangement.work_id == Work.id,
                        Arrangement.deleted_at.is_(None),
                        Score.deleted_at.is_(None),
                        Score.published_revision_id.is_not(None),
                        eligible_asset,
                    )
                    .exists(),
                )
                .order_by(Work.id)
                .limit(limit + 1)
            )
            if cursor:
                work_statement = work_statement.where(Work.id > cursor)
            works = list(uow.session.scalars(work_statement))
            has_more = len(works) > limit
            works = works[:limit]
            responses = [
                self._library_score_work_response(
                    uow.session, work, deliverable_providers, musicxml_types
                )
                for work in works
            ]
            return responses, works[-1].id if has_more and works else None

    def _library_score_work_response(
        self,
        session: Session,
        work: Work,
        deliverable_providers: list[str],
        musicxml_types: set[str],
    ) -> LibraryScoreWorkResponse:
        rows = session.execute(
            select(Score, Arrangement, ScoreRevision, func.count(Part.id))
            .join(Arrangement, Arrangement.id == Score.arrangement_id)
            .join(ScoreRevision, ScoreRevision.id == Score.published_revision_id)
            .outerjoin(
                Part,
                and_(Part.arrangement_id == Arrangement.id, Part.deleted_at.is_(None)),
            )
            .where(
                Arrangement.work_id == work.id,
                Arrangement.deleted_at.is_(None),
                Score.deleted_at.is_(None),
                select(ScoreRevisionAsset.asset_id)
                .join(Asset, Asset.id == ScoreRevisionAsset.asset_id)
                .where(
                    ScoreRevisionAsset.score_revision_id == ScoreRevision.id,
                    ScoreRevisionAsset.role == "primary_musicxml",
                    Asset.state == "ready",
                    Asset.deleted_at.is_(None),
                    Asset.detected_media_type.in_(musicxml_types),
                    select(AssetLocation.id)
                    .where(
                        AssetLocation.asset_id == Asset.id,
                        AssetLocation.state == "available",
                        AssetLocation.provider.in_(deliverable_providers),
                    )
                    .exists(),
                )
                .exists(),
            )
            .group_by(Score.id, Arrangement.id, ScoreRevision.id)
            .order_by(ScoreRevision.created_at.desc(), ScoreRevision.revision_no.desc(), Score.id)
        ).all()
        options = [
            LibraryScoreOptionResponse(
                arrangement_id=arrangement.id,
                arrangement_name=arrangement.name,
                score_id=score.id,
                revision_id=revision.id,
                score_label=score.label,
                origin=score.origin,
                part_count=part_count,
                revision_no=revision.revision_no,
                published_at=revision.created_at,
                preferred=arrangement.preferred_score_id == score.id,
            )
            for score, arrangement, revision, part_count in rows
        ]
        preferred = [option for option in options if option.preferred]
        default = preferred[0] if preferred else options[0]
        artist = session.scalar(
            select(Contributor.display_name)
            .join(WorkCredit, WorkCredit.contributor_id == Contributor.id)
            .where(WorkCredit.work_id == work.id)
            .order_by(
                case((WorkCredit.role == "composer", 0), else_=1),
                WorkCredit.position,
                WorkCredit.id,
            )
            .limit(1)
        )
        cover_delivery = self._cover_delivery(session, work.cover_asset_id)
        return LibraryScoreWorkResponse(
            work_id=work.id,
            title=work.canonical_title,
            artist=artist,
            cover_asset_id=cover_delivery.asset_id if cover_delivery else None,
            cover_url=cover_delivery.url if cover_delivery else None,
            default_score_id=default.score_id,
            latest_published_at=max(
                options,
                key=lambda option: _aware_datetime(option.published_at),
            ).published_at,
            score_count=len(options),
            origins=sorted({option.origin for option in options}),
            score_options=options,
        )

    def get_library_album(self, album_id: str) -> LibraryAlbumDetailResponse:
        with self.uow_factory() as uow:
            release = uow.session.scalar(
                select(Release).where(Release.id == album_id, Release.deleted_at.is_(None))
            )
            if release is None:
                raise V2NotFound("album not found")
            rows = uow.session.execute(
                self._library_song_statement()
                .where(ReleaseItem.release_id == release.id)
                .order_by(ReleaseItem.display_order, ReleaseItem.id)
            ).all()
            return LibraryAlbumDetailResponse(
                album=self._library_album_response(uow.session, release),
                songs=[self._library_song_response(uow.session, *row) for row in rows],
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
                        scores=[self._score_response(uow.session, item) for item in scores],
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
    def _require_lyric_source_document(session: Session, document_id: str) -> LyricSourceDocument:
        item = session.get(LyricSourceDocument, document_id)
        if item is None:
            raise V2NotFound("lyric source document not found")
        return item

    @staticmethod
    def _require_lyric_source_page(session: Session, page_id: str) -> LyricSourcePage:
        item = session.get(LyricSourcePage, page_id)
        if item is None:
            raise V2NotFound("lyric source page not found")
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

    def _attach_lyric_source_page(
        self,
        owner_type: str,
        owner_id: str,
        request: LyricSourceLinkCreate,
        expected_revision: int,
        idempotency_key: str,
        actor: ActorContext,
    ) -> StoredResponse:
        request_payload = {
            "body": request.model_dump(mode="json"),
            "expected_revision": expected_revision,
        }

        def operation(session: Session) -> tuple[LyricSourceImageResponse, int, dict[str, str]]:
            if owner_type == "work":
                owner: Work | Score | Rendition = self._require_work(session, owner_id)
                owner_column = LyricSourceLink.work_id
                work_id = owner.id
            elif owner_type == "score":
                owner = self._require_score(session, owner_id)
                owner_column = LyricSourceLink.score_id
                work_id = self._work_id_for_arrangement(session, owner.arrangement_id)
            else:
                owner = self._require_rendition(session, owner_id)
                owner_column = LyricSourceLink.rendition_id
                work_id = self._work_id_for_arrangement(session, owner.arrangement_id)
            require_revision(owner.revision, expected_revision)
            self._require_lyric_source_page(session, request.source_page_id)
            if (
                session.scalar(
                    select(LyricSourceLink.id).where(
                        owner_column == owner_id,
                        LyricSourceLink.source_page_id == request.source_page_id,
                    )
                )
                is not None
            ):
                raise V2Conflict("this lyric source page is already linked to the owner")
            link = LyricSourceLink(
                source_page_id=request.source_page_id,
                display_order=request.display_order,
                language_relations=[
                    relation.model_dump(mode="json") for relation in request.language_relations
                ],
                note=request.note.strip() if request.note else None,
                **{f"{owner_type}_id": owner_id},
            )
            session.add(link)
            owner.revision += 1
            owner.updated_at = utc_now()
            self._append_event(
                session,
                work_id,
                owner_type,
                owner.id,
                owner.revision,
                f"{owner_type}.lyric_source_page_added",
                actor,
                {"source_page_id": request.source_page_id},
            )
            session.flush()
            item = next(
                item
                for item in self._lyric_source_images_for_owner(session, owner_type, owner_id)
                if item.link_id == link.id
            )
            return item, 201, {"ETag": etag(owner.revision)}

        return self._idempotent(
            f"POST:/v2/{owner_type}s/{owner_id}/lyric-source-pages",
            idempotency_key,
            request_payload,
            actor,
            operation,
        )

    @staticmethod
    def _lyric_source_page_response(page: LyricSourcePage) -> LyricSourcePageResponse:
        return LyricSourcePageResponse(
            id=page.id,
            document_id=page.document_id,
            physical_page_number=page.physical_page_number,
            image_asset_id=page.image_asset_id,
            width_px=page.width_px,
            height_px=page.height_px,
            render_dpi=page.render_dpi,
            display_label=page.display_label,
            created_at=page.created_at,
        )

    def _lyric_source_document_response(
        self, session: Session, document: LyricSourceDocument
    ) -> LyricSourceDocumentResponse:
        pages = list(
            session.scalars(
                select(LyricSourcePage)
                .where(LyricSourcePage.document_id == document.id)
                .order_by(LyricSourcePage.physical_page_number, LyricSourcePage.id)
            )
        )
        return LyricSourceDocumentResponse(
            id=document.id,
            title=document.title,
            source_kind=document.source_kind,
            edition=document.edition,
            publisher=document.publisher,
            published_year=document.published_year,
            document_asset_id=document.document_asset_id,
            source_ref=document.source_ref,
            rights_note=document.rights_note,
            pages=[self._lyric_source_page_response(page) for page in pages],
            created_at=document.created_at,
            updated_at=document.updated_at,
        )

    @staticmethod
    def _lyric_source_images_for_owner(
        session: Session, owner_type: str, owner_id: str
    ) -> list[LyricSourceImageResponse]:
        owner_column = {
            "work": LyricSourceLink.work_id,
            "score": LyricSourceLink.score_id,
            "rendition": LyricSourceLink.rendition_id,
        }[owner_type]
        rows = session.execute(
            select(LyricSourceLink, LyricSourcePage, LyricSourceDocument)
            .join(LyricSourcePage, LyricSourcePage.id == LyricSourceLink.source_page_id)
            .join(
                LyricSourceDocument,
                LyricSourceDocument.id == LyricSourcePage.document_id,
            )
            .where(owner_column == owner_id)
            .order_by(
                LyricSourceLink.display_order,
                LyricSourcePage.physical_page_number,
                LyricSourceLink.id,
            )
        ).all()
        return [
            LyricSourceImageResponse(
                link_id=link.id,
                source_page_id=page.id,
                image_asset_id=page.image_asset_id,
                document_id=document.id,
                document_title=document.title,
                source_kind=document.source_kind,
                source_ref=document.source_ref,
                physical_page_number=page.physical_page_number,
                display_label=page.display_label,
                display_order=link.display_order,
                width_px=page.width_px,
                height_px=page.height_px,
                render_dpi=page.render_dpi,
                owner_type=owner_type,
                owner_id=owner_id,
                language_relations=link.language_relations,
                note=link.note,
            )
            for link, page, document in rows
        ]

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
            lyrics=work.lyrics,
            lyrics_language=work.lyrics_language,
            lyrics_translations=work.lyrics_translations,
            lyrics_source_images=self._lyric_source_images_for_owner(session, "work", work.id),
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

    def _score_response(self, session: Session, score: Score) -> ScoreResponse:
        return ScoreResponse(
            id=score.id,
            arrangement_id=score.arrangement_id,
            label=score.label,
            origin=score.origin,
            derived_from_revision_id=score.derived_from_revision_id,
            head_revision_id=score.head_revision_id,
            published_revision_id=score.published_revision_id,
            lyrics=score.lyrics,
            lyrics_language=score.lyrics_language,
            lyrics_translations=score.lyrics_translations,
            lyrics_source_images=self._lyric_source_images_for_owner(session, "score", score.id),
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
            lyrics=rendition.lyrics,
            lyrics_language=rendition.lyrics_language,
            lyrics_translations=rendition.lyrics_translations,
            lyrics_source_images=self._lyric_source_images_for_owner(
                session, "rendition", rendition.id
            ),
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

    def _asset_delivery_response(self, session: Session, asset: Asset) -> AssetDeliveryResponse:
        if asset.state != "ready":
            raise V2Conflict("asset is not ready")
        locations = list(
            session.scalars(
                select(AssetLocation)
                .where(
                    AssetLocation.asset_id == asset.id,
                    AssetLocation.state == "available",
                )
                .order_by(AssetLocation.provider)
            )
        )
        if not locations:
            raise V2NotFound("asset has no available content")
        cos_location = next((item for item in locations if item.provider == "cos"), None)
        if cos_location is not None and self.settings.cos_secret_id:
            bucket, _, key = cos_location.storage_key.partition("/")
            if not bucket or not key:
                raise V2Conflict("COS location has an invalid storage key")
            url, expires_at = presign_cos_get(
                bucket=bucket,
                region=self.settings.cos_region,
                key=key,
                secret_id=self.settings.cos_secret_id,
                secret_key=self.settings.cos_secret_key,
                expires_seconds=self.settings.cos_presign_expires_seconds,
            )
            return AssetDeliveryResponse(
                asset_id=asset.id,
                media_type=asset.detected_media_type,
                byte_size=asset.byte_size,
                sha256=asset.sha256,
                delivery="signed_url",
                url=url,
                cache_key=f"rhythm:asset:{asset.id}:{asset.sha256}",
                etag=f'"sha256:{asset.sha256}"',
                supports_range=True,
                expires_at=expires_at,
            )
        local_location = next((item for item in locations if item.provider == "local"), None)
        if local_location is None:
            raise V2Conflict("configured storage provider cannot issue a delivery URL yet")
        return AssetDeliveryResponse(
            asset_id=asset.id,
            media_type=asset.detected_media_type,
            byte_size=asset.byte_size,
            sha256=asset.sha256,
            delivery="authenticated_url",
            url=f"/v2/assets/{asset.id}/content",
            cache_key=f"rhythm:asset:{asset.id}:{asset.sha256}",
            etag=f'"sha256:{asset.sha256}"',
            supports_range=True,
        )

    def _library_song_statement(self):
        deliverable_providers = ["local"]
        if self.settings.cos_secret_id and self.settings.cos_secret_key:
            deliverable_providers.append("cos")
        return (
            select(ReleaseItem, Release, Rendition, Arrangement, Work)
            .join(Release, Release.id == ReleaseItem.release_id)
            .join(Rendition, Rendition.id == ReleaseItem.rendition_id)
            .join(Arrangement, Arrangement.id == Rendition.arrangement_id)
            .join(Work, Work.id == Arrangement.work_id)
            .where(
                Release.deleted_at.is_(None),
                Rendition.deleted_at.is_(None),
                Arrangement.deleted_at.is_(None),
                Work.deleted_at.is_(None),
                select(RenditionAsset.id)
                .join(Asset, Asset.id == RenditionAsset.asset_id)
                .where(
                    RenditionAsset.rendition_id == Rendition.id,
                    RenditionAsset.role.in_(PLAYBACK_AUDIO_ROLES),
                    Asset.state == "ready",
                    Asset.deleted_at.is_(None),
                    Asset.detected_media_type.in_(PLAYABLE_AUDIO_MEDIA_TYPES),
                    select(AssetLocation.id)
                    .where(
                        AssetLocation.asset_id == Asset.id,
                        AssetLocation.state == "available",
                        AssetLocation.provider.in_(deliverable_providers),
                    )
                    .exists(),
                )
                .exists(),
            )
        )

    def _library_song_response(
        self,
        session: Session,
        item: ReleaseItem,
        release: Release,
        rendition: Rendition,
        arrangement: Arrangement,
        work: Work,
    ) -> LibrarySongResponse:
        artist = session.scalar(
            select(Contributor.display_name)
            .join(RenditionCredit, RenditionCredit.contributor_id == Contributor.id)
            .where(RenditionCredit.rendition_id == rendition.id)
            .order_by(RenditionCredit.position, RenditionCredit.id)
            .limit(1)
        )
        if artist is None:
            artist = session.scalar(
                select(Contributor.display_name)
                .join(WorkCredit, WorkCredit.contributor_id == Contributor.id)
                .where(WorkCredit.work_id == work.id)
                .order_by(
                    case((WorkCredit.role == "composer", 0), else_=1),
                    WorkCredit.position,
                    WorkCredit.id,
                )
                .limit(1)
            )
        cover_asset_id = (
            rendition.cover_asset_id
            or release.cover_asset_id
            or arrangement.cover_asset_id
            or work.cover_asset_id
        )
        cover_delivery = self._cover_delivery(session, cover_asset_id)
        score: Score | None = None
        if arrangement.preferred_score_id:
            score = session.scalar(
                select(Score).where(
                    Score.id == arrangement.preferred_score_id,
                    Score.published_revision_id.is_not(None),
                    Score.deleted_at.is_(None),
                )
            )
        lyric_sources = [
            (
                rendition.lyrics,
                rendition.lyrics_language,
                rendition.lyrics_translations,
            ),
            *(
                [(score.lyrics, score.lyrics_language, score.lyrics_translations)]
                if score is not None
                else []
            ),
            (work.lyrics, work.lyrics_language, work.lyrics_translations),
        ]
        lyrics, lyrics_language, lyrics_translations = merge_lyrics_sources(lyric_sources)
        rendition_formats = _rendition_format_map(rendition)
        resolved_formats: dict[str, tuple[str, str]] = {}
        for source_index, (source_lyrics, source_language, source_translations) in enumerate(
            lyric_sources
        ):
            source_entries = (
                ([{"language": source_language, "lyrics": source_lyrics}] if source_lyrics else [])
                + list(source_translations or [])
            )
            for entry in source_entries:
                entry_language = normalize_language_tag(entry["language"])
                folded = entry_language.casefold()
                if folded in resolved_formats or not entry["lyrics"]:
                    continue
                stored_format = rendition_formats.get(folded) if source_index == 0 else None
                resolved_formats[folded] = (
                    entry_language,
                    stored_format or _detect_lyric_format(entry["lyrics"]),
                )
        effective_languages = [
            *([lyrics_language] if lyrics_language is not None else []),
            *[item["language"] for item in lyrics_translations],
        ]
        lyrics_formats = [
            LyricLanguageFormat(
                language=language,
                format=resolved_formats[language.casefold()][1],
            )
            for language in effective_languages
        ]
        source_images: list[LyricSourceImageResponse] = []
        seen_source_pages: set[str] = set()
        source_owners = [("rendition", rendition.id)]
        if score is not None:
            source_owners.append(("score", score.id))
        source_owners.append(("work", work.id))
        for owner_type, owner_id in source_owners:
            for source_image in self._lyric_source_images_for_owner(session, owner_type, owner_id):
                if source_image.source_page_id not in seen_source_pages:
                    seen_source_pages.add(source_image.source_page_id)
                    source_images.append(source_image)
        return LibrarySongResponse(
            work_id=work.id,
            arrangement_id=arrangement.id,
            rendition_id=rendition.id,
            rendition_revision=rendition.revision,
            album_id=release.id,
            title=rendition.label,
            artist=artist or release.album_artist,
            album_title=release.title,
            duration_ms=rendition.duration_ms,
            track_no=item.track_no,
            cover_asset_id=cover_delivery.asset_id if cover_delivery else None,
            cover_url=cover_delivery.url if cover_delivery else None,
            lyrics=lyrics,
            lyrics_language=lyrics_language or "und",
            lyrics_translations=lyrics_translations,
            lyrics_source_images=source_images,
            lyric_source_count=len(source_images),
            lyrics_formats=lyrics_formats,
        )

    def _library_album_response(self, session: Session, release: Release) -> LibraryAlbumResponse:
        deliverable_providers = ["local"]
        if self.settings.cos_secret_id and self.settings.cos_secret_key:
            deliverable_providers.append("cos")
        song_count = session.scalar(
            select(func.count(ReleaseItem.id))
            .join(Rendition, Rendition.id == ReleaseItem.rendition_id)
            .join(Arrangement, Arrangement.id == Rendition.arrangement_id)
            .join(Work, Work.id == Arrangement.work_id)
            .where(
                ReleaseItem.release_id == release.id,
                Rendition.deleted_at.is_(None),
                Arrangement.deleted_at.is_(None),
                Work.deleted_at.is_(None),
                select(RenditionAsset.id)
                .join(Asset, Asset.id == RenditionAsset.asset_id)
                .where(
                    RenditionAsset.rendition_id == Rendition.id,
                    RenditionAsset.role.in_(PLAYBACK_AUDIO_ROLES),
                    Asset.state == "ready",
                    Asset.deleted_at.is_(None),
                    Asset.detected_media_type.in_(PLAYABLE_AUDIO_MEDIA_TYPES),
                    select(AssetLocation.id)
                    .where(
                        AssetLocation.asset_id == Asset.id,
                        AssetLocation.state == "available",
                        AssetLocation.provider.in_(deliverable_providers),
                    )
                    .exists(),
                )
                .exists(),
            )
        )
        cover_delivery = self._cover_delivery(session, release.cover_asset_id)
        return LibraryAlbumResponse(
            id=release.id,
            key=release.key,
            title=release.title,
            artist=release.album_artist,
            cover_asset_id=cover_delivery.asset_id if cover_delivery else None,
            cover_url=cover_delivery.url if cover_delivery else None,
            song_count=song_count or 0,
        )

    def _cover_delivery(
        self, session: Session, asset_id: str | None
    ) -> AssetDeliveryResponse | None:
        if asset_id is None:
            return None
        asset = session.get(Asset, asset_id)
        if (
            asset is None
            or asset.state != "ready"
            or asset.deleted_at is not None
            or not asset.detected_media_type.startswith("image/")
        ):
            return None
        try:
            return self._asset_delivery_response(session, asset)
        except (V2Conflict, V2NotFound):
            return None

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
        if normalized == "application/pdf" or suffix == ".pdf":
            return self.settings.max_source_document_bytes
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


def encode_song_cursor(release_key: str, display_order: int, item_id: str) -> str:
    payload = json.dumps([release_key, display_order, item_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_song_cursor(cursor: str) -> tuple[str, int, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not isinstance(value[0], str)
            or not isinstance(value[1], int)
            or not isinstance(value[2], str)
        ):
            raise ValueError
        return value[0], value[1], value[2]
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise V2DomainError("invalid song cursor") from error


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


def _aware_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
        media_type and media_type.split(";", 1)[0].strip().lower() in PLAYABLE_AUDIO_MEDIA_TYPES
    )
