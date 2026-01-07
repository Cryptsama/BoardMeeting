# memory_vault.py — BM_SUPER_LOGGER_V2 (Monthly folders + Daily TXT + JSONL + SQLite FTS)
from __future__ import annotations

import json, sqlite3, hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Dict, List

BASE_DIR = Path(__file__).resolve().parent
VAULT_DIR = BASE_DIR / "chat_vault"
VAULT_DIR.mkdir(exist_ok=True)

DB_PATH = VAULT_DIR / "memory.sqlite"

# ---- time bucketing ----
def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")

def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")

def _bucket_day_starting_noon(dt: datetime) -> str:
    """
    Your rule: each daily TXT file is a "day" starting at 12:00 PM local time.
    - 12:00 PM -> 11:59 AM next day stays in same file.
    Implementation: anything before 12:00 is assigned to previous date.
    """
    if dt.hour < 12:
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y-%m-%d")

def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    return con

def init_db() -> None:
    con = _connect()
    try:
        con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            meta_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            chain_hash TEXT NOT NULL
        )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)")

        # Full-text search over content
        con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
            content, content='events', content_rowid='id'
        )
        """)
        con.execute("""
        CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
            INSERT INTO events_fts(rowid, content) VALUES (new.id, new.content);
        END;
        """)
        con.commit()
    finally:
        con.close()

def _get_last_chain_hash(con: sqlite3.Connection, session_id: str) -> str:
    row = con.execute(
        "SELECT chain_hash FROM events WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return row[0] if row else "GENESIS"

def _append_daily_txt(*, dt: datetime, session_id: str, role: str, source: str, content: str) -> None:
    month_dir = VAULT_DIR / _month_key(dt)
    month_dir.mkdir(exist_ok=True)

    day_key = _bucket_day_starting_noon(dt)
    txt_path = month_dir / f"chat-{day_key}.txt"

    safe_content = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    line = f"[{_iso(dt)}] [{session_id}] [{role}|{source}] {safe_content}\n"

    with txt_path.open("a", encoding="utf-8") as f:
        f.write(line)

def log_event(
    *,
    session_id: str,
    role: str,                 # "user" | "assistant" | "system" | "model:openai" etc.
    source: str,               # "ui" | "openai" | "gemini" | "claude" | "tool:web" etc.
    content: str,
    meta: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Writes:
      1) Daily TXT (month folder / chat-YYYY-MM-DD.txt)
      2) Monthly JSONL (append-only)
      3) SQLite (durable + searchable via FTS5)
    Includes per-session hash chain to detect tampering.
    """
    meta = meta or {}
    dt = datetime.now()
    ts = _iso(dt)

    # 1) Daily TXT (month folder)
    _append_daily_txt(dt=dt, session_id=session_id, role=role, source=source, content=content)

    # 2) Monthly JSONL (append-only)
    vault_path = VAULT_DIR / f"events-{_month_key(dt)}.jsonl"
    rec = {"ts": ts, "session_id": session_id, "role": role, "source": source, "content": content, "meta": meta}
    with vault_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 3) SQLite (durable + searchable)
    con = _connect()
    try:
        prev_hash = _get_last_chain_hash(con, session_id)
        content_hash = _sha256(content or "")
        chain_hash = _sha256(prev_hash + "|" + ts + "|" + role + "|" + source + "|" + content_hash)

        cur = con.execute(
            """
            INSERT INTO events (ts, session_id, role, source, content, meta_json, content_hash, prev_hash, chain_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, session_id, role, source, content or "", json.dumps(meta, ensure_ascii=False),
             content_hash, prev_hash, chain_hash),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()

def recall(*, query: str, limit: int = 12, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fast recall using SQLite FTS5.
    """
    con = _connect()
    try:
        if session_id:
            rows = con.execute(
                """
                SELECT e.id, e.ts, e.session_id, e.role, e.source, e.content, e.meta_json
                FROM events_fts f
                JOIN events e ON e.id = f.rowid
                WHERE events_fts MATCH ? AND e.session_id = ?
                ORDER BY e.ts DESC
                LIMIT ?
                """,
                (query, session_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT e.id, e.ts, e.session_id, e.role, e.source, e.content, e.meta_json
                FROM events_fts f
                JOIN events e ON e.id = f.rowid
                WHERE events_fts MATCH ?
                ORDER BY e.ts DESC
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r[0],
                "ts": r[1],
                "session_id": r[2],
                "role": r[3],
                "source": r[4],
                "content": r[5],
                "meta": json.loads(r[6]) if r[6] else {},
            })
        return out
    finally:
        con.close()
