"""add rooms for picking and ordering a gathering's songs together

Revision ID: issue124rooms
Revises: worktitletranslations
Create Date: 2026-10-25

issue 124: a room has an expiry date and ordered songs; every song sits in a section (one
unnamed default section today) so room templates and section assignees can be added later
without moving data. ``template_id`` and ``assignee_user_id`` are reserved and stay null.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue124rooms"
down_revision: str | Sequence[str] | None = "worktitletranslations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v2_rooms",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.String(100), nullable=False),
        sa.Column("template_id", sa.String(36), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("v2_rooms_expiry_idx", "v2_rooms", ["expires_at"])
    op.create_table(
        "v2_room_sections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "room_id", sa.String(36), sa.ForeignKey("v2_rooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(60), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("assignee_user_id", sa.String(100), nullable=True),
    )
    op.create_index("v2_room_sections_room_idx", "v2_room_sections", ["room_id", "position"])
    op.create_table(
        "v2_room_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "room_id", sa.String(36), sa.ForeignKey("v2_rooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "section_id", sa.String(36),
            sa.ForeignKey("v2_room_sections.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "work_id", sa.String(36), sa.ForeignKey("v2_works.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("added_by_user_id", sa.String(100), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("room_id", "work_id", name="v2_room_items_one_work_per_room"),
    )
    op.create_index("v2_room_items_section_idx", "v2_room_items", ["section_id", "position"])


def downgrade() -> None:
    op.drop_index("v2_room_items_section_idx", table_name="v2_room_items")
    op.drop_table("v2_room_items")
    op.drop_index("v2_room_sections_room_idx", table_name="v2_room_sections")
    op.drop_table("v2_room_sections")
    op.drop_index("v2_rooms_expiry_idx", table_name="v2_rooms")
    op.drop_table("v2_rooms")
