"""issue 58: store rendition lyric formats for device writes

Revision ID: issue58lyricwrite
Revises: issue52lyricsources
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue58lyricwrite"
down_revision: str | Sequence[str] | None = "issue52lyricsources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "v2_renditions",
        sa.Column(
            "lyrics_formats",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("v2_renditions", "lyrics_formats")
