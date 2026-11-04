"""issue 154: administrators publish announcements; enrolled devices only read published ones.

Invariants:
- Devices never see a draft or archived announcement, and the list and detail filter on one status.
- Every state change checks ``revision``, so a double submit becomes a 412 and not a second transition.
- A body may only reference images recorded in ``v2_announcement_images``.

Why: announcements reach every device, and the database and COS share no transaction.

Accepted residuals:
- COS is written before the image row. A failed insert leaves an invisible object; it is harmless, and
  uploading the same bytes again reuses the same content-addressed key.
- Deleting an announcement or replacing an image keeps the object and its row. The content-addressed URL
  stays reachable to anyone who already has it, and a later cleanup job can remove unreferenced objects.
- Publishing again after archiving keeps the first ``published_at`` so a correction does not look new.

Silent failure if broken: dropping the status filter would leak drafts, and writing the image row first
would publish a broken image. Rejected: clients uploading directly to COS, which needs bucket CORS and is
only worth it for video, which is out of scope.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError

from rhythm_metadata_api.application.catalog_service import etag
from rhythm_metadata_api.application.music_images import MusicImageArchiveError, _detected_type
from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.domain.v2.announcements import (
    IMAGE_NAME,
    IMAGE_PATH_PREFIX,
    SCHEMA_VERSION,
    AdminAnnouncementDetail,
    AdminAnnouncementListItem,
    AdminAnnouncementListResponse,
    AnnouncementDetail,
    AnnouncementImageResponse,
    AnnouncementListItem,
    AnnouncementListResponse,
    AnnouncementPatch,
    AnnouncementWrite,
    body_excerpt,
    body_has_content,
    body_image_paths,
)
from rhythm_metadata_api.domain.v2.errors import (
    StaleRevision,
    V2Conflict,
    V2DomainError,
    V2NotFound,
    V2Unavailable,
)
from rhythm_metadata_api.infrastructure.db.models import Announcement, AnnouncementImage, utc_now
from rhythm_metadata_api.infrastructure.storage.cos_presign import presign_cos_get, presign_cos_put

LOGGER = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10_000_000
_ADMIN_LIST_LIMIT = 200
_IMAGE_URL_TTL_SECONDS = 600


class AnnouncementImageTooLarge(V2DomainError):
    status_code = 413
    problem_type = "announcement-image-too-large"
    title = "Announcement image is too large"


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _encode_cursor(published_at: datetime, announcement_id: str) -> str:
    raw = json.dumps([published_at.isoformat(), announcement_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        published_at, announcement_id = json.loads(raw)
        return datetime.fromisoformat(published_at), str(announcement_id)
    except (binascii.Error, ValueError, TypeError) as error:
        raise V2DomainError("announcement cursor is invalid") from error


class AnnouncementService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.uow_factory = uow_factory
        self.settings = settings
        self._now = now

    # ---- device reads ----

    def list_published(self, *, limit: int, cursor: str | None) -> AnnouncementListResponse:
        statement = select(Announcement).where(Announcement.status == "published")
        if cursor:
            published_at, announcement_id = _decode_cursor(cursor)
            statement = statement.where(or_(
                Announcement.published_at < published_at,
                and_(Announcement.published_at == published_at, Announcement.id < announcement_id),
            ))
        statement = statement.order_by(Announcement.published_at.desc(), Announcement.id.desc()).limit(limit + 1)
        with self.uow_factory() as uow:
            rows = list(uow.session.scalars(statement))
            page = rows[:limit]
            next_cursor = _encode_cursor(page[-1].published_at, page[-1].id) if len(rows) > limit else None
            return AnnouncementListResponse(items=[self._list_item(row) for row in page], next_cursor=next_cursor)

    def get_published(self, announcement_id: str) -> AnnouncementDetail:
        with self.uow_factory() as uow:
            row = uow.session.get(Announcement, announcement_id)
            if row is None or row.status != "published":
                raise V2NotFound("announcement not found")
            return AnnouncementDetail(
                **self._list_item(row).model_dump(),
                schema_version=row.schema_version, body=row.body, updated_at=_utc(row.updated_at),
            )

    # ---- administration ----

    def list_admin(self, *, status: str | None) -> AdminAnnouncementListResponse:
        statement = select(Announcement)
        if status:
            statement = statement.where(Announcement.status == status)
        statement = statement.order_by(Announcement.updated_at.desc(), Announcement.id.desc()).limit(_ADMIN_LIST_LIMIT)
        with self.uow_factory() as uow:
            return AdminAnnouncementListResponse(
                items=[self._admin_item(row) for row in uow.session.scalars(statement)]
            )

    def get_admin(self, announcement_id: str) -> AdminAnnouncementDetail:
        with self.uow_factory() as uow:
            return self._admin_detail(self._required(uow.session, announcement_id))

    def create(self, request: AnnouncementWrite, actor_id: str) -> AdminAnnouncementDetail:
        now = self._now()
        with self.uow_factory() as uow:
            self._require_images(uow.session, request.body)
            row = Announcement(
                title=request.title, summary=request.summary, importance=request.importance, body=request.body,
                schema_version=SCHEMA_VERSION, status="draft", revision=1, created_by=actor_id,
                created_at=now, updated_at=now,
            )
            uow.session.add(row)
            uow.session.flush()
            uow.session.flush()
            LOGGER.info("announcement created: id=%s actor=%s", row.id, actor_id)
            return self._admin_detail(row)

    def update(self, announcement_id: str, request: AnnouncementPatch, actor_id: str) -> AdminAnnouncementDetail:
        with self.uow_factory() as uow:
            row = self._required(uow.session, announcement_id)
            self._check_revision(row, request.revision)
            self._require_images(uow.session, request.body)
            if row.status == "published":
                self._require_publishable(request.body)
            row.title = request.title
            row.summary = request.summary
            row.importance = request.importance
            row.body = request.body
            row.schema_version = SCHEMA_VERSION
            self._touch(row)
            uow.session.flush()
            LOGGER.info("announcement updated: id=%s revision=%s actor=%s", row.id, row.revision, actor_id)
            return self._admin_detail(row)

    def publish(self, announcement_id: str, revision: int, actor_id: str) -> AdminAnnouncementDetail:
        with self.uow_factory() as uow:
            row = self._required(uow.session, announcement_id)
            self._check_revision(row, revision)
            if row.status == "published":
                raise V2Conflict("announcement is already published")
            self._require_publishable(row.body)
            self._require_images(uow.session, row.body)
            row.status = "published"
            row.published_at = row.published_at or self._now()
            self._touch(row)
            uow.session.flush()
            LOGGER.info("announcement published: id=%s revision=%s actor=%s", row.id, row.revision, actor_id)
            return self._admin_detail(row)

    def archive(self, announcement_id: str, revision: int, actor_id: str) -> AdminAnnouncementDetail:
        with self.uow_factory() as uow:
            row = self._required(uow.session, announcement_id)
            self._check_revision(row, revision)
            if row.status != "published":
                raise V2Conflict("only published announcements can be archived")
            row.status = "archived"
            self._touch(row)
            uow.session.flush()
            LOGGER.info("announcement archived: id=%s revision=%s actor=%s", row.id, row.revision, actor_id)
            return self._admin_detail(row)

    def delete(self, announcement_id: str, revision: int, actor_id: str) -> None:
        with self.uow_factory() as uow:
            row = self._required(uow.session, announcement_id)
            self._check_revision(row, revision)
            if row.status == "published":
                raise V2Conflict("archive a published announcement before deleting it")
            uow.session.execute(delete(Announcement).where(Announcement.id == announcement_id))
            uow.session.flush()
            LOGGER.info("announcement deleted: id=%s actor=%s", announcement_id, actor_id)

    # ---- images ----

    def upload_image(self, body: bytes, declared_type: str, actor_id: str) -> AnnouncementImageResponse:
        if len(body) > MAX_IMAGE_BYTES:
            raise AnnouncementImageTooLarge("announcement image must not exceed 10 MB")
        if not body:
            raise V2DomainError("announcement image is empty")
        try:
            media_type, suffix = _detected_type(body)
        except MusicImageArchiveError as error:
            raise V2DomainError("announcement image must be JPEG, PNG, WebP or GIF") from error
        if declared_type.split(";", 1)[0].strip().lower() != media_type:
            raise V2DomainError("announcement image type does not match its bytes")
        self._require_storage()
        name = f"{hashlib.sha256(body).hexdigest()}.{suffix}"
        with self.uow_factory() as uow:
            existing = uow.session.get(AnnouncementImage, name)
            if existing is not None:
                return self._image_response(existing)
        self._put_object(self._object_key(name), body, media_type)
        try:
            with self.uow_factory() as uow:
                row = AnnouncementImage(
                    name=name, media_type=media_type, byte_size=len(body),
                    uploaded_by=actor_id, created_at=self._now(),
                )
                uow.session.add(row)
                uow.session.flush()
                LOGGER.info("announcement image stored: name=%s bytes=%s actor=%s", name, len(body), actor_id)
                return self._image_response(row)
        except IntegrityError:
            # A concurrent upload of identical bytes wrote the same content-addressed object.
            with self.uow_factory() as uow:
                return self._image_response(uow.session.get_one(AnnouncementImage, name))

    def image_url(self, name: str) -> str:
        if not IMAGE_NAME.fullmatch(name):
            raise V2NotFound("announcement image not found")
        with self.uow_factory() as uow:
            if uow.session.get(AnnouncementImage, name) is None:
                raise V2NotFound("announcement image not found")
        self._require_storage()
        url, _ = presign_cos_get(
            self.settings.music_image_cos_bucket, self.settings.cos_region, self._object_key(name),
            self.settings.cos_secret_id, self.settings.cos_secret_key,
            expires_seconds=_IMAGE_URL_TTL_SECONDS,
        )
        return url

    def _put_object(self, key: str, body: bytes, media_type: str) -> None:
        url, _ = presign_cos_put(
            self.settings.music_image_cos_bucket, self.settings.cos_region, key,
            self.settings.cos_secret_id, self.settings.cos_secret_key,
            headers={"Content-Type": media_type},
        )
        try:
            with urlopen(
                Request(url, data=body, method="PUT", headers={"Content-Type": media_type}), timeout=30,
            ) as response:
                if response.status not in {200, 201, 204}:
                    raise V2Unavailable("announcement image storage rejected the upload")
        except (HTTPError, URLError, TimeoutError) as error:
            LOGGER.warning("announcement image upload failed: key=%s error=%s", key, type(error).__name__)
            raise V2Unavailable("announcement image storage is unavailable") from error

    def _require_storage(self) -> None:
        settings = self.settings
        if not (settings.music_image_cos_bucket and settings.cos_secret_id and settings.cos_secret_key):
            LOGGER.warning("announcement image storage is not configured")
            raise V2Unavailable("announcement image storage is not configured")

    @staticmethod
    def _object_key(name: str) -> str:
        return f"announcement-images/{name[:2]}/{name}"

    @staticmethod
    def _image_response(row: AnnouncementImage) -> AnnouncementImageResponse:
        return AnnouncementImageResponse(
            name=row.name, path=IMAGE_PATH_PREFIX + row.name, media_type=row.media_type, byte_size=row.byte_size,
        )

    # ---- helpers ----

    @staticmethod
    def _required(session, announcement_id: str) -> Announcement:
        row = session.get(Announcement, announcement_id)
        if row is None:
            raise V2NotFound("announcement not found")
        return row

    @staticmethod
    def _check_revision(row: Announcement, revision: int) -> None:
        if row.revision != revision:
            raise StaleRevision(etag(revision), etag(row.revision))

    def _touch(self, row: Announcement) -> None:
        row.revision += 1
        row.updated_at = self._now()

    @staticmethod
    def _require_publishable(body: dict) -> None:
        if not body_has_content(body):
            raise V2DomainError("announcement body must not be empty when published")

    @staticmethod
    def _require_images(session, body: dict) -> None:
        names = {path.removeprefix(IMAGE_PATH_PREFIX) for path in body_image_paths(body)}
        if not names:
            return
        known = set(session.scalars(select(AnnouncementImage.name).where(AnnouncementImage.name.in_(names))))
        if names - known:
            raise V2DomainError("announcement references an image that was not uploaded")

    @staticmethod
    def _list_item(row: Announcement) -> AnnouncementListItem:
        images = body_image_paths(row.body)
        return AnnouncementListItem(
            id=row.id, title=row.title, summary=row.summary or body_excerpt(row.body), importance=row.importance,
            published_at=_utc(row.published_at), cover_image=images[0] if images else None, revision=row.revision,
        )

    @staticmethod
    def _admin_item(row: Announcement) -> AdminAnnouncementListItem:
        return AdminAnnouncementListItem(
            id=row.id, title=row.title, summary=row.summary, importance=row.importance, status=row.status,
            published_at=_utc(row.published_at) if row.published_at else None, revision=row.revision,
            created_by=row.created_by, created_at=_utc(row.created_at), updated_at=_utc(row.updated_at),
        )

    def _admin_detail(self, row: Announcement) -> AdminAnnouncementDetail:
        return AdminAnnouncementDetail(
            **self._admin_item(row).model_dump(), schema_version=row.schema_version, body=row.body,
        )
