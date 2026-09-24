from __future__ import annotations

from sqlalchemy import and_, exists, func, or_, select

from rhythm_metadata_api.application.unit_of_work import UnitOfWorkFactory
from rhythm_metadata_api.domain.v2.pending import (
    PendingAsset,
    PendingAssetSection,
    PendingScore,
    PendingScoreSection,
    PendingSummary,
    PendingUpload,
    PendingUploadSection,
)
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    Asset,
    AssetLocation,
    Score,
    UploadSession,
    Work,
    utc_now,
)


class PendingCenterService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self.uow_factory = uow_factory

    def summary(self, limit: int = 30) -> PendingSummary:
        now = utc_now()
        score_conditions = (
            Score.published_revision_id.is_(None),
            Score.deleted_at.is_(None),
            Arrangement.deleted_at.is_(None),
            Work.deleted_at.is_(None),
        )
        upload_condition = or_(
            UploadSession.state.in_(("failed", "expired")),
            and_(
                UploadSession.state.in_(("created", "uploaded")),
                UploadSession.expires_at <= now,
            ),
        )
        missing_location = exists(
            select(AssetLocation.id).where(
                AssetLocation.asset_id == Asset.id,
                AssetLocation.state == "missing",
            )
        )
        failed_asset_condition = and_(
            Asset.deleted_at.is_(None),
            or_(Asset.state == "rejected", missing_location),
        )
        pending_asset_condition = and_(
            Asset.deleted_at.is_(None), Asset.state == "pending_inspection"
        )
        with self.uow_factory() as uow:
            session = uow.session
            scores = (
                select(Score, Arrangement, Work)
                .join(Arrangement, Arrangement.id == Score.arrangement_id)
                .join(Work, Work.id == Arrangement.work_id)
                .where(*score_conditions)
            )
            score_count = session.scalar(
                select(func.count(Score.id))
                .join(Arrangement, Arrangement.id == Score.arrangement_id)
                .join(Work, Work.id == Arrangement.work_id)
                .where(*score_conditions)
            ) or 0
            score_rows = session.execute(
                scores.order_by(Score.updated_at.desc(), Score.id).limit(limit)
            ).all()
            upload_count = session.scalar(
                select(func.count(UploadSession.id)).where(upload_condition)
            ) or 0
            uploads = session.scalars(
                select(UploadSession)
                .where(upload_condition)
                .order_by(UploadSession.updated_at.desc(), UploadSession.id)
                .limit(limit)
            ).all()
            failed_asset_count = session.scalar(
                select(func.count(Asset.id)).where(failed_asset_condition)
            ) or 0
            failed_assets = session.scalars(
                select(Asset)
                .where(failed_asset_condition)
                .order_by(Asset.updated_at.desc(), Asset.id)
                .limit(limit)
            ).all()
            pending_asset_count = session.scalar(
                select(func.count(Asset.id)).where(pending_asset_condition)
            ) or 0
            pending_assets = session.scalars(
                select(Asset)
                .where(pending_asset_condition)
                .order_by(Asset.updated_at.desc(), Asset.id)
                .limit(limit)
            ).all()
            return PendingSummary(
                unpublished_scores=PendingScoreSection(
                    total=score_count,
                    items=[
                        PendingScore(
                            id=score.id,
                            label=score.label,
                            work_id=work.id,
                            work_title=work.canonical_title,
                            arrangement_name=arrangement.name,
                            head_revision_id=score.head_revision_id,
                            updated_at=score.updated_at,
                        )
                        for score, arrangement, work in score_rows
                    ],
                ),
                failed_uploads=PendingUploadSection(
                    total=upload_count,
                    items=[
                        PendingUpload(
                            id=upload.id,
                            state=(
                                "expired"
                                if upload.state in {"created", "uploaded"}
                                else upload.state
                            ),
                            original_filename=upload.original_filename,
                            media_type=upload.media_type,
                            source=upload.source,
                            expected_size=upload.expected_size,
                            expires_at=upload.expires_at,
                            updated_at=upload.updated_at,
                        )
                        for upload in uploads
                    ],
                ),
                failed_assets=PendingAssetSection(
                    total=failed_asset_count,
                    items=[
                        PendingAsset(
                            id=asset.id,
                            state="rejected" if asset.state == "rejected" else "location_missing",
                            media_type=asset.detected_media_type,
                            byte_size=asset.byte_size,
                            sha256=asset.sha256,
                            created_at=asset.created_at,
                        )
                        for asset in failed_assets
                    ],
                ),
                pending_assets=PendingAssetSection(
                    total=pending_asset_count,
                    items=[
                        PendingAsset(
                            id=asset.id,
                            state=asset.state,
                            media_type=asset.detected_media_type,
                            byte_size=asset.byte_size,
                            sha256=asset.sha256,
                            created_at=asset.created_at,
                        )
                        for asset in pending_assets
                    ],
                ),
            )
