from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from rhythm_metadata_api.application.formal_import import (
    FormalCatalogImporter,
    FormalImportError,
    ScoreManifestRow,
    SongMappingRow,
)
from rhythm_metadata_api.infrastructure.db.database import create_v2_engine, migrate_v2_database
from rhythm_metadata_api.infrastructure.db.models import (
    Arrangement,
    AssetLocation,
    ChangeEvent,
    ChangeEventWork,
    Release,
    ReleaseItem,
    Rendition,
    Score,
    ScoreRevision,
    Work,
)


def score_rows() -> list[ScoreManifestRow]:
    return [
        ScoreManifestRow(
            mongo_id="gmusic:343:rev1",
            work_key="gmusic:343",
            revision_no=1,
            title="你的信实广大",
            status="superseded",
            cos_bucket="musicxml-1328751369",
            cos_key="gmusic/343/rev1.musicxml",
            sha256="1" * 64,
            byte_size=100,
            verified=True,
        ),
        ScoreManifestRow(
            mongo_id="gmusic:343:rev2",
            work_key="gmusic:343",
            revision_no=2,
            title="你的信实广大",
            status="canonical",
            cos_bucket="musicxml-1328751369",
            cos_key="gmusic/343/rev2.musicxml",
            sha256="2" * 64,
            byte_size=120,
            lyrics="lyrics",
            verified=True,
        ),
    ]


def song_rows() -> list[SongMappingRow]:
    return [
        SongMappingRow(
            album_key="ihope",
            mp3_cos_key="music/109-你的信实广大.mp3",
            source_filename="109-你的信实广大.mp3",
            song_title="你的信实广大",
            language_variant=None,
            track_no_hint=109,
            mongo_work_key="gmusic:343",
            score_status="canonical",
            match_method="normalized-exact",
            confidence="high",
            evidence="title",
            mp3_sha256="a" * 64,
            byte_size=1000,
            duration_ms=180000,
            cos_bucket="bible-1328751369",
            verified=True,
        ),
        SongMappingRow(
            album_key="ihope",
            mp3_cos_key="music/999-Only Song-English.mp3",
            source_filename="999-Only Song-English.mp3",
            song_title="Only Song",
            language_variant="English",
            track_no_hint=999,
            mongo_work_key=None,
            score_status="no-score",
            match_method="song-only",
            confidence="medium",
            evidence="no score",
            mp3_sha256="b" * 64,
            byte_size=2000,
            duration_ms=None,
            cos_bucket="bible-1328751369",
            verified=True,
        ),
    ]


def test_formal_import_is_direct_cos_and_idempotent() -> None:
    engine = create_v2_engine(":memory:")
    migrate_v2_database(engine, ":memory:")
    with Session(engine) as session, session.begin():
        importer = FormalCatalogImporter(session)
        expected = importer.validate(score_rows(), song_rows())
        first = importer.import_all(score_rows(), song_rows())
        second = importer.import_all(score_rows(), song_rows())
        assert first == expected == second
        assert first.works == 2
        assert first.scores == 1
        assert first.score_revisions == 2
        assert first.songs == 2
        assert first.albums == 1
        assert first.release_items == 2
        assert first.assets == 4
        assert first.cos_locations == 4
        assert not list(
            session.scalars(select(AssetLocation).where(AssetLocation.provider == "local"))
        )
        release = session.scalar(select(Release).where(Release.key == "ihope"))
        assert release is not None
        assert len(list(session.scalars(select(ReleaseItem)))) == 2
        score = session.scalar(select(Score))
        assert score is not None
        assert score.head_revision_id == score.published_revision_id
        assert len(list(session.scalars(select(ScoreRevision)))) == 2
        assert len(list(session.scalars(select(Rendition)))) == 2
        assert len(list(session.scalars(select(Work)))) == 2
        only_song_work = session.scalar(select(Work).where(Work.canonical_title == "Only Song"))
        assert only_song_work is not None
        assert only_song_work.language == "en"
        assert len(list(session.scalars(select(Arrangement)))) == 2
        assert len(list(session.scalars(select(ChangeEvent)))) == 10
        assert len(list(session.scalars(select(ChangeEventWork)))) == 11
        revisions = list(session.scalars(select(ScoreRevision).order_by(ScoreRevision.revision_no)))
        assert revisions[0].based_on_revision_id is None
        assert revisions[1].based_on_revision_id == revisions[0].id


def test_formal_import_rejects_unverified_or_wrong_album() -> None:
    engine = create_v2_engine(":memory:")
    migrate_v2_database(engine, ":memory:")
    with Session(engine) as session:
        importer = FormalCatalogImporter(session)
        with pytest.raises(FormalImportError, match="hash-verified"):
            importer.validate([replace(score_rows()[0], verified=False)], song_rows())
        with pytest.raises(FormalImportError, match="album_key=ihope"):
            importer.validate(score_rows(), [replace(song_rows()[0], album_key="other")])
