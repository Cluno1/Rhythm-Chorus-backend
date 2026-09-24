from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, func, select

from rhythm_metadata_api.application.pending_center import PendingCenterService
from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    Asset,
    ChangeEvent,
    ChangeEventWork,
    ChorusProject,
    Rendition,
    Score,
    Work,
    utc_now,
)


class OverviewService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self.uow_factory = uow_factory

    def summary(self, pending_tracks: int) -> dict[str, object]:
        pending = PendingCenterService(self.uow_factory).summary(limit=0)
        with self.uow_factory() as uow:
            session = uow.session
            live_works = (Work.deleted_at.is_(None),)
            live_arrangements = (*live_works, Arrangement.deleted_at.is_(None))
            live_scores = (*live_arrangements, Score.deleted_at.is_(None))
            live_renditions = (*live_arrangements, Rendition.deleted_at.is_(None))

            def count(statement: Select[tuple[int]]) -> int:
                return int(session.scalar(statement) or 0)

            counts = {
                "works": count(select(func.count(Work.id)).where(*live_works)),
                "active_works": count(
                    select(func.count(Work.id)).where(*live_works, Work.status == "active")
                ),
                "arrangements": count(
                    select(func.count(Arrangement.id))
                    .join(Work, Work.id == Arrangement.work_id)
                    .where(*live_arrangements)
                ),
                "scores": count(
                    select(func.count(Score.id))
                    .join(Arrangement, Arrangement.id == Score.arrangement_id)
                    .join(Work, Work.id == Arrangement.work_id)
                    .where(*live_scores)
                ),
                "published_scores": count(
                    select(func.count(Score.id))
                    .join(Arrangement, Arrangement.id == Score.arrangement_id)
                    .join(Work, Work.id == Arrangement.work_id)
                    .where(*live_scores, Score.published_revision_id.is_not(None))
                ),
                "renditions": count(
                    select(func.count(Rendition.id))
                    .join(Arrangement, Arrangement.id == Rendition.arrangement_id)
                    .join(Work, Work.id == Arrangement.work_id)
                    .where(*live_renditions)
                ),
                "assets": count(
                    select(func.count(Asset.id)).where(Asset.deleted_at.is_(None))
                ),
                "ready_assets": count(
                    select(func.count(Asset.id)).where(
                        Asset.deleted_at.is_(None), Asset.state == "ready"
                    )
                ),
                "chorus_projects": count(
                    select(func.count(ChorusProject.id))
                    .join(Work, Work.id == ChorusProject.work_id)
                    .where(ChorusProject.deleted_at.is_(None), *live_works)
                ),
            }
            recent_works = session.scalars(
                select(Work)
                .where(*live_works)
                .order_by(Work.updated_at.desc(), Work.id)
                .limit(6)
            ).all()
            recent_events = session.execute(
                select(ChangeEvent, Work)
                .join(ChangeEventWork, ChangeEventWork.event_sequence == ChangeEvent.sequence)
                .join(Work, Work.id == ChangeEventWork.work_id)
                .where(*live_works)
                .order_by(ChangeEvent.sequence.desc(), Work.id)
                .limit(8)
            ).all()

            return {
                "generated_at": utc_now().isoformat(),
                "counts": counts,
                "attention": {
                    "pending_tracks": pending_tracks,
                    "unpublished_scores": pending.unpublished_scores.total,
                    "failed_uploads": pending.failed_uploads.total,
                    "failed_assets": pending.failed_assets.total,
                    "pending_assets": pending.pending_assets.total,
                },
                "recent_works": [
                    {
                        "id": work.id,
                        "title": work.canonical_title,
                        "status": work.status,
                        "cover_asset_id": work.cover_asset_id,
                        "updated_at": _utc_isoformat(work.updated_at),
                    }
                    for work in recent_works
                ],
                "recent_events": [
                    {
                        "sequence": event.sequence,
                        "operation": event.operation,
                        "entity_type": event.entity_type,
                        "work_id": work.id,
                        "work_title": work.canonical_title,
                        "created_at": _utc_isoformat(event.created_at),
                    }
                    for event, work in recent_events
                ],
            }


def _utc_isoformat(value: datetime) -> str:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
