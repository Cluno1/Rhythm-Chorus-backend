"""Per-source upstream rules editable by administrators.

Revision ID: issue145sourceconfig
Revises: issue141metadata
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue145sourceconfig"
down_revision: str | Sequence[str] | None = "issue141metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("v2_music_sources") as batch:
        batch.add_column(sa.Column("config", sa.JSON(), nullable=False, server_default="{}"))
    op.execute(sa.text(
        "UPDATE v2_music_sources SET config = :config WHERE adapter = 'feiniao'"
    ).bindparams(config=json.dumps({
        "audio_hosts": ["car-er.kuwo.cn", "car-bj.kuwo.cn", "car-lv.kuwo.cn"],
        "artwork_hosts": ["img1.kuwo.cn", "img2.kuwo.cn", "img3.kuwo.cn", "img4.kuwo.cn"],
        "upstream_source": "酷我音乐", "audio_url_ttl_seconds": 150,
    }, ensure_ascii=False)))
def downgrade() -> None:
    with op.batch_alter_table("v2_music_sources") as batch:
        batch.drop_column("config")
