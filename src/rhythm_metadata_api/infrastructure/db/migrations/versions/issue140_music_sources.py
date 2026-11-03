"""Administrator-managed music source instances.

Revision ID: issue140music
Revises: issue134removechorus
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue140music"
down_revision: str | Sequence[str] | None = "issue134removechorus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v2_music_sources",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("adapter", sa.String(40), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "v2_music_sources_enabled_priority_idx", "v2_music_sources", ["enabled", "priority"]
    )
    # Explicitly disabled until an administrator confirms the external source in Web.
    op.execute("""
        INSERT INTO v2_music_sources
        (id, name, adapter, base_url, enabled, priority, revision, created_at, updated_at)
        VALUES ('feiniao', '飞鸟轻音', 'feiniao', 'http://music.xn--q35am7h.site/',
                0, 100, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
    """)


def downgrade() -> None:
    op.drop_index("v2_music_sources_enabled_priority_idx", table_name="v2_music_sources")
    op.drop_table("v2_music_sources")
