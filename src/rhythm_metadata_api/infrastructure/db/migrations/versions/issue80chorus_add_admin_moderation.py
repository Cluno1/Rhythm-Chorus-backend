"""issue 80: add administrator devices and chorus moderation settings

Revision ID: issue80chorus
Revises: issue78chorus
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue80chorus"
down_revision: str | Sequence[str] | None = "issue78chorus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("auth_devices") as batch:
        batch.add_column(
            sa.Column(
                "is_administrator",
                sa.Boolean(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )

    op.create_table(
        "v2_chorus_moderation_settings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "automatic_approval",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO v2_chorus_moderation_settings "
        "(id, automatic_approval, updated_by, updated_at) "
        "VALUES ('global', 1, 'migration', CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    op.drop_table("v2_chorus_moderation_settings")
    with op.batch_alter_table("auth_devices") as batch:
        batch.drop_column("is_administrator")
