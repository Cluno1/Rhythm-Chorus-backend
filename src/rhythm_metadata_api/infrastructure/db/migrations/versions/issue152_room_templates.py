"""Administrator room templates and their ordered sections.

Revision ID: issue152templates
Revises: issue146sourcerules
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue152templates"
down_revision: str | Sequence[str] | None = "issue146sourcerules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v2_room_templates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "v2_room_template_sections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "template_id", sa.String(36),
            sa.ForeignKey("v2_room_templates.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("assignee_user_id", sa.String(100), nullable=True),
    )
    op.create_index(
        "v2_room_template_sections_template_idx",
        "v2_room_template_sections", ["template_id", "position"],
    )


def downgrade() -> None:
    op.drop_index("v2_room_template_sections_template_idx", table_name="v2_room_template_sections")
    op.drop_table("v2_room_template_sections")
    op.drop_table("v2_room_templates")
