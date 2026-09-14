"""issue 83: add configurable active-device slots

Revision ID: issue83multidevice
Revises: issue80timeline
Create Date: 2026-09-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue83multidevice"
down_revision: str | Sequence[str] | None = "issue80timeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("auth_devices", sa.Column("active_slot", sa.Integer(), nullable=True))
    op.execute("UPDATE auth_devices SET active_slot = 1 WHERE status = 'active'")
    op.drop_index("uq_auth_devices_active_user_app", table_name="auth_devices")
    with op.batch_alter_table("auth_devices") as batch:
        batch.create_check_constraint(
            "auth_device_active_slot",
            "status != 'active' OR (active_slot IS NOT NULL AND active_slot > 0)",
        )
    op.create_index(
        "uq_auth_devices_active_user_app_slot",
        "auth_devices",
        ["user_id", "application_id", "active_slot"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            "SELECT user_id, application_id FROM auth_devices "
            "WHERE status = 'active' GROUP BY user_id, application_id HAVING COUNT(*) > 1 "
            "LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "cannot downgrade while a user/application has multiple active devices"
        )
    op.drop_index("uq_auth_devices_active_user_app_slot", table_name="auth_devices")
    with op.batch_alter_table("auth_devices") as batch:
        batch.drop_constraint("auth_device_active_slot", type_="check")
        batch.drop_column("active_slot")
    op.create_index(
        "uq_auth_devices_active_user_app",
        "auth_devices",
        ["user_id", "application_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
