"""issue 107: add v2_works.number_meta (songbook numbers)

Production already received this column manually in issue 105, so the upgrade is a no-op
when the column exists.

Revision ID: issue107numbermeta
Revises: issue91imagesettings
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue107numbermeta"
down_revision: str | Sequence[str] | None = "issue91imagesettings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("v2_works")}
    if "number_meta" not in columns:
        op.add_column(
            "v2_works",
            sa.Column("number_meta", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )


def downgrade() -> None:
    with op.batch_alter_table("v2_works") as batch:
        batch.drop_column("number_meta")
