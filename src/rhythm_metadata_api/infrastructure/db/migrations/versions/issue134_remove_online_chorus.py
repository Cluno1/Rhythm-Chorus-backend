"""Remove online chorus and its exclusive catalog data.

Revision ID: issue134removechorus
Revises: issue124rooms

The user explicitly requested permanent removal without a chorus data backup. Shared assets
and the general score/rendition synchronization table are retained. COS objects are removed
separately after deployment, after checking they have no remaining non-chorus references.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "issue134removechorus"
down_revision: str | Sequence[str] | None = "issue124rooms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("PRAGMA secure_delete=ON")
    op.execute("CREATE TEMP TABLE issue134_renditions (id TEXT PRIMARY KEY)")
    op.execute("""
        INSERT OR IGNORE INTO issue134_renditions
        SELECT rendition_id FROM v2_chorus_tracks
        UNION SELECT id FROM v2_renditions WHERE kind = 'chorus_track'
    """)
    op.execute("CREATE TEMP TABLE issue134_uploads (id TEXT PRIMARY KEY)")
    op.execute("""
        INSERT OR IGNORE INTO issue134_uploads
        SELECT upload_session_id FROM v2_chorus_tracks WHERE upload_session_id IS NOT NULL
        UNION SELECT id FROM v2_upload_sessions WHERE source = 'chorus_track'
    """)
    op.execute("CREATE TEMP TABLE issue134_assets (id TEXT PRIMARY KEY)")
    op.execute("""
        INSERT OR IGNORE INTO issue134_assets
        SELECT asset_id FROM v2_rendition_assets WHERE rendition_id IN
            (SELECT id FROM issue134_renditions)
        UNION SELECT cover_asset_id FROM v2_renditions WHERE id IN
            (SELECT id FROM issue134_renditions) AND cover_asset_id IS NOT NULL
        UNION SELECT asset_id FROM v2_chorus_mix_variants WHERE asset_id IS NOT NULL
        UNION SELECT completed_asset_id FROM v2_upload_sessions WHERE id IN
            (SELECT id FROM issue134_uploads) AND completed_asset_id IS NOT NULL
    """)
    # Remove chorus events and associated work mappings without affecting other activity.
    op.execute("""
        DELETE FROM v2_change_event_works WHERE event_sequence IN (
            SELECT sequence FROM v2_change_events
            WHERE entity_type LIKE 'chorus_%' OR operation LIKE 'chorus_%'
               OR (entity_type = 'rendition' AND entity_id IN (SELECT id FROM issue134_renditions))
        )
    """)
    op.execute("""
        DELETE FROM v2_change_events
        WHERE entity_type LIKE 'chorus_%' OR operation LIKE 'chorus_%'
           OR (entity_type = 'rendition' AND entity_id IN (SELECT id FROM issue134_renditions))
    """)
    op.execute("DELETE FROM v2_idempotency_keys WHERE lower(scope) LIKE '%chorus%'")
    # Explicit ordering also works on databases configured with foreign_keys enabled.
    for table in (
        "v2_chorus_mix_variants", "v2_chorus_tracks", "v2_chorus_timelines",
        "v2_chorus_projects", "v2_chorus_moderation_settings",
    ):
        op.drop_table(table)
    for table in (
        "v2_score_rendition_sync", "v2_release_items", "v2_lyric_source_links",
        "v2_rendition_credits", "v2_rendition_assets",
    ):
        op.execute(f"DELETE FROM {table} WHERE rendition_id IN (SELECT id FROM issue134_renditions)")
    op.execute("DELETE FROM v2_renditions WHERE id IN (SELECT id FROM issue134_renditions)")
    op.execute("""
        DELETE FROM v2_upload_sessions WHERE id IN (SELECT id FROM issue134_uploads)
        AND NOT EXISTS (SELECT 1 FROM v2_client_images WHERE upload_session_id = v2_upload_sessions.id)
    """)
    op.execute("DELETE FROM v2_asset_sources WHERE source = 'chorus_track'")
    # Keep any asset still used by another feature, including images and active uploads.
    op.execute("""
        CREATE TEMP TABLE issue134_exclusive_assets AS
        SELECT id FROM v2_assets WHERE id IN (SELECT id FROM issue134_assets)
        AND NOT EXISTS (SELECT 1 FROM v2_rendition_assets WHERE asset_id = v2_assets.id)
        AND NOT EXISTS (SELECT 1 FROM v2_score_revision_assets WHERE asset_id = v2_assets.id)
        AND NOT EXISTS (SELECT 1 FROM v2_works WHERE cover_asset_id = v2_assets.id)
        AND NOT EXISTS (SELECT 1 FROM v2_arrangements WHERE cover_asset_id = v2_assets.id)
        AND NOT EXISTS (SELECT 1 FROM v2_renditions WHERE cover_asset_id = v2_assets.id)
        AND NOT EXISTS (SELECT 1 FROM v2_releases WHERE cover_asset_id = v2_assets.id)
        AND NOT EXISTS (SELECT 1 FROM v2_lyric_source_documents WHERE document_asset_id = v2_assets.id)
        AND NOT EXISTS (SELECT 1 FROM v2_lyric_source_pages WHERE image_asset_id = v2_assets.id)
        AND NOT EXISTS (SELECT 1 FROM v2_client_images WHERE asset_id = v2_assets.id)
        AND NOT EXISTS (SELECT 1 FROM v2_upload_sessions WHERE completed_asset_id = v2_assets.id)
    """)
    op.execute("DELETE FROM v2_asset_sources WHERE asset_id IN (SELECT id FROM issue134_exclusive_assets)")
    op.execute("DELETE FROM v2_asset_locations WHERE asset_id IN (SELECT id FROM issue134_exclusive_assets)")
    op.execute("DELETE FROM v2_assets WHERE id IN (SELECT id FROM issue134_exclusive_assets)")
    for table in ("issue134_exclusive_assets", "issue134_assets", "issue134_uploads", "issue134_renditions"):
        op.execute(f"DROP TABLE {table}")


def downgrade() -> None:
    raise RuntimeError("online chorus data was permanently removed and cannot be reconstructed")
