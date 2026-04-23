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
    """Start the background scheduler (idempotent)."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
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
