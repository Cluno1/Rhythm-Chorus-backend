"""issue 14: add one-device registration and request-proof tables

Revision ID: issue14deviceauth
Revises: issue12release
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue14deviceauth"
down_revision: str | Sequence[str] | None = "issue12release"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auth_users",
        sa.Column("id", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="auth_user_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "auth_device_invites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_device_id", sa.String(length=36), nullable=True),
        sa.Column("issued_by", sa.String(length=100), nullable=False),
        sa.Column("replace_existing_device", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
    )
    op.create_index(
        "auth_device_invites_user_idx",
        "auth_device_invites",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "auth_devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=100), nullable=False),
        sa.Column("public_key_spki", sa.Text(), nullable=False),
        sa.Column("public_key_thumbprint", sa.String(length=64), nullable=False),
        sa.Column("key_algorithm", sa.String(length=20), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="auth_device_status"),
        sa.ForeignKeyConstraint(["user_id"], ["auth_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_key_thumbprint"),
    )
    op.create_index("auth_devices_user_idx", "auth_devices", ["user_id"], unique=False)
    op.create_index(
        "uq_auth_devices_active_user",
        "auth_devices",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "auth_device_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["auth_devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "auth_device_sessions_device_idx",
        "auth_device_sessions",
        ["device_id"],
        unique=False,
    )
    op.create_table(
        "auth_nonces",
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("nonce_hash"),
    )
    op.create_index("auth_nonces_expiry_idx", "auth_nonces", ["expires_at"], unique=False)
    op.create_table(
        "auth_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_type", sa.String(length=30), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("source_ip", sa.String(length=100), nullable=True),
        sa.Column("result", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "auth_audit_event_lookup_idx",
        "auth_audit_events",
        ["event_type", "source_ip", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("auth_audit_event_lookup_idx", table_name="auth_audit_events")
    op.drop_table("auth_audit_events")
    op.drop_index("auth_nonces_expiry_idx", table_name="auth_nonces")
    op.drop_table("auth_nonces")
    op.drop_index("auth_device_sessions_device_idx", table_name="auth_device_sessions")
    op.drop_table("auth_device_sessions")
    op.drop_index("uq_auth_devices_active_user", table_name="auth_devices")
    op.drop_index("auth_devices_user_idx", table_name="auth_devices")
    op.drop_table("auth_devices")
    op.drop_index("auth_device_invites_user_idx", table_name="auth_device_invites")
    op.drop_table("auth_device_invites")
    op.drop_table("auth_users")
