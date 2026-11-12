"""Per-operation source health without changing stable source/song identities.

Revision ID: issue189musiccaps
Revises: issue185health
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "issue189musiccaps"
down_revision = "issue185health"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("v2_music_sources", sa.Column("service_group_id", sa.String(40)))
    op.add_column("v2_music_sources", sa.Column("archived_at", sa.DateTime(timezone=True)))
    op.add_column("v2_music_settings", sa.Column(
        "cross_platform_playback_enabled", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    op.add_column("v2_music_sources", sa.Column(
        "operation_health", sa.JSON(), nullable=False, server_default="{}",
    ))
    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT id, health_failed, health_checked_at, health_latency_ms, health_error "
        "FROM v2_music_sources"
    )).mappings()
    for row in rows:
        if row["health_checked_at"] is None:
            continue
        value = {"song_search": {
            "state": "unhealthy" if row["health_failed"] else "healthy",
            "checked_at": str(row["health_checked_at"]),
            "latency_ms": row["health_latency_ms"],
            "error": row["health_error"],
        }}
        connection.execute(sa.text(
            "UPDATE v2_music_sources SET operation_health = :value WHERE id = :id"
        ), {"id": row["id"], "value": json.dumps(value, ensure_ascii=False)})
    op.create_table(
        "v2_music_playback_tickets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("device_id", sa.String(100)),
        sa.Column("origin_source_id", sa.String(40), nullable=False),
        sa.Column("origin_song_id", sa.String(80), nullable=False),
        sa.Column("serving_source_id", sa.String(40), nullable=False),
        sa.Column("serving_song_id", sa.String(80), nullable=False),
        sa.Column("quality", sa.String(12), nullable=False),
        sa.Column("audio_entity_id", sa.String(96), nullable=False),
        sa.Column("encrypted_url", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("content_length", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(200)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("v2_music_playback_tickets_expires_idx", "v2_music_playback_tickets", ["expires_at"])


def downgrade():
    op.drop_index("v2_music_playback_tickets_expires_idx", table_name="v2_music_playback_tickets")
    op.drop_table("v2_music_playback_tickets")
    op.drop_column("v2_music_sources", "operation_health")
    op.drop_column("v2_music_sources", "archived_at")
    op.drop_column("v2_music_sources", "service_group_id")
    op.drop_column("v2_music_settings", "cross_platform_playback_enabled")
