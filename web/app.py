"""Flask app — SaaS dashboard + SSE live updates + auth + billing."""
from __future__ import annotations

import asyncio
import json
import os
import queue
import re
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from flask import Flask, Response, abort, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from kdp_checker.checker import MarketplaceChecker
from kdp_checker.intelligence import analyze_results
from kdp_checker.email_gen import generate_emails
from kdp_checker.marketplaces import MARKETPLACES, MARKETPLACES_BY_CODE
from kdp_checker import storage, scheduler as monitor_scheduler

from web.auth import login_manager, auth_bp
from web.billing import billing_bp, PLANS


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    login_manager.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(billing_bp)

    # In-memory job state (per-process). For multi-worker, move to Redis.
    jobs: dict[str, queue.Queue] = {}
    job_results: dict[str, list[dict]] = {}
    job_reports: dict[str, dict] = {}
    job_emails: dict[str, list[dict]] = {}

    _ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

    # ---------- pages ----------

    @app.get("/")
    def index():
        return render_template("index.html", marketplaces=MARKETPLACES, plans=PLANS)

    @app.get("/dashboard")
    @login_required
    def dashboard():
        with storage.connect() as conn:
            asins = storage.list_user_asins(conn, current_user.user_id)
            checks = storage.recent_checks_for_user(conn, current_user.user_id, limit=25)
            events = storage.change_events_for_user(conn, current_user.user_id, limit=25)
        return render_template("dashboard.html", asins=asins, checks=checks,
                               events=events, plans=PLANS)

    @app.get("/history/<asin>")
    @login_required
    def history(asin):
        asin = asin.upper()
        if not _ASIN_RE.match(asin):
            abort(400)
        with storage.connect() as conn:
            rows = storage.recent_checks_for_asin(conn, asin, limit=60)
        return render_template("history.html", asin=asin, rows=rows)

    # ---------- APIs ----------

    @app.get("/api/marketplaces")
    def list_marketplaces():
        return jsonify([{"code": m.code, "country": m.country, "domain": m.domain,
                         "currency": m.currency} for m in MARKETPLACES])

    @app.post("/api/check")
    def start_check():
        data = request.get_json(silent=True) or request.form
        asin = (data.get("asin") or "").strip().upper()
        if not _ASIN_RE.match(asin):
            return jsonify({"error": "Invalid ASIN"}), 400

        # Plan-based limits
        plan = current_user.plan if current_user.is_authenticated else "free"
        limit = PLANS.get(plan, PLANS["free"])["asin_limit"]

        codes = data.get("codes") or []
        if isinstance(codes, str):
            codes = [c.strip().upper() for c in codes.split(",") if c.strip()]
        targets = ([MARKETPLACES_BY_CODE[c] for c in codes if c in MARKETPLACES_BY_CODE]
                   if codes else list(MARKETPLACES))
        if not targets:
            return jsonify({"error": "No valid marketplaces"}), 400

        job_id = uuid.uuid4().hex
        q: queue.Queue = queue.Queue()
        jobs[job_id] = q
        job_results[job_id] = []

        concurrency = int(data.get("concurrency", 4))
        retries = int(data.get("retries", 3))
        author_name = (data.get("author_name") or "").strip() or None
        book_title = (data.get("book_title") or "").strip() or None
        user_id = current_user.user_id if current_user.is_authenticated else None
        monitor = bool(data.get("monitor"))

        use_browser = os.environ.get("USE_BROWSER_FALLBACK", "1") == "1"

        def worker():
            async def run():
                checker = MarketplaceChecker(
                    concurrency=concurrency, max_retries=retries,
                    use_browser_fallback=use_browser,
                )

                def on_progress(result):
                    payload = result.to_dict()
                    job_results[job_id].append(payload)
                    q.put(("result", payload))

                try:
                    results = await checker.run(asin, targets, progress_cb=on_progress)
                    report = analyze_results(asin, results)
                    emails = generate_emails(asin, report, results, author_name, book_title)
                    job_reports[job_id] = report.to_dict()
                    job_emails[job_id] = [e.to_dict() for e in emails]
                    q.put(("intelligence", job_reports[job_id]))
                    q.put(("emails", job_emails[job_id]))
                    if user_id:
                        try:
                            with storage.connect() as conn:
                                storage.upsert_asin(conn, user_id, asin, book_title,
                                                    author_name, monitor)
                                storage.save_check(conn, asin, report, results, user_id)
                        except Exception as e:
                            q.put(("error", {"message": f"DB save failed: {e}"}))
                    q.put(("done", {"asin": asin, "total": len(targets)}))
                except Exception as e:
                    q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
                finally:
                    q.put(("close", None))

            asyncio.run(run())

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"job_id": job_id, "total": len(targets)})

    @app.get("/api/stream/<job_id>")
    def stream(job_id):
        q = jobs.get(job_id)
        if q is None:
            return jsonify({"error": "Unknown job"}), 404

        def gen():
            while True:
                event, payload = q.get()
                if event == "close":
                    break
                yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            jobs.pop(job_id, None)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/results/<job_id>")
    def results(job_id):
        return jsonify({
            "results": job_results.get(job_id, []),
            "intelligence": job_reports.get(job_id),
            "emails": job_emails.get(job_id, []),
        })

    # ---------- Start the scheduler if enabled ----------
    if os.environ.get("ENABLE_SCHEDULER", "0") == "1":
        monitor_scheduler.start(
            schedule_hour=int(os.environ.get("SCHEDULER_HOUR", "7")),
            schedule_minute=int(os.environ.get("SCHEDULER_MINUTE", "0")),
        )

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
