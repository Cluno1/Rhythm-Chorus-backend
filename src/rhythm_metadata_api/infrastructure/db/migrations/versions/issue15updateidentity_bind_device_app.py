"""issue 15: bind registered devices to one Sonorus application identity

Revision ID: issue15updateidentity
Revises: issue14deviceauth
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue15updateidentity"
down_revision: str | Sequence[str] | None = "issue14deviceauth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("auth_devices", sa.Column("application_id", sa.String(200), nullable=True))
    op.add_column(
        "auth_devices",
        sa.Column("signing_certificate_sha256", sa.String(64), nullable=True),
    )
    op.execute(
        "UPDATE auth_devices SET application_id = 'legacy.rhythm', "
        "signing_certificate_sha256 = '" + "0" * 64 + "'"
    )
    with op.batch_alter_table("auth_devices") as batch:
        batch.alter_column("application_id", nullable=False)
        batch.alter_column("signing_certificate_sha256", nullable=False)
    op.drop_index("uq_auth_devices_active_user", table_name="auth_devices")
    op.create_index(
        "uq_auth_devices_active_user_app",
        "auth_devices",
        ["user_id", "application_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_auth_devices_active_user_app", table_name="auth_devices")
    op.create_index(
        "uq_auth_devices_active_user",
        "auth_devices",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
    with op.batch_alter_table("auth_devices") as batch:
        batch.drop_column("signing_certificate_sha256")
        batch.drop_column("application_id")
