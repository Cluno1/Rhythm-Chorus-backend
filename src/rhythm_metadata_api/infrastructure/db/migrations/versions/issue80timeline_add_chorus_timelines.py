"""issue 80: split logical chorus projects from score revision timelines

Revision ID: issue80timeline
Revises: issue80chorus
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue80timeline"
down_revision: str | Sequence[str] | None = "issue80chorus"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("v2_chorus_projects") as batch:
        batch.add_column(sa.Column("score_id", sa.String(length=36), nullable=True))
    op.execute(
        "UPDATE v2_chorus_projects "
        "SET score_id = ("
        "SELECT score_id FROM v2_score_revisions "
        "WHERE v2_score_revisions.id = v2_chorus_projects.alignment_score_revision_id"
        ")"
    )
    with op.batch_alter_table("v2_chorus_projects") as batch:
        batch.alter_column("score_id", existing_type=sa.String(length=36), nullable=False)
        batch.create_foreign_key(
            "fk_v2_chorus_projects_score_id",
            "v2_scores",
            ["score_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "v2_chorus_projects_score_idx",
        "v2_chorus_projects",
        ["work_id", "score_id"],
    )

    op.create_table(
        "v2_chorus_timelines",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("chorus_project_id", sa.String(length=36), nullable=False),
        sa.Column("score_revision_id", sa.String(length=36), nullable=False),
        sa.Column("timeline_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["chorus_project_id"], ["v2_chorus_projects.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["score_revision_id"], ["v2_score_revisions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "chorus_project_id",
            "score_revision_id",
            name="uq_v2_chorus_timeline_revision",
        ),
    )
    op.create_index(
        "v2_chorus_timelines_project_idx",
        "v2_chorus_timelines",
        ["chorus_project_id", "score_revision_id"],
    )
    # Reusing the old project UUID makes the compatibility backfill deterministic:
    # every pre-migration track and mix points to its project's original timeline.
    op.execute(
        "INSERT INTO v2_chorus_timelines "
        "(id, chorus_project_id, score_revision_id, timeline_hash, revision, "
        "created_at, updated_at, deleted_at) "
        "SELECT id, id, alignment_score_revision_id, timeline_hash, 1, "
        "created_at, updated_at, deleted_at FROM v2_chorus_projects"
    )

    with op.batch_alter_table("v2_chorus_tracks") as batch:
        batch.add_column(sa.Column("chorus_timeline_id", sa.String(length=36), nullable=True))
    op.execute("UPDATE v2_chorus_tracks SET chorus_timeline_id = chorus_project_id")
    with op.batch_alter_table("v2_chorus_tracks") as batch:
        batch.alter_column(
            "chorus_timeline_id", existing_type=sa.String(length=36), nullable=False
        )
        batch.create_foreign_key(
            "fk_v2_chorus_tracks_timeline_id",
            "v2_chorus_timelines",
            ["chorus_timeline_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "v2_chorus_tracks_timeline_idx",
        "v2_chorus_tracks",
        ["chorus_timeline_id", "status"],
    )

    with op.batch_alter_table("v2_chorus_mix_variants") as batch:
        batch.add_column(sa.Column("chorus_timeline_id", sa.String(length=36), nullable=True))
    op.execute("UPDATE v2_chorus_mix_variants SET chorus_timeline_id = chorus_project_id")
    with op.batch_alter_table("v2_chorus_mix_variants") as batch:
        batch.alter_column(
            "chorus_timeline_id", existing_type=sa.String(length=36), nullable=False
        )
        batch.create_foreign_key(
            "fk_v2_chorus_mix_timeline_id",
            "v2_chorus_timelines",
            ["chorus_timeline_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "v2_chorus_mix_timeline_idx",
        "v2_chorus_mix_variants",
        ["chorus_timeline_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("v2_chorus_mix_timeline_idx", table_name="v2_chorus_mix_variants")
    with op.batch_alter_table("v2_chorus_mix_variants") as batch:
        batch.drop_constraint("fk_v2_chorus_mix_timeline_id", type_="foreignkey")
        batch.drop_column("chorus_timeline_id")

    op.drop_index("v2_chorus_tracks_timeline_idx", table_name="v2_chorus_tracks")
    with op.batch_alter_table("v2_chorus_tracks") as batch:
        batch.drop_constraint("fk_v2_chorus_tracks_timeline_id", type_="foreignkey")
        batch.drop_column("chorus_timeline_id")

    op.drop_index("v2_chorus_timelines_project_idx", table_name="v2_chorus_timelines")
    op.drop_table("v2_chorus_timelines")
    op.drop_index("v2_chorus_projects_score_idx", table_name="v2_chorus_projects")
    with op.batch_alter_table("v2_chorus_projects") as batch:
        batch.drop_constraint("fk_v2_chorus_projects_score_id", type_="foreignkey")
        batch.drop_column("score_id")
