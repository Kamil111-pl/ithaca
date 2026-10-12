"""SQLite access and versioned migrations.

A single connection guarded by a lock is plenty for a single-user, local
app and keeps transactions simple. Migrations are numbered SQL files in
`migrations/`, applied in order and tracked with PRAGMA user_version.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

_MIGRATIONS = Path(__file__).parent / "migrations"
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def now() -> float:
    return time.time()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (or reopen) the database and apply pending migrations."""
    global _conn
    config.ensure_dirs()
    target = path or config.DB_PATH
    conn = sqlite3.connect(str(target), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    _migrate(conn)
    with _lock:
        if _conn is not None and _conn is not conn:
            _conn.close()
        _conn = conn
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    files = sorted(p for p in _MIGRATIONS.glob("[0-9]*.sql") if not p.name.startswith("._"))
    for f in files:
        version = int(f.name.split("_", 1)[0])
        if version <= current:
            continue
        conn.execute("BEGIN")
        try:
            # executescript() would commit mid-migration, so run statements
            # one by one inside our own transaction.
            for stmt in _split_sql(f.read_text(encoding="utf-8")):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies intact."""
    statements, buf = [], []
    for line in script.splitlines():
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        buf.append(line)
        candidate = "\n".join(buf)
        if sqlite3.complete_statement(candidate):
            statements.append(candidate)
            buf = []
    if buf and "\n".join(buf).strip():
        statements.append("\n".join(buf))
    return statements


def conn() -> sqlite3.Connection:
    if _conn is None:
        return connect()
    return _conn


def query(sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
    with _lock:
        row = conn().execute(sql, params).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: tuple | dict = ()) -> int:
    with _lock:
        cur = conn().execute(sql, params)
        return cur.lastrowid if cur.lastrowid is not None else cur.rowcount


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)
