"""Periodic search probes; an OS lock elects one scheduler per shared database."""
from __future__ import annotations

import fcntl
import logging
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
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
            if not self.service._allows(source, "song_search", healthy=False):
                continue
            checked = source.health_checked_at
            if not force and checked and checked.replace(tzinfo=UTC) + timedelta(minutes=interval) > now:
                continue
            started = time.monotonic()
            try:
                if not self.service._search_slots.acquire(blocking=False):
                    raise TimeoutError('搜索检测并发已满')
                def probe(item=source):
                    return self.service._adapter(item).search(item.base_url, item.id, '周杰伦', 1)
                try:
                    future = self.service._search_pool.submit(probe)
                except RuntimeError:
                    self.service._search_slots.release()
                    raise
                future.add_done_callback(lambda _: self.service._search_slots.release())
                try:
                    result = future.result(timeout=self.service._capability(source, "song_search")["timeout_ms"] / 1000)
                except FutureTimeout as exc:
                    if future.done():
                        raise
                    future.cancel()
                    raise TimeoutError('search probe timeout') from exc
                if not result.get('items'):
                    raise ValueError('检测搜索未返回歌曲')
                error = None
            except Exception as exc:  # noqa: BLE001 - isolate unexpected adapter failures
                error = f'{type(exc).__name__}: {exc!s}'[:300]
            with self.service.sessions() as session:
                settings = session.get(MusicSettings, 'global')
                if not force and settings is not None and not settings.health_enabled:
                    return
                health = dict(source.operation_health or {})
                health['song_search'] = {
                    'state': 'unhealthy' if error is not None else 'healthy',
                    'checked_at': utc_now().isoformat(),
                    'latency_ms': round((time.monotonic() - started) * 1000),
                    'error': error,
                }
                session.execute(update(MusicSource).where(
                    MusicSource.id == source.id, MusicSource.enabled.is_(True),
                    MusicSource.revision == source.revision,
                ).values(operation_health=health, health_failed=error is not None,
                         health_checked_at=utc_now(), health_error=error,
                         health_latency_ms=round((time.monotonic() - started) * 1000)))
                session.commit()
            log.info('Music health check source=%s result=%s', source.id, error or 'ok')
