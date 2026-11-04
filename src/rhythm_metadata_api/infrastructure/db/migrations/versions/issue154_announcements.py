"""Administrator announcements and the images they may reference.

Revision ID: issue154announcements
Revises: issue152templates
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue154announcements"
down_revision: str | Sequence[str] | None = "issue152templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v2_announcements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("summary", sa.String(200), nullable=True),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("importance", sa.String(20), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "v2_announcements_status_published_idx", "v2_announcements", ["status", "published_at"],
    )
    op.create_table(
        "v2_announcement_images",
        sa.Column("name", sa.String(80), primary_key=True),
        sa.Column("media_type", sa.String(40), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("v2_announcement_images")
    op.drop_index("v2_announcements_status_published_idx", table_name="v2_announcements")
    op.drop_table("v2_announcements")
