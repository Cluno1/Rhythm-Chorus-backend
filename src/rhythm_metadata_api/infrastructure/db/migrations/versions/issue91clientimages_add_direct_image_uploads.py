"""issue 91: add client-owned direct image uploads

Revision ID: issue91clientimages
Revises: issue83multidevice
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue91clientimages"
down_revision: str | Sequence[str] | None = "issue83multidevice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("v2_upload_sessions") as batch:
        batch.drop_constraint("upload_session_state", type_="check")
        batch.create_check_constraint(
            "upload_session_state",
            "state IN ('created', 'uploaded', 'completed', 'failed', 'expired', 'cancelled')",
        )

    op.create_table(
        "v2_client_image_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=100), nullable=False),
        sa.Column("client_batch_id", sa.String(length=100), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("succeeded_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("total_bytes >= 0", name="client_image_batch_bytes"),
        sa.CheckConstraint("total_count > 0", name="client_image_batch_count"),
        sa.CheckConstraint(
            "state IN ('active', 'partial', 'completed', 'cancelled')",
            name="client_image_batch_state",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "client_batch_id",
            name="uq_v2_client_image_batch_owner_client",
        ),
    )
    op.create_index(
        "v2_client_image_batches_owner_idx",
        "v2_client_image_batches",
        ["owner_user_id", "created_at"],
    )

    op.create_table(
        "v2_client_images",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=100), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("client_item_id", sa.String(length=100), nullable=False),
        sa.Column("upload_session_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=True),
        sa.Column("uploader_device_id", sa.String(length=36), nullable=True),
        sa.Column("display_name", sa.String(length=500), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("image_format", sa.String(length=16), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("content_md5", sa.String(length=64), nullable=False),
        sa.Column("client_sha256", sa.String(length=64), nullable=False),
        sa.Column("thumbnail_media_type", sa.String(length=64), nullable=False),
        sa.Column("thumbnail_width", sa.Integer(), nullable=False),
        sa.Column("thumbnail_height", sa.Integer(), nullable=False),
        sa.Column("thumbnail_byte_size", sa.Integer(), nullable=False),
        sa.Column("thumbnail_content_md5", sa.String(length=64), nullable=False),
        sa.Column("thumbnail_client_sha256", sa.String(length=64), nullable=False),
        sa.Column("thumbnail_storage_key", sa.String(length=1000), nullable=True),
        sa.Column("cos_crc64", sa.String(length=32), nullable=True),
        sa.Column("thumbnail_cos_crc64", sa.String(length=32), nullable=True),
        sa.Column("verification_method", sa.String(length=64), nullable=True),
        sa.Column("metadata_sanitized", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("byte_size > 0", name="client_image_size"),
        sa.CheckConstraint("width > 0 AND height > 0", name="client_image_dimensions"),
        sa.CheckConstraint(
            "thumbnail_width > 0 AND thumbnail_height > 0 "
            "AND thumbnail_width <= 512 AND thumbnail_height <= 512",
            name="client_image_thumbnail_dimensions",
        ),
        sa.CheckConstraint("thumbnail_byte_size > 0", name="client_image_thumbnail_size"),
        sa.CheckConstraint(
            "state IN ('upload_pending', 'verifying', 'ready', 'rejected', 'cancelled', 'deleted')",
            name="client_image_state",
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["v2_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["v2_client_image_batches.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["upload_session_id"], ["v2_upload_sessions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("upload_session_id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "batch_id",
            "client_item_id",
            name="uq_v2_client_image_owner_batch_item",
        ),
    )
    op.create_index(
        "v2_client_images_owner_idx",
        "v2_client_images",
        ["owner_user_id", "created_at"],
    )
    op.create_index(
        "v2_client_images_asset_idx", "v2_client_images", ["asset_id"]
    )
    op.create_index(
        "v2_client_images_state_idx", "v2_client_images", ["state", "created_at"]
    )

    op.create_table(
        "v2_user_image_admin_visibility",
        sa.Column("owner_user_id", sa.String(length=100), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["auth_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("owner_user_id"),
    )

    op.create_table(
        "v2_client_image_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.String(length=100), nullable=False),
        sa.Column("image_id", sa.String(length=36), nullable=True),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("device_id", sa.String(length=200), nullable=True),
        sa.Column("operation", sa.String(length=100), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "v2_client_image_audit_owner_idx",
        "v2_client_image_audit_events",
        ["owner_user_id", "created_at"],
    )
    op.create_index(
        "v2_client_image_audit_image_idx",
        "v2_client_image_audit_events",
        ["image_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "v2_client_image_audit_image_idx", table_name="v2_client_image_audit_events"
    )
    op.drop_index(
        "v2_client_image_audit_owner_idx", table_name="v2_client_image_audit_events"
    )
    op.drop_table("v2_client_image_audit_events")
    op.drop_table("v2_user_image_admin_visibility")
    op.drop_index("v2_client_images_state_idx", table_name="v2_client_images")
    op.drop_index("v2_client_images_asset_idx", table_name="v2_client_images")
    op.drop_index("v2_client_images_owner_idx", table_name="v2_client_images")
    op.drop_table("v2_client_images")
    op.drop_index(
        "v2_client_image_batches_owner_idx", table_name="v2_client_image_batches"
    )
    op.drop_table("v2_client_image_batches")

    # The pre-Issue-91 constraint has no cancelled state. Preserve rollback
    # compatibility for databases that already contain cancelled image uploads.
    op.execute(
        sa.update(sa.table("v2_upload_sessions", sa.column("state", sa.String())))
        .where(sa.column("state") == "cancelled")
        .values(state="failed")
    )
    with op.batch_alter_table("v2_upload_sessions") as batch:
        batch.drop_constraint("upload_session_state", type_="check")
        batch.create_check_constraint(
            "upload_session_state",
            "state IN ('created', 'uploaded', 'completed', 'failed', 'expired')",
        )
