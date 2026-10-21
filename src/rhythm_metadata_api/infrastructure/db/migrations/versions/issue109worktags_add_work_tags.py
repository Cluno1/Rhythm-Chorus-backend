"""Add Catalog Work tags.

Revision ID: issue109worktags
Revises: issue107numbermeta
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue109worktags"
down_revision: str | Sequence[str] | None = "issue107numbermeta"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "v2_works",
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    with op.batch_alter_table("v2_works") as batch:
        batch.drop_column("tags")
