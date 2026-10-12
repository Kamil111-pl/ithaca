"""Single-user password auth with scrypt hashes and opaque session tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from . import config, db

COOKIE = "ithaca_session"


def _hash(password: str, salt: bytes) -> str:
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def is_configured() -> bool:
    return db.query_one("SELECT 1 AS x FROM settings WHERE key = 'auth.password'") is not None


def set_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Use at least 8 characters.")
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('auth.password', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (db.dumps(_hash(password, secrets.token_bytes(16))),),
    )
    db.execute("DELETE FROM auth_sessions")


def check_password(password: str) -> bool:
    row = db.query_one("SELECT value FROM settings WHERE key = 'auth.password'")
    if not row:
        return False
    _, salt_hex, _ = db.loads(row["value"]).split("$")
    expected = db.loads(row["value"])
    return hmac.compare_digest(_hash(password, bytes.fromhex(salt_hex)), expected)


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    t = db.now()
    db.execute("INSERT INTO auth_sessions (token, created_at, expires_at) VALUES (?, ?, ?)",
               (token, t, t + config.SESSION_TTL_SECONDS))
    db.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (t,))
    return token


def valid(token: str | None) -> bool:
    if not token:
        return False
    row = db.query_one("SELECT expires_at FROM auth_sessions WHERE token = ?", (token,))
    return bool(row and row["expires_at"] > db.now())


def end_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
