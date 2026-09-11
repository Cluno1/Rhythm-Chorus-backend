"""issue 78: add online chorus projects, tracks, timing anchors, and mixes

Revision ID: issue78chorus
Revises: issue58lyricwrite
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue78chorus"
down_revision: str | Sequence[str] | None = "issue58lyricwrite"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v2_chorus_projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_id", sa.String(length=36), nullable=False),
        sa.Column("arrangement_id", sa.String(length=36), nullable=False),
        sa.Column("alignment_score_revision_id", sa.String(length=36), nullable=False),
        sa.Column("timeline_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=100), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'open', 'closed', 'archived')",
            name="chorus_project_status",
        ),
        sa.ForeignKeyConstraint(["work_id"], ["v2_works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["arrangement_id"], ["v2_arrangements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["alignment_score_revision_id"],
            ["v2_score_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("v2_chorus_projects_work_idx", "v2_chorus_projects", ["work_id", "status"])
    op.create_index(
        "v2_chorus_projects_arrangement_idx",
        "v2_chorus_projects",
        ["arrangement_id"],
    )

    op.create_table(
        "v2_chorus_tracks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("chorus_project_id", sa.String(length=36), nullable=False),
        sa.Column("rendition_id", sa.String(length=36), nullable=False),
        sa.Column("uploader_user_id", sa.String(length=100), nullable=False),
        sa.Column("part_id", sa.String(length=36), nullable=True),
        sa.Column("upload_session_id", sa.String(length=36), nullable=True),
        sa.Column("contribution_kind", sa.String(length=32), nullable=False),
        sa.Column("display_label", sa.String(length=300), nullable=False),
        sa.Column("take_no", sa.Integer(), nullable=False),
        sa.Column("alignment_state", sa.String(length=32), nullable=False),
        sa.Column("alignment_offset_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("gain_millibels", sa.Integer(), nullable=False),
        sa.Column("pan_milli", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("waveform_peaks", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "contribution_kind IN ('vocal_part', 'harmony', 'guitar', 'piano', "
            "'percussion', 'other')",
            name="chorus_track_kind",
        ),
        sa.CheckConstraint(
            "alignment_state IN ('pending', 'automatic', 'manual', 'verified', 'failed')",
            name="chorus_track_alignment_state",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'processing', 'pending_review', 'published', "
            "'rejected', 'withdrawn', 'failed')",
            name="chorus_track_status",
        ),
        sa.CheckConstraint("take_no >= 1", name="chorus_track_take_no"),
        sa.CheckConstraint(
            "gain_millibels >= -2400 AND gain_millibels <= 1200",
            name="chorus_track_gain",
        ),
        sa.CheckConstraint("pan_milli >= -1000 AND pan_milli <= 1000", name="chorus_track_pan"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms > 0", name="chorus_track_duration"),
        sa.ForeignKeyConstraint(
            ["chorus_project_id"], ["v2_chorus_projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["rendition_id"], ["v2_renditions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploader_user_id"], ["auth_users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["part_id"], ["v2_parts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["upload_session_id"], ["v2_upload_sessions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rendition_id"),
    )
    op.create_index(
        "v2_chorus_tracks_project_idx",
        "v2_chorus_tracks",
        ["chorus_project_id", "status"],
    )
    op.create_index(
        "v2_chorus_tracks_uploader_idx",
        "v2_chorus_tracks",
        ["uploader_user_id", "status"],
    )

    op.create_table(
        "v2_score_rendition_sync",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("score_revision_id", sa.String(length=36), nullable=False),
        sa.Column("rendition_id", sa.String(length=36), nullable=False),
        sa.Column("anchor_order", sa.Integer(), nullable=False),
        sa.Column("score_tick", sa.Integer(), nullable=False),
        sa.Column("media_ms", sa.Integer(), nullable=False),
        sa.Column("confidence_milli", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.CheckConstraint("anchor_order >= 0", name="score_rendition_sync_order"),
        sa.CheckConstraint("score_tick >= 0", name="score_rendition_sync_tick"),
        sa.CheckConstraint("media_ms >= 0", name="score_rendition_sync_media"),
        sa.CheckConstraint(
            "confidence_milli >= 0 AND confidence_milli <= 1000",
            name="score_rendition_sync_confidence",
        ),
        sa.CheckConstraint(
            "source IN ('in_app_clock', 'automatic', 'manual')",
            name="score_rendition_sync_source",
        ),
        sa.ForeignKeyConstraint(
            ["score_revision_id"], ["v2_score_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["rendition_id"], ["v2_renditions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "score_revision_id",
            "rendition_id",
            "anchor_order",
            name="uq_v2_score_rendition_sync_order",
        ),
    )
    op.create_index(
        "v2_score_rendition_sync_lookup_idx",
        "v2_score_rendition_sync",
        ["score_revision_id", "rendition_id", "anchor_order"],
    )

    op.create_table(
        "v2_chorus_mix_variants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("chorus_project_id", sa.String(length=36), nullable=False),
        sa.Column("selection_hash", sa.String(length=64), nullable=False),
        sa.Column("selected_track_ids", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("selected_track_count", sa.Integer(), nullable=False),
        sa.Column("mix_profile", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("selected_track_count >= 1", name="chorus_mix_track_count"),
        sa.CheckConstraint(
            "state IN ('queued', 'processing', 'ready', 'failed', 'obsolete')",
            name="chorus_mix_state",
        ),
        sa.ForeignKeyConstraint(
            ["chorus_project_id"], ["v2_chorus_projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["v2_assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chorus_project_id",
            "selection_hash",
            "mix_profile",
            name="uq_v2_chorus_mix_selection",
        ),
    )
    op.create_index(
        "v2_chorus_mix_project_idx",
        "v2_chorus_mix_variants",
        ["chorus_project_id", "state"],
    )


def downgrade() -> None:
    op.drop_table("v2_chorus_mix_variants")
    op.drop_table("v2_score_rendition_sync")
    op.drop_table("v2_chorus_tracks")
    op.drop_table("v2_chorus_projects")
