"""add structured multilingual title variants to work

Revision ID: worktitletranslations
Revises: issue109worktags
Create Date: 2026-10-22

Mirrors ``lyrics_translations`` (issue 32): ``canonical_title`` + ``language``
remain the Work's single default-language title, while ``title_translations``
stores the other language variants (e.g. the official simplified/traditional/
English/Cantonese names) as a JSON array of ``{"language": ..., "title": ...}``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "worktitletranslations"
down_revision: str | Sequence[str] | None = "issue109worktags"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "v2_works",
        sa.Column(
            "title_translations",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("v2_works", "title_translations")
