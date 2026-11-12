"""Periodic search probes; an OS lock elects one scheduler per shared database."""
from __future__ import annotations

import fcntl
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from rhythm_metadata_api.infrastructure.db.models import MusicSettings, MusicSource, utc_now

log = logging.getLogger(__name__)


class MusicHealthScheduler:
    def __init__(self, service, database_path: str):
        self.service = service
        self.lock_path = database_path + '.music-health.lock'
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name='music-health', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                with open(self.lock_path, 'a') as lock:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        self.check_due()
            except Exception:
                log.exception('Music health scheduler failed')
            self.stop_event.wait(15)

    def trigger(self):
        lock = open(self.lock_path, "a")  # noqa: SIM115 - lock is owned by the background thread
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return False

        def run():
            try:
                self.check_due(force=True)
            except Exception:
                log.exception("Manual music health check failed")
            finally:
                lock.close()

        threading.Thread(target=run, name="music-health-manual", daemon=True).start()
        return True

    def check_due(self, *, force=False):
        with self.service.sessions() as session:
            settings = session.get(MusicSettings, 'global')
            if not force and settings is not None and not settings.health_enabled:
                return
            interval = settings.health_interval_minutes if settings else 60
            sources = list(session.scalars(select(MusicSource).where(MusicSource.enabled.is_(True))))
        now = datetime.now(UTC)
        for source in sources:
            if self.stop_event.is_set():
                return
            checked = source.health_checked_at
            if not force and checked and checked.replace(tzinfo=UTC) + timedelta(minutes=interval) > now:
                continue
            started = time.monotonic()
            try:
                result = self.service._adapter(source).search(source.base_url, source.id, '周杰伦', 1)
                if time.monotonic() - started > 8:
                    raise TimeoutError('搜索检测超过 8 秒')
                if not result.get('items'):
                    raise ValueError('检测搜索未返回歌曲')
                error = None
            except Exception as exc:  # noqa: BLE001 - isolate unexpected adapter failures
                error = f'{type(exc).__name__}: {exc!s}'[:300]
            with self.service.sessions() as session:
                settings = session.get(MusicSettings, 'global')
                if not force and settings is not None and not settings.health_enabled:
                    return
                session.execute(update(MusicSource).where(
                    MusicSource.id == source.id, MusicSource.enabled.is_(True),
                    MusicSource.revision == source.revision,
                ).values(health_failed=error is not None, health_checked_at=utc_now(), health_error=error,
                         health_latency_ms=round((time.monotonic() - started) * 1000)))
                session.commit()
            log.info('Music health check source=%s result=%s', source.id, error or 'ok')
