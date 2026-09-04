"""issue 9: add cover_asset_id and lyrics for album/song/score fallback

Revision ID: issue9coverlyrics
Revises: 25ff14940d0d
Create Date: 2026-09-04

封面回退链：song(rendition) -> album(arrangement) -> work
歌词回退链：song(rendition) -> 乐谱(score) -> work
均为可空新增列，向后兼容。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue9coverlyrics"
down_revision: str | Sequence[str] | None = "25ff14940d0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("v2_works", sa.Column("cover_asset_id", sa.String(length=36), nullable=True))
    op.add_column("v2_works", sa.Column("lyrics", sa.Text(), nullable=True))
    op.add_column(
        "v2_arrangements", sa.Column("cover_asset_id", sa.String(length=36), nullable=True)
    )
    op.add_column("v2_scores", sa.Column("lyrics", sa.Text(), nullable=True))
    op.add_column(
        "v2_renditions", sa.Column("cover_asset_id", sa.String(length=36), nullable=True)
    )
    op.add_column("v2_renditions", sa.Column("lyrics", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("v2_renditions", "lyrics")
    op.drop_column("v2_renditions", "cover_asset_id")
    op.drop_column("v2_scores", "lyrics")
    op.drop_column("v2_arrangements", "cover_asset_id")
    op.drop_column("v2_works", "lyrics")
    op.drop_column("v2_works", "cover_asset_id")
