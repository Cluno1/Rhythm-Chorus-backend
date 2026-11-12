from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from rhythm_metadata_api.application.music_health import MusicHealthScheduler
from rhythm_metadata_api.application.music_sources import MusicSourceService
from rhythm_metadata_api.core.config import Settings
from rhythm_metadata_api.infrastructure.db.models import Base, MusicSettings, MusicSource


def test_failure_recovery_and_manual_disable(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "health.db"}')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(MusicSettings(id='global', recommended_keywords=[], health_enabled=True,
                                  health_interval_minutes=60, revision=1))
        session.add_all([MusicSource(id='good', name='good', adapter='x', base_url='http://example.com/', config={}, enabled=True),
                         MusicSource(id='disabled', name='disabled', adapter='x', base_url='http://example.com/', config={}, enabled=False)])
    service = MusicSourceService(sessions, Settings())
    class Adapter:
        fail = True
        def search(self, *args):
            if self.fail:
                raise TimeoutError('timeout')
            return {'items': [{'id': '1'}]}
    adapter = Adapter()
    service._adapter = lambda source: adapter
    scheduler = MusicHealthScheduler(service, str(tmp_path / 'health.db'))
    scheduler.check_due()
    with sessions.begin() as session:
        row = session.get(MusicSource, 'good')
        assert row.health_failed and 'timeout' in row.health_error
        assert session.get(MusicSource, 'disabled').health_checked_at is None
        row.health_checked_at = datetime.now(UTC) - timedelta(minutes=61)
    assert [source.id for source in service._enabled_sources()] == ['good']
    import pytest

    from rhythm_metadata_api.domain.v2.errors import V2Unavailable
    with pytest.raises(V2Unavailable):
        service.search("周杰伦", 1)
    adapter.fail = False
    scheduler.check_due()
    with sessions() as session:
        row = session.get(MusicSource, 'good')
        assert not row.health_failed and row.health_error is None

        assert not session.get(MusicSource, 'disabled').enabled
    with sessions.begin() as session:
        session.get(MusicSettings, 'global').health_enabled = False
        session.get(MusicSource, 'good').health_checked_at = None
    adapter.fail = True
    scheduler.check_due()
    with sessions() as session:
        assert not session.get(MusicSource, 'good').health_failed
    scheduler.check_due(force=True)
    with sessions() as session:
        assert session.get(MusicSource, 'good').health_failed
        assert session.get(MusicSource, 'disabled').health_checked_at is None
