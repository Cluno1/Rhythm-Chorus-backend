"""issue 12: add cross-work releases and release items

Revision ID: issue12release
Revises: issue9coverlyrics
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue12release"
down_revision: str | Sequence[str] | None = "issue9coverlyrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("v2_works") as batch_op:
        batch_op.create_foreign_key(
            "fk_v2_works_cover_asset", "v2_assets", ["cover_asset_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("v2_arrangements") as batch_op:
        batch_op.create_foreign_key(
            "fk_v2_arrangements_cover_asset",
            "v2_assets",
            ["cover_asset_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("v2_renditions") as batch_op:
        batch_op.create_foreign_key(
            "fk_v2_renditions_cover_asset",
            "v2_assets",
            ["cover_asset_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_table(
        "v2_releases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("album_artist", sa.String(length=500), nullable=True),
        sa.Column("release_date", sa.String(length=10), nullable=True),
        sa.Column("cover_asset_id", sa.String(length=36), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["cover_asset_id"], ["v2_assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index("v2_releases_title_idx", "v2_releases", ["title"], unique=False)
    op.create_table(
        "v2_release_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("release_id", sa.String(length=36), nullable=False),
        sa.Column("rendition_id", sa.String(length=36), nullable=False),
        sa.Column("disc_no", sa.Integer(), nullable=False),
        sa.Column("track_no", sa.Integer(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("disc_no >= 1", name="release_item_disc_no"),
        sa.CheckConstraint("track_no IS NULL OR track_no >= 1", name="release_item_track_no"),
        sa.CheckConstraint("display_order >= 1", name="release_item_display_order"),
        sa.ForeignKeyConstraint(["release_id"], ["v2_releases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rendition_id"], ["v2_renditions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("release_id", "display_order", name="uq_v2_release_display_order"),
        sa.UniqueConstraint("release_id", "rendition_id", name="uq_v2_release_rendition"),
    )
    op.create_index(
        "v2_release_items_release_idx",
        "v2_release_items",
        ["release_id", "display_order"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("v2_release_items_release_idx", table_name="v2_release_items")
    op.drop_table("v2_release_items")
    op.drop_index("v2_releases_title_idx", table_name="v2_releases")
    op.drop_table("v2_releases")
    with op.batch_alter_table("v2_renditions") as batch_op:
        batch_op.drop_constraint("fk_v2_renditions_cover_asset", type_="foreignkey")
    with op.batch_alter_table("v2_arrangements") as batch_op:
        batch_op.drop_constraint("fk_v2_arrangements_cover_asset", type_="foreignkey")
    with op.batch_alter_table("v2_works") as batch_op:
        batch_op.drop_constraint("fk_v2_works_cover_asset", type_="foreignkey")
