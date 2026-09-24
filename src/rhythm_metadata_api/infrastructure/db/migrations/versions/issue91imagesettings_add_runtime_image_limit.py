"""issue 91: add administrator-managed client image size limit

Revision ID: issue91imagesettings
Revises: issue91clientimages
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue91imagesettings"
down_revision: str | Sequence[str] | None = "issue91clientimages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v2_client_image_settings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column(
            "max_image_bytes",
            sa.Integer(),
            server_default=sa.text("52428800"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_image_bytes >= 1048576 AND max_image_bytes <= 524288000",
            name="client_image_settings_max_bytes",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO v2_client_image_settings "
        "(id, max_image_bytes, updated_by, updated_at) "
        "VALUES ('global', 52428800, 'migration', CURRENT_TIMESTAMP)"
    )


def downgrade() -> None:
    op.drop_table("v2_client_image_settings")
