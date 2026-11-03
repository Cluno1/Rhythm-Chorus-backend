"""Store client-supplied public music display metadata.

Revision ID: issue141metadata
Revises: issue140music
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue141metadata"
down_revision: str | Sequence[str] | None = "issue140music"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v2_music_song_metadata",
        sa.Column("source_id", sa.String(40), sa.ForeignKey("v2_music_sources.id"), primary_key=True),
        sa.Column("song_id", sa.String(80), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("artists", sa.JSON(), nullable=False),
        sa.Column("album", sa.String(500)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("artwork_url", sa.String(2000)),
        sa.Column("artist_avatar_url", sa.String(2000)),
        sa.Column("lyrics_lrc", sa.Text()),
        sa.Column("qualities", sa.JSON(), nullable=False),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("v2_music_song_metadata")
