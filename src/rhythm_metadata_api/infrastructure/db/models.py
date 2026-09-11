from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RevisionedMixin:
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Work(RevisionedMixin, Base):
    __tablename__ = "v2_works"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    canonical_title: Mapped[str] = mapped_column(String(500), nullable=False)
    language: Mapped[str | None] = mapped_column(String(35), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # issue 9: 封面/歌词回退链的末级来源（封面 song→album→work；歌词 song→乐谱→work）
    cover_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="SET NULL"), nullable=True
    )
    lyrics: Mapped[str | None] = mapped_column(Text, nullable=True)
    # issue 32: lyrics 保持为默认语言文本，其他语言以结构化 JSON 数组保存。
    lyrics_language: Mapped[str] = mapped_column(
        String(35), nullable=False, default="und", server_default="und"
    )
    lyrics_translations: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'active', 'archived')", name="work_status"),
        Index("v2_works_title_idx", "canonical_title"),
    )


class WorkAlias(Base):
    __tablename__ = "v2_work_aliases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_id: Mapped[str] = mapped_column(
        ForeignKey("v2_works.id", ondelete="CASCADE"), nullable=False
    )
    namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    external_id: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint("namespace", "external_id", name="uq_v2_work_alias"),
        Index("v2_work_aliases_work_idx", "work_id"),
    )


class Contributor(RevisionedMixin, Base):
    __tablename__ = "v2_contributors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    sort_name: Mapped[str | None] = mapped_column(String(500), nullable=True)


class WorkCredit(Base):
    __tablename__ = "v2_work_credits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_id: Mapped[str] = mapped_column(
        ForeignKey("v2_works.id", ondelete="CASCADE"), nullable=False
    )
    contributor_id: Mapped[str] = mapped_column(
        ForeignKey("v2_contributors.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("work_id", "contributor_id", "role", name="uq_v2_work_credit"),
    )


class Arrangement(RevisionedMixin, Base):
    __tablename__ = "v2_arrangements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_id: Mapped[str] = mapped_column(
        ForeignKey("v2_works.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    voicing: Mapped[str | None] = mapped_column(String(100), nullable=True)
    key_signature: Mapped[str | None] = mapped_column(String(100), nullable=True)
    based_on_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_arrangements.id", ondelete="SET NULL"), nullable=True
    )
    preferred_score_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_scores.id", ondelete="SET NULL"), nullable=True
    )
    # issue 9: Arrangement 充当"专辑"，封面回退链的中级来源（song→album→work）
    cover_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (Index("v2_arrangements_work_idx", "work_id"),)


class Part(Base):
    __tablename__ = "v2_parts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    arrangement_id: Mapped[str] = mapped_column(
        ForeignKey("v2_arrangements.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    midi_channel: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("arrangement_id", "code", name="uq_v2_part_code"),
        CheckConstraint(
            "midi_channel IS NULL OR (midi_channel >= 1 AND midi_channel <= 16)",
            name="part_midi_channel",
        ),
    )


class Score(RevisionedMixin, Base):
    __tablename__ = "v2_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    arrangement_id: Mapped[str] = mapped_column(
        ForeignKey("v2_arrangements.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    origin: Mapped[str] = mapped_column(String(50), nullable=False)
    derived_from_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_score_revisions.id", ondelete="SET NULL"), nullable=True
    )
    head_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_score_revisions.id", ondelete="SET NULL"), nullable=True
    )
    published_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_score_revisions.id", ondelete="SET NULL"), nullable=True
    )
    # issue 9: 乐谱天生带词，是歌词回退链的中级来源（song→乐谱→work）
    lyrics: Mapped[str | None] = mapped_column(Text, nullable=True)
    lyrics_language: Mapped[str] = mapped_column(
        String(35), nullable=False, default="und", server_default="und"
    )
    lyrics_translations: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )

    __table_args__ = (
        CheckConstraint(
            "origin IN ('ocr', 'midi_transcription', 'manual', 'external_import')",
            name="score_origin",
        ),
        Index("v2_scores_arrangement_idx", "arrangement_id"),
    )


class ScoreRevision(Base):
    __tablename__ = "v2_score_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    score_id: Mapped[str] = mapped_column(
        ForeignKey("v2_scores.id", ondelete="CASCADE"), nullable=False
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    based_on_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_score_revisions.id", ondelete="RESTRICT"), nullable=True
    )
    edit_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    editor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint("score_id", "revision_no", name="uq_v2_score_revision_no"),
        Index("v2_score_revisions_score_idx", "score_id"),
    )


class Asset(Base):
    __tablename__ = "v2_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    detected_media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("state IN ('pending_inspection', 'ready', 'rejected')", name="asset_state"),
    )


class AssetLocation(Base):
    __tablename__ = "v2_asset_locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint("provider", "storage_key", name="uq_v2_asset_location"),
        UniqueConstraint("asset_id", "provider", name="uq_v2_asset_provider"),
        CheckConstraint(
            "state IN ('available', 'migrating', 'missing')", name="asset_location_state"
        ),
    )


class AssetSource(Base):
    __tablename__ = "v2_asset_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, default="upload")
    source_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LyricSourceDocument(Base):
    __tablename__ = "v2_lyric_source_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    edition: Mapped[str | None] = mapped_column(String(500), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(500), nullable=True)
    published_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="SET NULL"), nullable=True
    )
    source_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    rights_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('pdf', 'scan', 'photo', 'booklet', 'web')",
            name="lyric_source_document_kind",
        ),
    )


class LyricSourcePage(Base):
    __tablename__ = "v2_lyric_source_pages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("v2_lyric_source_documents.id", ondelete="CASCADE"), nullable=False
    )
    physical_page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    image_asset_id: Mapped[str] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="RESTRICT"), nullable=False
    )
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    render_dpi: Mapped[int] = mapped_column(Integer, nullable=False)
    display_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint(
            "document_id", "physical_page_number", name="uq_v2_lyric_source_page_number"
        ),
        CheckConstraint("physical_page_number >= 1", name="lyric_source_page_number"),
        CheckConstraint(
            "width_px > 0 AND height_px > 0 AND render_dpi > 0",
            name="lyric_source_page_dimensions",
        ),
        Index("v2_lyric_source_pages_document_idx", "document_id"),
    )


class LyricSourceLink(Base):
    __tablename__ = "v2_lyric_source_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_works.id", ondelete="CASCADE"), nullable=True
    )
    score_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_scores.id", ondelete="CASCADE"), nullable=True
    )
    rendition_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_renditions.id", ondelete="CASCADE"), nullable=True
    )
    source_page_id: Mapped[str] = mapped_column(
        ForeignKey("v2_lyric_source_pages.id", ondelete="CASCADE"), nullable=False
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    language_relations: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        CheckConstraint(
            "(CASE WHEN work_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN score_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN rendition_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="lyric_source_link_one_owner",
        ),
        CheckConstraint("display_order >= 1", name="lyric_source_link_display_order"),
        Index(
            "uq_v2_lyric_source_link_work_page",
            "work_id",
            "source_page_id",
            unique=True,
            sqlite_where=text("work_id IS NOT NULL"),
        ),
        Index(
            "uq_v2_lyric_source_link_score_page",
            "score_id",
            "source_page_id",
            unique=True,
            sqlite_where=text("score_id IS NOT NULL"),
        ),
        Index(
            "uq_v2_lyric_source_link_rendition_page",
            "rendition_id",
            "source_page_id",
            unique=True,
            sqlite_where=text("rendition_id IS NOT NULL"),
        ),
        Index("v2_lyric_source_links_work_idx", "work_id", "display_order"),
        Index("v2_lyric_source_links_score_idx", "score_id", "display_order"),
        Index("v2_lyric_source_links_rendition_idx", "rendition_id", "display_order"),
    )


class ScoreRevisionAsset(Base):
    __tablename__ = "v2_score_revision_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    score_revision_id: Mapped[str] = mapped_column(
        ForeignKey("v2_score_revisions.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "score_revision_id", "asset_id", "role", name="uq_v2_score_revision_asset"
        ),
        Index(
            "uq_v2_score_primary_musicxml",
            "score_revision_id",
            unique=True,
            sqlite_where=text("role = 'primary_musicxml'"),
        ),
    )


class Rendition(RevisionedMixin, Base):
    __tablename__ = "v2_renditions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    arrangement_id: Mapped[str] = mapped_column(
        ForeignKey("v2_arrangements.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(500), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    ensemble: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recorded_at: Mapped[str | None] = mapped_column(String(10), nullable=True)
    location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # issue 9: Rendition 即"歌曲(MP3)"，封面/歌词回退链的首选来源
    cover_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="SET NULL"), nullable=True
    )
    lyrics: Mapped[str | None] = mapped_column(Text, nullable=True)
    lyrics_language: Mapped[str] = mapped_column(
        String(35), nullable=False, default="und", server_default="und"
    )
    lyrics_translations: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    lyrics_formats: Mapped[dict[str, str]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )

    __table_args__ = (Index("v2_renditions_arrangement_idx", "arrangement_id"),)


class RenditionCredit(Base):
    __tablename__ = "v2_rendition_credits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rendition_id: Mapped[str] = mapped_column(
        ForeignKey("v2_renditions.id", ondelete="CASCADE"), nullable=False
    )
    contributor_id: Mapped[str] = mapped_column(
        ForeignKey("v2_contributors.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("rendition_id", "contributor_id", "role", name="uq_v2_rendition_credit"),
    )


class RenditionAsset(Base):
    __tablename__ = "v2_rendition_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rendition_id: Mapped[str] = mapped_column(
        ForeignKey("v2_renditions.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    part_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_parts.id", ondelete="RESTRICT"), nullable=True
    )
    codec_profile: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "rendition_id",
            "asset_id",
            "role",
            "part_id",
            name="uq_v2_rendition_asset",
        ),
        CheckConstraint(
            "(role = 'stem' AND part_id IS NOT NULL) OR (role <> 'stem' AND part_id IS NULL)",
            name="rendition_asset_part_role",
        ),
    )


class ChorusProject(RevisionedMixin, Base):
    __tablename__ = "v2_chorus_projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_id: Mapped[str] = mapped_column(
        ForeignKey("v2_works.id", ondelete="CASCADE"), nullable=False
    )
    arrangement_id: Mapped[str] = mapped_column(
        ForeignKey("v2_arrangements.id", ondelete="CASCADE"), nullable=False
    )
    alignment_score_revision_id: Mapped[str] = mapped_column(
        ForeignKey("v2_score_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    timeline_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    created_by_user_id: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'open', 'closed', 'archived')",
            name="chorus_project_status",
        ),
        Index("v2_chorus_projects_work_idx", "work_id", "status"),
        Index("v2_chorus_projects_arrangement_idx", "arrangement_id"),
    )


class ChorusTrack(RevisionedMixin, Base):
    __tablename__ = "v2_chorus_tracks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chorus_project_id: Mapped[str] = mapped_column(
        ForeignKey("v2_chorus_projects.id", ondelete="CASCADE"), nullable=False
    )
    rendition_id: Mapped[str] = mapped_column(
        ForeignKey("v2_renditions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    uploader_user_id: Mapped[str] = mapped_column(
        ForeignKey("auth_users.id", ondelete="RESTRICT"), nullable=False
    )
    part_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_parts.id", ondelete="RESTRICT"), nullable=True
    )
    upload_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_upload_sessions.id", ondelete="SET NULL"), nullable=True
    )
    contribution_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    display_label: Mapped[str] = mapped_column(String(300), nullable=False)
    take_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    alignment_state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    alignment_offset_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    gain_millibels: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pan_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waveform_peaks: Mapped[list[float]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "contribution_kind IN ('vocal_part', 'harmony', 'guitar', 'piano', "
            "'percussion', 'other')",
            name="chorus_track_kind",
        ),
        CheckConstraint(
            "alignment_state IN ('pending', 'automatic', 'manual', 'verified', 'failed')",
            name="chorus_track_alignment_state",
        ),
        CheckConstraint(
            "status IN ('draft', 'processing', 'pending_review', 'published', "
            "'rejected', 'withdrawn', 'failed')",
            name="chorus_track_status",
        ),
        CheckConstraint("take_no >= 1", name="chorus_track_take_no"),
        CheckConstraint(
            "gain_millibels >= -2400 AND gain_millibels <= 1200",
            name="chorus_track_gain",
        ),
        CheckConstraint(
            "pan_milli >= -1000 AND pan_milli <= 1000",
            name="chorus_track_pan",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms > 0",
            name="chorus_track_duration",
        ),
        Index("v2_chorus_tracks_project_idx", "chorus_project_id", "status"),
        Index("v2_chorus_tracks_uploader_idx", "uploader_user_id", "status"),
    )


class ScoreRenditionSync(Base):
    __tablename__ = "v2_score_rendition_sync"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    score_revision_id: Mapped[str] = mapped_column(
        ForeignKey("v2_score_revisions.id", ondelete="CASCADE"), nullable=False
    )
    rendition_id: Mapped[str] = mapped_column(
        ForeignKey("v2_renditions.id", ondelete="CASCADE"), nullable=False
    )
    anchor_order: Mapped[int] = mapped_column(Integer, nullable=False)
    score_tick: Mapped[int] = mapped_column(Integer, nullable=False)
    media_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_milli: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "score_revision_id",
            "rendition_id",
            "anchor_order",
            name="uq_v2_score_rendition_sync_order",
        ),
        CheckConstraint("anchor_order >= 0", name="score_rendition_sync_order"),
        CheckConstraint("score_tick >= 0", name="score_rendition_sync_tick"),
        CheckConstraint("media_ms >= 0", name="score_rendition_sync_media"),
        CheckConstraint(
            "confidence_milli >= 0 AND confidence_milli <= 1000",
            name="score_rendition_sync_confidence",
        ),
        CheckConstraint(
            "source IN ('in_app_clock', 'automatic', 'manual')",
            name="score_rendition_sync_source",
        ),
        Index(
            "v2_score_rendition_sync_lookup_idx",
            "score_revision_id",
            "rendition_id",
            "anchor_order",
        ),
    )


class ChorusMixVariant(Base):
    __tablename__ = "v2_chorus_mix_variants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chorus_project_id: Mapped[str] = mapped_column(
        ForeignKey("v2_chorus_projects.id", ondelete="CASCADE"), nullable=False
    )
    selection_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_track_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    selected_track_count: Mapped[int] = mapped_column(Integer, nullable=False)
    mix_profile: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="SET NULL"), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "chorus_project_id",
            "selection_hash",
            "mix_profile",
            name="uq_v2_chorus_mix_selection",
        ),
        CheckConstraint("selected_track_count >= 1", name="chorus_mix_track_count"),
        CheckConstraint(
            "state IN ('queued', 'processing', 'ready', 'failed', 'obsolete')",
            name="chorus_mix_state",
        ),
        Index("v2_chorus_mix_project_idx", "chorus_project_id", "state"),
    )


class Release(RevisionedMixin, Base):
    """A client-visible album that may collect renditions from many works."""

    __tablename__ = "v2_releases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    album_artist: Mapped[str | None] = mapped_column(String(500), nullable=True)
    release_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    cover_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (Index("v2_releases_title_idx", "title"),)


class ReleaseItem(Base):
    __tablename__ = "v2_release_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("v2_releases.id", ondelete="CASCADE"), nullable=False
    )
    rendition_id: Mapped[str] = mapped_column(
        ForeignKey("v2_renditions.id", ondelete="CASCADE"), nullable=False
    )
    disc_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    track_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        UniqueConstraint("release_id", "rendition_id", name="uq_v2_release_rendition"),
        UniqueConstraint("release_id", "display_order", name="uq_v2_release_display_order"),
        CheckConstraint("disc_no >= 1", name="release_item_disc_no"),
        CheckConstraint("track_no IS NULL OR track_no >= 1", name="release_item_track_no"),
        CheckConstraint("display_order >= 1", name="release_item_display_order"),
        Index("v2_release_items_release_idx", "release_id", "display_order"),
    )


class UploadSession(Base):
    __tablename__ = "v2_upload_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    expected_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_size: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False, default="upload")
    source_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    temporary_key: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    actual_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("v2_assets.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('created', 'uploaded', 'completed', 'failed', 'expired')",
            name="upload_session_state",
        ),
    )


class ChangeEvent(Base):
    __tablename__ = "v2_change_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entity_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False, default="owner")
    device_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    tombstone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ChangeEventWork(Base):
    __tablename__ = "v2_change_event_works"

    event_sequence: Mapped[int] = mapped_column(
        ForeignKey("v2_change_events.sequence", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[str] = mapped_column(
        ForeignKey("v2_works.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("v2_change_event_works_work_idx", "work_id", "event_sequence"),)


class IdempotencyKey(Base):
    __tablename__ = "v2_idempotency_keys"

    actor_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    scope: Mapped[str] = mapped_column(String(300), primary_key=True)
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response_headers_json: Mapped[dict[str, str]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthUser(Base):
    __tablename__ = "auth_users"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    __table_args__ = (CheckConstraint("status IN ('active', 'disabled')", name="auth_user_status"),)


class DeviceInvite(Base):
    __tablename__ = "auth_device_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    issued_by: Mapped[str] = mapped_column(String(100), nullable=False)
    replace_existing_device: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (Index("auth_device_invites_user_idx", "user_id", "created_at"),)


class RegisteredDevice(Base):
    __tablename__ = "auth_devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[str] = mapped_column(String(200), nullable=False)
    signing_certificate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    public_key_spki: Mapped[str] = mapped_column(Text, nullable=False)
    public_key_thumbprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_algorithm: Mapped[str] = mapped_column(String(20), nullable=False, default="ES256")
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('active', 'revoked')", name="auth_device_status"),
        Index("auth_devices_user_idx", "user_id"),
        Index(
            "uq_auth_devices_active_user_app",
            "user_id",
            "application_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
    )


class DeviceSession(Base):
    __tablename__ = "auth_device_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("auth_devices.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (Index("auth_device_sessions_device_idx", "device_id"),)


class AuthNonce(Base):
    __tablename__ = "auth_nonces"

    nonce_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (Index("auth_nonces_expiry_idx", "expires_at"),)


class AuthAuditEvent(Base):
    __tablename__ = "auth_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_type: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("auth_audit_event_lookup_idx", "event_type", "source_ip", "created_at"),
    )
