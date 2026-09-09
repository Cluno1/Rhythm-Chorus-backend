"""issue 32: add structured multilingual lyrics to work, score, and rendition

Revision ID: issue32multilyrics
Revises: issue15updateidentity
Create Date: 2026-09-09

The existing ``lyrics`` column remains the default-language text for backward
compatibility. ``lyrics_language`` identifies that text, while
``lyrics_translations`` stores the other language variants as a JSON array.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue32multilyrics"
down_revision: str | Sequence[str] | None = "issue15updateidentity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_multilingual_columns(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column(
            "lyrics_language",
            sa.String(length=35),
            nullable=False,
            server_default="und",
        ),
    )
    op.add_column(
        table_name,
        sa.Column(
            "lyrics_translations",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def upgrade() -> None:
    for table_name in ("v2_works", "v2_scores", "v2_renditions"):
        _add_multilingual_columns(table_name)

    op.execute(
        "UPDATE v2_works "
        "SET lyrics_language = COALESCE(NULLIF(TRIM(language), ''), 'und')"
    )
    for table_name in ("v2_scores", "v2_renditions"):
        op.execute(
            f"UPDATE {table_name} "
            "SET lyrics_language = COALESCE(("
            "SELECT NULLIF(TRIM(work.language), '') "
            "FROM v2_arrangements AS arrangement "
            "JOIN v2_works AS work ON work.id = arrangement.work_id "
            f"WHERE arrangement.id = {table_name}.arrangement_id"
            "), 'und')"
        )


def downgrade() -> None:
    for table_name in ("v2_renditions", "v2_scores", "v2_works"):
        op.drop_column(table_name, "lyrics_translations")
        op.drop_column(table_name, "lyrics_language")
