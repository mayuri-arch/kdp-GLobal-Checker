"""SQLite persistence for users, ASINs, checks, history, and change events.

Schema is intentionally simple — no ORM. Migrations are idempotent: every
`connect()` runs `CREATE TABLE IF NOT EXISTS`. For production SaaS you'd
swap this for Postgres + Alembic, but the query surface here maps directly.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .checker import CheckResult
from .intelligence import IntelligenceReport


DEFAULT_DB = Path(os.environ.get("KDP_DB", "kdp_checker.db"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    name TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS asins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    asin TEXT NOT NULL,
    book_title TEXT,
    author_name TEXT,
    monitoring_enabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE(user_id, asin),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asin TEXT NOT NULL,
    user_id INTEGER,
    ran_at INTEGER NOT NULL,
    revenue_score INTEGER,
    live_count INTEGER,
    total INTEGER,
    summary TEXT,
    report_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_asin_ran ON checks(asin, ran_at DESC);
CREATE INDEX IF NOT EXISTS idx_checks_user ON checks(user_id, ran_at DESC);

CREATE TABLE IF NOT EXISTS marketplace_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    status TEXT NOT NULL,
    has_buy_button INTEGER,
    price_value REAL,
    currency TEXT,
    title TEXT,
    result_json TEXT NOT NULL,
    FOREIGN KEY (check_id) REFERENCES checks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mr_check ON marketplace_results(check_id);

CREATE TABLE IF NOT EXISTS change_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asin TEXT NOT NULL,
    user_id INTEGER,
    marketplace_code TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    detected_at INTEGER NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_change_asin ON change_events(asin, detected_at DESC);
"""


@contextmanager
def connect(db_path: str | Path = DEFAULT_DB):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- Users ----------

def create_user(conn, email: str, password_hash: str, name: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, name, created_at) VALUES (?, ?, ?, ?)",
        (email.lower().strip(), password_hash, name, int(time.time())),
    )
    return cur.lastrowid


def get_user_by_email(conn, email: str):
    return conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
    ).fetchone()


def get_user(conn, user_id: int):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def update_user_plan(conn, user_id: int, plan: str,
                     stripe_customer_id: str | None = None,
                     stripe_subscription_id: str | None = None):
    conn.execute(
        "UPDATE users SET plan = ?, stripe_customer_id = COALESCE(?, stripe_customer_id),"
        " stripe_subscription_id = COALESCE(?, stripe_subscription_id) WHERE id = ?",
        (plan, stripe_customer_id, stripe_subscription_id, user_id),
    )


# ---------- ASINs ----------

def upsert_asin(conn, user_id: int, asin: str,
                book_title: str | None = None, author_name: str | None = None,
                monitoring_enabled: bool = False) -> int:
    row = conn.execute(
        "SELECT id FROM asins WHERE user_id = ? AND asin = ?", (user_id, asin)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE asins SET book_title = COALESCE(?, book_title),"
            " author_name = COALESCE(?, author_name),"
            " monitoring_enabled = ? WHERE id = ?",
            (book_title, author_name, 1 if monitoring_enabled else 0, row["id"]),
        )
        return row["id"]
    cur = conn.execute(
        "INSERT INTO asins (user_id, asin, book_title, author_name,"
        " monitoring_enabled, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, asin, book_title, author_name,
         1 if monitoring_enabled else 0, int(time.time())),
    )
    return cur.lastrowid


def list_monitored_asins(conn):
    return conn.execute(
        "SELECT a.*, u.email FROM asins a JOIN users u ON u.id = a.user_id"
        " WHERE a.monitoring_enabled = 1"
    ).fetchall()


def list_user_asins(conn, user_id: int):
    return conn.execute(
        "SELECT * FROM asins WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()


# ---------- Checks & history ----------

def save_check(conn, asin: str, report: IntelligenceReport,
               results: Iterable[CheckResult], user_id: int | None = None) -> int:
    ran_at = int(time.time())
    cur = conn.execute(
        "INSERT INTO checks (asin, user_id, ran_at, revenue_score, live_count, total, summary, report_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (asin, user_id, ran_at, report.revenue_score, report.live_count,
         report.total, report.summary, json.dumps(report.to_dict(), ensure_ascii=False)),
    )
    check_id = cur.lastrowid
    for r in results:
        a = r.analysis
        conn.execute(
            "INSERT INTO marketplace_results (check_id, code, status, has_buy_button,"
            " price_value, currency, title, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (check_id, r.marketplace.code, a.status.value,
             1 if a.has_buy_button else 0, a.price_value, a.currency, a.title,
             json.dumps(r.to_dict(), ensure_ascii=False)),
        )
    _record_change_events(conn, asin, user_id, results, ran_at)
    return check_id


def _record_change_events(conn, asin, user_id, results, ran_at):
    # Compare vs. the previous marketplace_results for this ASIN
    prev = {}
    row = conn.execute(
        "SELECT id FROM checks WHERE asin = ? AND id < (SELECT MAX(id) FROM checks WHERE asin = ?)"
        " ORDER BY id DESC LIMIT 1", (asin, asin)
    ).fetchone()
    if row:
        for mr in conn.execute(
            "SELECT code, status FROM marketplace_results WHERE check_id = ?", (row["id"],)
        ):
            prev[mr["code"]] = mr["status"]
    for r in results:
        new_status = r.analysis.status.value
        old = prev.get(r.marketplace.code)
        if old and old != new_status:
            conn.execute(
                "INSERT INTO change_events (asin, user_id, marketplace_code,"
                " from_status, to_status, detected_at) VALUES (?, ?, ?, ?, ?, ?)",
                (asin, user_id, r.marketplace.code, old, new_status, ran_at),
            )


def recent_checks_for_user(conn, user_id: int, limit: int = 50):
    return conn.execute(
        "SELECT id, asin, ran_at, revenue_score, live_count, total, summary"
        " FROM checks WHERE user_id = ? ORDER BY ran_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


def recent_checks_for_asin(conn, asin: str, limit: int = 20):
    return conn.execute(
        "SELECT id, ran_at, revenue_score, live_count, total, summary"
        " FROM checks WHERE asin = ? ORDER BY ran_at DESC LIMIT ?",
        (asin, limit),
    ).fetchall()


def change_events_for_user(conn, user_id: int, limit: int = 50):
    return conn.execute(
        "SELECT * FROM change_events WHERE user_id = ? ORDER BY detected_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


def pending_notifications(conn):
    return conn.execute(
        "SELECT c.*, u.email FROM change_events c JOIN users u ON u.id = c.user_id"
        " WHERE c.notified = 0 ORDER BY c.detected_at ASC LIMIT 200"
    ).fetchall()


def mark_notified(conn, change_ids: list[int]):
    if not change_ids:
        return
    placeholders = ",".join(["?"] * len(change_ids))
    conn.execute(f"UPDATE change_events SET notified = 1 WHERE id IN ({placeholders})", change_ids)
