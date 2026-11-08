"""One music configuration shared by every source (recommended keywords).

Revision ID: issue181musicsettings
Revises: issue166lyrics
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "issue181musicsettings"
down_revision: str | Sequence[str] | None = "issue166lyrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same words every source used by default before this revision (issue 171).
_KEYWORDS = [
    "流行", "民谣", "摇滚", "古典", "治愈", "轻音乐", "爵士", "粤语", "经典老歌", "华语",
    "欧美", "日语", "说唱", "电子", "学习", "运动", "睡前", "咖啡馆", "钢琴", "怀旧",
]


def upgrade() -> None:
    table = op.create_table(
        "v2_music_settings",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("recommended_keywords", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(table, [{
        "id": "global", "recommended_keywords": _KEYWORDS, "revision": 1,
        "updated_at": datetime.now(UTC),
    }])


def downgrade() -> None:
    op.drop_table("v2_music_settings")
