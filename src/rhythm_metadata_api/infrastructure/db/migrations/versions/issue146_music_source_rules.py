"""Lyric filter rules and audio quality order on music source instances.

Revision ID: issue146sourcerules
Revises: issue145sourceconfig
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "issue146sourcerules"
down_revision: str | Sequence[str] | None = "issue145sourceconfig"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of the rules shipped by issue 144; later code changes must not alter this migration.
_FEINIAO_FILTERS = {
    "exact_lines": ["该音源由 飞鸟·轻音 提供服务", "官网:飞鸟.site", "————下面是正歌部分————"],
    "by_tag_contains": ["飞鸟·轻音", "飞鸟.site"],
}
_QUALITY_ORDER = ["mp3", "m4a", "ogg"]


def _rewrite(change) -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, adapter, config FROM v2_music_sources")).fetchall()
    for source_id, adapter, raw in rows:
        config = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        change(config, adapter)
        connection.execute(
            sa.text("UPDATE v2_music_sources SET config = :config WHERE id = :id"),
            {"config": json.dumps(config, ensure_ascii=False), "id": source_id},
        )


def upgrade() -> None:
    def add(config: dict, adapter: str) -> None:
        config["lyric_filters"] = (
            _FEINIAO_FILTERS if adapter == "feiniao" else {"exact_lines": [], "by_tag_contains": []}
        )
        config["audio_quality_order"] = _QUALITY_ORDER

    _rewrite(add)


def downgrade() -> None:
    def remove(config: dict, _adapter: str) -> None:
        config.pop("lyric_filters", None)
        config.pop("audio_quality_order", None)

    _rewrite(remove)
