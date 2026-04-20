"""
SQLite persistence layer.

Two concerns live here:
  1. alerts          — one row per graph run; tracks state through the workflow
  2. audit_log       — append-only log of every significant action
  3. settings        — key/value store for runtime configuration

LangGraph uses its OWN SqliteSaver (checkpointer) for internal graph state.
This module is our application-level store that the Streamlit UI reads from.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "legal_agent.db"
_lock = threading.Lock()


# ── Connection helper ─────────────────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Schema bootstrap ──────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables if they do not exist. Safe to call on every startup."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                id               TEXT PRIMARY KEY,
                thread_id        TEXT UNIQUE NOT NULL,
                source_url       TEXT NOT NULL,
                source_name      TEXT NOT NULL,
                source_category  TEXT,
                document_title   TEXT,          -- title of the specific document
                publication_date TEXT,          -- date the source document was published/gazetted
                raw_content      TEXT,
                is_relevant      INTEGER DEFAULT 0,
                alert_draft      TEXT,          -- JSON blob
                approval_status  TEXT DEFAULT 'pending',
                reviewer_notes   TEXT,
                dispatched_email INTEGER DEFAULT 0,
                dispatch_error   TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                reviewed_at      TEXT,
                reviewed_by      TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id    TEXT,
                action      TEXT NOT NULL,
                actor       TEXT DEFAULT 'system',
                timestamp   TEXT NOT NULL,
                details     TEXT            -- JSON blob
            );

            CREATE TABLE IF NOT EXISTS settings (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_recipients (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT UNIQUE NOT NULL,
                label       TEXT,
                type        TEXT DEFAULT 'internal',  -- 'internal' | 'client'
                active      INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS source_tracking (
                source_url       TEXT PRIMARY KEY,   -- base URL of source from sources.json
                last_seen_date   TEXT DEFAULT '',    -- most recent pub_date alerted on for this source
                last_scraped_at  TEXT DEFAULT '',    -- ISO timestamp of last scrape run
                total_alerts_generated INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_alerts_status
                ON alerts(approval_status);
            CREATE INDEX IF NOT EXISTS idx_audit_alert
                ON audit_log(alert_id);
            CREATE INDEX IF NOT EXISTS idx_alerts_url
                ON alerts(source_url);
        """)

        # ── Schema migrations (safe to run on every startup) ──────────────────
        # ALTER TABLE ADD COLUMN silently fails if the column already exists,
        # so we catch the OperationalError and continue.
        _add_column_if_missing(conn, "alerts", "document_title", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "alerts", "publication_date", "TEXT DEFAULT ''")
        _add_column_if_missing(conn, "alerts", "geography_scope", "TEXT DEFAULT 'kenya'")
        _add_column_if_missing(conn, "alerts", "kenya_nexus", "TEXT DEFAULT ''")

        # Add indexes only after columns exist
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alerts_pubdate ON alerts(publication_date)"
            )
        except Exception:
            pass


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()
    except Exception:
        pass  # Column already exists — safe to ignore


# ── Alert CRUD ────────────────────────────────────────────────────────────────

def save_alert(alert_id: str, thread_id: str, source_url: str,
               source_name: str, source_category: str = "",
               document_title: str = "", publication_date: str = "") -> None:
    now = _now()
    with _lock, get_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO alerts
                (id, thread_id, source_url, source_name, source_category,
                 document_title, publication_date,
                 approval_status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (alert_id, thread_id, source_url, source_name, source_category,
              document_title, publication_date, now, now))


def check_duplicate(source_url: str,
                    document_title: str = "",
                    publication_date: str = "") -> tuple[bool, str]:
    """
    Return (is_duplicate, reason_string).

    Duplicate rules (in order of precedence):
      1. Same source_url already exists in the alerts table.
      2. Same document_title + publication_date already exists
         (catches the same document reported by multiple sources).
    """
    with get_connection() as conn:
        # Rule 1: URL match
        row = conn.execute(
            "SELECT id, created_at FROM alerts WHERE source_url = ? LIMIT 1",
            (source_url,),
        ).fetchone()
        if row:
            return True, f"Already processed on {row['created_at'][:10]} (same URL)."

        # Rule 2: title + date match (cross-source deduplication)
        if document_title and publication_date:
            row = conn.execute(
                """SELECT id, created_at FROM alerts
                   WHERE document_title = ? AND publication_date = ? LIMIT 1""",
                (document_title, publication_date),
            ).fetchone()
            if row:
                return True, (
                    f"Already processed on {row['created_at'][:10]} "
                    f"(same document title and publication date — different source)."
                )

    return False, ""


def update_alert(alert_id: str, **fields) -> None:
    """Update arbitrary columns on an alert row."""
    if not fields:
        return
    allowed = {
        "raw_content", "is_relevant", "alert_draft",
        "approval_status", "reviewer_notes", "dispatched_email",
        "dispatch_error", "reviewed_at", "reviewed_by",
        "document_title", "publication_date",
        "geography_scope", "kenya_nexus",
    }
    safe = {k: v for k, v in fields.items() if k in allowed}
    if not safe:
        return

    set_clause = ", ".join(f"{k} = ?" for k in safe)
    values = list(safe.values()) + [_now(), alert_id]

    # Serialise dict/list values to JSON
    params = []
    for v in safe.values():
        params.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    params += [_now(), alert_id]

    with _lock, get_connection() as conn:
        conn.execute(
            f"UPDATE alerts SET {set_clause}, updated_at = ? WHERE id = ?",
            params,
        )


def get_alert(alert_id: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM alerts WHERE id = ?", (alert_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_alert_by_thread(thread_id: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM alerts WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_alerts(status: Optional[str] = None,
                practice_area: Optional[str] = None,
                date_from: Optional[str] = None,
                date_to: Optional[str] = None,
                limit: int = 200) -> list[dict]:
    clauses, params = [], []
    if status:
        clauses.append("approval_status = ?")
        params.append(status)
    if date_from:
        clauses.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("created_at <= ?")
        params.append(date_to + "T23:59:59")
    if practice_area:
        # search inside the alert_draft JSON
        clauses.append("alert_draft LIKE ?")
        params.append(f"%{practice_area}%")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM alerts {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── Audit log ─────────────────────────────────────────────────────────────────

def log_action(alert_id: str, action: str, actor: str = "system",
               details: Optional[dict] = None) -> None:
    with _lock, get_connection() as conn:
        conn.execute("""
            INSERT INTO audit_log (alert_id, action, actor, timestamp, details)
            VALUES (?, ?, ?, ?, ?)
        """, (alert_id, action, actor, _now(),
              json.dumps(details) if details else None))


def list_audit_log(alert_id: Optional[str] = None,
                   limit: int = 500) -> list[dict]:
    if alert_id:
        query = ("SELECT * FROM audit_log WHERE alert_id = ? "
                 "ORDER BY timestamp DESC LIMIT ?")
        params = (alert_id, limit)
    else:
        query = "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?"
        params = (limit,)

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ── Settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _lock, get_connection() as conn:
        conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
        """, (key, value, _now()))


# ── Source tracking ───────────────────────────────────────────────────────────

def get_source_tracking(source_url: str) -> Optional[dict]:
    """Return the tracking record for a source, or None if never scraped."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM source_tracking WHERE source_url = ?", (source_url,)
        ).fetchone()
    return dict(row) if row else None


def update_source_tracking(
    source_url: str,
    last_seen_date: str = "",
    alerts_delta: int = 0,
) -> None:
    """
    Upsert a source tracking record.
    - last_seen_date: only updated if it is more recent than the stored value.
    - alerts_delta: added to total_alerts_generated.
    """
    now = _now()
    with _lock, get_connection() as conn:
        existing = conn.execute(
            "SELECT last_seen_date, total_alerts_generated FROM source_tracking WHERE source_url = ?",
            (source_url,),
        ).fetchone()

        if existing:
            current_last_seen = existing["last_seen_date"] or ""
            # Only advance last_seen_date — never go backwards
            new_last_seen = (
                last_seen_date
                if last_seen_date and last_seen_date > current_last_seen
                else current_last_seen
            )
            conn.execute(
                """UPDATE source_tracking
                   SET last_seen_date = ?, last_scraped_at = ?,
                       total_alerts_generated = total_alerts_generated + ?
                   WHERE source_url = ?""",
                (new_last_seen, now, alerts_delta, source_url),
            )
        else:
            conn.execute(
                """INSERT INTO source_tracking
                       (source_url, last_seen_date, last_scraped_at, total_alerts_generated)
                   VALUES (?, ?, ?, ?)""",
                (source_url, last_seen_date, now, alerts_delta),
            )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.utcnow().isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Deserialise JSON columns
    for col in ("alert_draft",):
        if d.get(col) and isinstance(d[col], str):
            try:
                d[col] = json.loads(d[col])
            except json.JSONDecodeError:
                pass
    return d
