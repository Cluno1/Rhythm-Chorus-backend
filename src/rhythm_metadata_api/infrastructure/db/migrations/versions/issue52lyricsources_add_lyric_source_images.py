"""issue 52: retain lyric source PDF pages as shared image assets

Revision ID: issue52lyricsources
Revises: issue32multilyrics
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue52lyricsources"
down_revision: str | Sequence[str] | None = "issue32multilyrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v2_lyric_source_documents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("edition", sa.String(length=500), nullable=True),
        sa.Column("publisher", sa.String(length=500), nullable=True),
        sa.Column("published_year", sa.Integer(), nullable=True),
        sa.Column("document_asset_id", sa.String(length=36), nullable=True),
        sa.Column("source_ref", sa.String(length=1000), nullable=True),
        sa.Column("rights_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('pdf', 'scan', 'photo', 'booklet', 'web')",
            name="lyric_source_document_kind",
        ),
        sa.ForeignKeyConstraint(["document_asset_id"], ["v2_assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "v2_lyric_source_pages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("physical_page_number", sa.Integer(), nullable=False),
        sa.Column("image_asset_id", sa.String(length=36), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=False),
        sa.Column("height_px", sa.Integer(), nullable=False),
        sa.Column("render_dpi", sa.Integer(), nullable=False),
        sa.Column("display_label", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("physical_page_number >= 1", name="lyric_source_page_number"),
        sa.CheckConstraint(
            "width_px > 0 AND height_px > 0 AND render_dpi > 0",
            name="lyric_source_page_dimensions",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["v2_lyric_source_documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["image_asset_id"], ["v2_assets.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "physical_page_number", name="uq_v2_lyric_source_page_number"
        ),
    )
    op.create_index(
        "v2_lyric_source_pages_document_idx",
        "v2_lyric_source_pages",
        ["document_id"],
    )
    op.create_table(
        "v2_lyric_source_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_id", sa.String(length=36), nullable=True),
        sa.Column("score_id", sa.String(length=36), nullable=True),
        sa.Column("rendition_id", sa.String(length=36), nullable=True),
        sa.Column("source_page_id", sa.String(length=36), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("language_relations", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(CASE WHEN work_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN score_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN rendition_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="lyric_source_link_one_owner",
        ),
        sa.CheckConstraint("display_order >= 1", name="lyric_source_link_display_order"),
        sa.ForeignKeyConstraint(["work_id"], ["v2_works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["score_id"], ["v2_scores.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rendition_id"], ["v2_renditions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_page_id"], ["v2_lyric_source_pages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for owner in ("work", "score", "rendition"):
        owner_column = f"{owner}_id"
        op.create_index(
            f"uq_v2_lyric_source_link_{owner}_page",
            "v2_lyric_source_links",
            [owner_column, "source_page_id"],
            unique=True,
            sqlite_where=sa.text(f"{owner_column} IS NOT NULL"),
        )
        op.create_index(
            f"v2_lyric_source_links_{owner}_idx",
            "v2_lyric_source_links",
            [owner_column, "display_order"],
        )


def downgrade() -> None:
    op.drop_table("v2_lyric_source_links")
    op.drop_index("v2_lyric_source_pages_document_idx", table_name="v2_lyric_source_pages")
    op.drop_table("v2_lyric_source_pages")
    op.drop_table("v2_lyric_source_documents")
