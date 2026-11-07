"""SQLite state for the gateway.

``accounts.jsonl`` stays the source of truth for credentials (it is written by
the registration pipeline and must remain append-only).  Everything that
changes at runtime -- health, cooldowns, free-tier readings, gateway keys and
the request log -- lives here, so a crash or restart cannot corrupt the
credential file.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_state (
    email           TEXT PRIMARY KEY,
    enabled         INTEGER NOT NULL DEFAULT 1,
    free_pct        INTEGER,
    free_reset_at   TEXT,
    free_exhausted  INTEGER NOT NULL DEFAULT 0,
    cooldown_until  REAL NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_error_at   REAL,
    last_used_at    REAL,
    ok_count        INTEGER NOT NULL DEFAULT 0,
    err_count       INTEGER NOT NULL DEFAULT 0,
    checked_at      REAL
);

CREATE TABLE IF NOT EXISTS gateway_keys (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,
    preview      TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   REAL NOT NULL,
    last_used_at REAL,
    requests     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS request_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    key_name          TEXT,
    model             TEXT,
    account           TEXT,
    status            INTEGER,
    latency_ms        INTEGER,
    ttft_ms           INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    error             TEXT,
    stream            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log (ts DESC);
"""


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_preview(key: str) -> str:
    """``sk-th2api-…4f2a``: enough to recognise a key, useless if leaked."""
    return f"{key[:11]}…{key[-4:]}"


class Database:
    """Thin synchronous wrapper; calls are short and run on the event loop.

    A single connection guarded by a lock is plenty here: the workload is a
    handful of panel reads plus one write per proxied request.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("PRAGMA journal_mode=WAL;" + SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ---- account state -------------------------------------------------

    def state(self, email: str) -> dict[str, Any]:
        rows = self.query("SELECT * FROM account_state WHERE email = ?", (email,))
        if rows:
            return rows[0]
        self._execute("INSERT OR IGNORE INTO account_state (email) VALUES (?)", (email,))
        return self.query("SELECT * FROM account_state WHERE email = ?", (email,))[0]

    def all_states(self) -> dict[str, dict[str, Any]]:
        return {row["email"]: row for row in self.query("SELECT * FROM account_state")}

    def ensure_accounts(self, emails: list[str]) -> None:
        for email in emails:
            self._execute("INSERT OR IGNORE INTO account_state (email) VALUES (?)", (email,))

    def set_enabled(self, email: str, enabled: bool) -> None:
        self.state(email)
        self._execute(
            "UPDATE account_state SET enabled = ?, cooldown_until = 0 WHERE email = ?",
            (1 if enabled else 0, email),
        )

    def mark_used(self, email: str, ok: bool) -> None:
        now = time.time()
        column = "ok_count" if ok else "err_count"
        self.state(email)
        self._execute(
            f"UPDATE account_state SET last_used_at = ?, {column} = {column} + 1 WHERE email = ?",
            (now, email),
        )

    def mark_error(self, email: str, message: str, cooldown: float = 0.0) -> None:
        now = time.time()
        self.state(email)
        self._execute(
            """UPDATE account_state
               SET last_error = ?, last_error_at = ?, err_count = err_count + 1,
                   cooldown_until = MAX(cooldown_until, ?)
             WHERE email = ?""",
            (message, now, now + cooldown if cooldown else 0, email),
        )

    def clear_error(self, email: str) -> None:
        self._execute(
            "UPDATE account_state SET last_error = NULL, cooldown_until = 0 WHERE email = ?",
            (email,),
        )

    def set_free_tier(
        self,
        email: str,
        *,
        pct: int | None,
        reset_at: str | None,
        exhausted: bool | None,
    ) -> None:
        self.state(email)
        self._execute(
            """UPDATE account_state
               SET free_pct = ?, free_reset_at = ?, free_exhausted = ?, checked_at = ?
             WHERE email = ?""",
            (pct, reset_at, None if exhausted is None else int(exhausted), time.time(), email),
        )

    # ---- gateway keys --------------------------------------------------

    def create_gateway_key(self, name: str, prefix: str) -> dict[str, Any]:
        key = f"{prefix}-{secrets.token_urlsafe(24)}"
        key_id = secrets.token_hex(8)
        self._execute(
            """INSERT INTO gateway_keys (id, name, key_hash, preview, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (key_id, name, hash_key(key), key_preview(key), time.time()),
        )
        return {"id": key_id, "name": name, "key": key, "preview": key_preview(key)}

    def list_gateway_keys(self) -> list[dict[str, Any]]:
        return self.query(
            """SELECT id, name, preview, enabled, created_at, last_used_at, requests
               FROM gateway_keys ORDER BY created_at DESC"""
        )

    def resolve_gateway_key(self, raw: str) -> dict[str, Any] | None:
        rows = self.query(
            "SELECT * FROM gateway_keys WHERE key_hash = ? AND enabled = 1",
            (hash_key(raw),),
        )
        if not rows:
            return None
        key = rows[0]
        self._execute(
            "UPDATE gateway_keys SET last_used_at = ?, requests = requests + 1 WHERE id = ?",
            (time.time(), key["id"]),
        )
        return key

    def delete_gateway_key(self, key_id: str) -> None:
        self._execute("DELETE FROM gateway_keys WHERE id = ?", (key_id,))

    def count_gateway_keys(self) -> int:
        rows = self.query("SELECT COUNT(*) AS n FROM gateway_keys")
        return int(rows[0]["n"]) if rows else 0

    # ---- request log ---------------------------------------------------

    def log_request(self, **fields: Any) -> None:
        self._execute(
            """INSERT INTO request_log
                 (ts, key_name, model, account, status, latency_ms, ttft_ms,
                  prompt_tokens, completion_tokens, error, stream)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                time.time(),
                fields.get("key_name"),
                fields.get("model"),
                fields.get("account"),
                fields.get("status"),
                fields.get("latency_ms"),
                fields.get("ttft_ms"),
                fields.get("prompt_tokens"),
                fields.get("completion_tokens"),
                fields.get("error"),
                1 if fields.get("stream") else 0,
            ),
        )
        # The panel only ever reads a window; trimming keeps the file small on
        # a long-running gateway.
        self._execute(
            "DELETE FROM request_log WHERE id < (SELECT MAX(id) FROM request_log) - 20000"
        )

    def recent_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM request_log ORDER BY id DESC LIMIT ?", (limit,))

    def request_stats(self, since: float) -> dict[str, Any]:
        rows = self.query(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN status BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS ok,
                      AVG(latency_ms) AS avg_latency,
                      SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)) AS tokens
               FROM request_log WHERE ts >= ?""",
            (since,),
        )
        return rows[0] if rows else {}