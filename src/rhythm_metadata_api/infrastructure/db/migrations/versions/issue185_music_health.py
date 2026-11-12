"""Scheduled source health.

Revision ID: issue185health
Revises: issue181musicsettings
"""
import sqlalchemy as sa
from alembic import op

revision = "issue185health"
down_revision = "issue181musicsettings"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("v2_music_sources", sa.Column("health_failed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("v2_music_sources", sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("v2_music_sources", sa.Column("health_latency_ms", sa.Integer(), nullable=True))
    op.add_column("v2_music_sources", sa.Column("health_error", sa.String(300), nullable=True))
    op.add_column("v2_music_settings", sa.Column("health_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("v2_music_settings", sa.Column("health_interval_minutes", sa.Integer(), nullable=False, server_default="60"))

def downgrade():
    op.drop_column("v2_music_settings", "health_interval_minutes")
    op.drop_column("v2_music_settings", "health_enabled")
    op.drop_column("v2_music_sources", "health_error")
    op.drop_column("v2_music_sources", "health_latency_ms")
    op.drop_column("v2_music_sources", "health_checked_at")
    op.drop_column("v2_music_sources", "health_failed")
