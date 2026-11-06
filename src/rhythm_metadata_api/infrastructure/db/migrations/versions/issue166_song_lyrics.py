"""Track who chose public song lyrics and share lyrics adopted for local songs.

Revision ID: issue166lyrics
Revises: issue154announcements
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue166lyrics"
down_revision: str | Sequence[str] | None = "issue154announcements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("v2_music_song_metadata") as batch:
        batch.add_column(sa.Column("lyrics_mode", sa.String(12), nullable=True))
        batch.add_column(sa.Column("lyrics_provider", sa.String(40), nullable=True))
        batch.add_column(sa.Column("lyrics_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "v2_device_song_lyrics",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title_key", sa.String(200), nullable=False),
        sa.Column("artist_key", sa.String(200), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("artist", sa.String(500), nullable=False),
        sa.Column("album", sa.String(500)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("lyrics_lrc", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("provider", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "v2_device_song_lyrics_key_idx", "v2_device_song_lyrics", ["title_key", "artist_key"]
    )


def downgrade() -> None:
    op.drop_index("v2_device_song_lyrics_key_idx", table_name="v2_device_song_lyrics")
    op.drop_table("v2_device_song_lyrics")
    with op.batch_alter_table("v2_music_song_metadata") as batch:
        batch.drop_column("lyrics_updated_at")
        batch.drop_column("lyrics_provider")
        batch.drop_column("lyrics_mode")
