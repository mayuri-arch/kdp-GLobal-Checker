"""Daily background checks for every monitored ASIN.

Uses APScheduler's BackgroundScheduler (in-process, good for single-server
SaaS). For horizontal scale, swap to Celery + Redis + beat.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .checker import MarketplaceChecker
from .intelligence import analyze_results
from . import storage


log = logging.getLogger("kdp.scheduler")
_scheduler: BackgroundScheduler | None = None


def _run_checks_once():
    """Fetch every monitored ASIN and persist a new check."""
    with storage.connect() as conn:
        monitored = list(storage.list_monitored_asins(conn))
    if not monitored:
        log.info("No monitored ASINs; skipping scheduled run.")
        return

    log.info("Running scheduled checks for %d ASIN(s)", len(monitored))
    # APScheduler runs us on a worker thread — open a fresh asyncio loop here.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        for row in monitored:
            asin = row["asin"]
            try:
                checker = MarketplaceChecker(concurrency=3, max_retries=3)
                results = loop.run_until_complete(checker.run(asin))
                report = analyze_results(asin, results)
                with storage.connect() as conn:
                    storage.save_check(conn, asin, report, results, user_id=row["user_id"])
                log.info("asin=%s score=%s live=%s/%s", asin,
                         report.revenue_score, report.live_count, report.total)
            except Exception:
                log.exception("Scheduled check failed for ASIN %s", asin)
    finally:
        loop.close()


def start(schedule_hour: int = 7, schedule_minute: int = 0):
    """Start the background scheduler (idempotent, multi-worker safe).

    Uses an OS-level file lock so that only ONE gunicorn/uvicorn worker
    actually schedules jobs. Other workers silently no-op. If the lock can't
    be acquired, we assume another worker owns the scheduler.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    # Multi-worker guard — best effort, cross-platform (Windows + POSIX).
    lock_path = os.environ.get("KDP_SCHEDULER_LOCK", "/tmp/kdp_scheduler.lock")
    try:
        # O_EXCL creates the file atomically; fails if it exists.
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        # Best-effort cleanup on interpreter exit
        import atexit
        atexit.register(lambda: os.path.exists(lock_path) and os.remove(lock_path))
    except FileExistsError:
        log.info("Scheduler lock %s already held — skipping in this worker.", lock_path)
        return None
    except OSError as e:
        log.warning("Scheduler lock unavailable (%s); continuing anyway.", e)

    _scheduler = BackgroundScheduler(timezone="UTC")
    # Daily at the configured UTC hour
    _scheduler.add_job(
        _run_checks_once,
        CronTrigger(hour=schedule_hour, minute=schedule_minute),
        id="daily_checks", replace_existing=True, max_instances=1,
    )
    _scheduler.start()
    log.info("Scheduler started; daily at %02d:%02d UTC", schedule_hour, schedule_minute)
    return _scheduler


def stop():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def run_now():
    """Trigger one immediate pass — useful for CLI/admin."""
    threading.Thread(target=_run_checks_once, daemon=True).start()
