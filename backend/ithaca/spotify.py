"""Spotify through its official Web API, connected to the user's own account.

The user creates a free app at developer.spotify.com (one minute), pastes its
Client ID into Ithaca's settings, and signs in on Spotify's own page. Ithaca
uses the Authorization Code flow with PKCE (no client secret), keeps the
tokens in the local database, and refreshes them as needed. It never sees the
user's Spotify password.

Search works on any account; controlling playback needs Spotify Premium.
`transport` lets tests and evals swap in a fake Spotify.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from . import config, db, settings

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing user-read-private"

transport: httpx.AsyncBaseTransport | None = None  # tests/evals: a fake Spotify
_pending: dict[str, tuple[str, float]] = {}       # state -> (code_verifier, expires_at)


class SpotifyError(RuntimeError):
    pass


def redirect_uri() -> str:
    # Spotify accepts loopback IPs (not "localhost") for local apps.
    return f"http://127.0.0.1:{config.PORT}/api/integrations/spotify/callback"


def _client_id() -> str:
    cid = (settings.get("spotify.client_id") or "").strip()
    if not cid:
        raise SpotifyError("Spotify isn't set up yet. In Ithaca, open Settings → Connected apps → Spotify and follow "
                           "the steps (create a free app at developer.spotify.com and paste its Client ID).")
    return cid


def _load() -> dict[str, Any]:
    row = db.query_one("SELECT data FROM integrations WHERE name = 'spotify'")
    return db.loads(row["data"], {}) if row else {}


def _save(data: dict[str, Any]) -> None:
    db.execute("INSERT INTO integrations (name, data, updated_at) VALUES ('spotify', ?, ?) "
               "ON CONFLICT(name) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
               (db.dumps(data), db.now()))


def _client(timeout: float = 20) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=transport)


# ---------------------------------------------------------------- sign-in

def start_auth() -> str:
    """The Spotify sign-in URL to open in the user's browser."""
    cid = _client_id()
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    now = time.time()
    for s, (_, exp) in list(_pending.items()):
        if exp < now:
            _pending.pop(s, None)
    _pending[state] = (verifier, now + 600)
    return AUTH_URL + "?" + urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": redirect_uri(), "scope": SCOPES,
        "state": state, "code_challenge_method": "S256", "code_challenge": challenge,
    })


async def complete_auth(state: str, code: str) -> bool:
    entry = _pending.pop(state, None)
    if not entry or entry[1] < time.time():
        return False
    async with _client() as c:
        r = await c.post(TOKEN_URL, data={"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": redirect_uri(), "client_id": _client_id(),
                                          "code_verifier": entry[0]})
    if r.status_code != 200:
        return False
    _store_tokens(r.json())
    try:
        me = await api("GET", "/me")
        data = _load()
        data["account"] = {"name": me.get("display_name") or me.get("id"), "product": me.get("product")}
        _save(data)
    except SpotifyError:
        pass
    return True


def _store_tokens(tok: dict[str, Any]) -> None:
    data = _load()
    data["access_token"] = tok["access_token"]
    if tok.get("refresh_token"):
        data["refresh_token"] = tok["refresh_token"]
    data["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    _save(data)


def status() -> dict[str, Any]:
    data = _load()
    return {"configured": bool((settings.get("spotify.client_id") or "").strip()),
            "connected": bool(data.get("refresh_token")), "account": data.get("account"),
            "redirect_uri": redirect_uri()}


def disconnect() -> None:
    db.execute("DELETE FROM integrations WHERE name = 'spotify'")


async def _access_token() -> str:
    data = _load()
    if not data.get("refresh_token"):
        raise SpotifyError("Spotify isn't connected. Ask the user to connect it in Settings → Connected apps → Spotify.")
    if data.get("expires_at", 0) > time.time():
        return data["access_token"]
    async with _client() as c:
        r = await c.post(TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                          "client_id": _client_id()})
    if r.status_code != 200:
        raise SpotifyError("The Spotify connection expired. Ask the user to reconnect it in Settings → Connected apps.")
    _store_tokens(r.json())
    return _load()["access_token"]


# -------------------------------------------------------------------- API

async def api(method: str, path: str, params: dict[str, Any] | None = None, json: Any = None) -> dict[str, Any]:
    token = await _access_token()
    async with _client() as c:
        r = await c.request(method, API + path, params=params, json=json, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:  # token revoked early: refresh once
            data = _load()
            data["expires_at"] = 0
            _save(data)
            token = await _access_token()
            r = await c.request(method, API + path, params=params, json=json, headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 204 or not r.content:
        return {}
    body = r.json() if "json" in r.headers.get("content-type", "") else {}
    if r.status_code >= 400:
        err = body.get("error") if isinstance(body, dict) else None
        reason = (err or {}).get("reason") if isinstance(err, dict) else None
        message = (err or {}).get("message") if isinstance(err, dict) else str(err or r.text)
        if reason == "PREMIUM_REQUIRED" or r.status_code == 403 and "premium" in str(message).lower():
            raise SpotifyError("Controlling playback needs Spotify Premium. Search still works; the user can press play "
                               "in Spotify themselves.")
        if reason == "NO_ACTIVE_DEVICE" or r.status_code == 404 and "device" in str(message).lower():
            raise SpotifyError("NO_ACTIVE_DEVICE")
        raise SpotifyError(f"Spotify said: {message} ({r.status_code})")
    return body


async def search(query: str, kinds: list[str], limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    body = await api("GET", "/search", {"q": query, "type": ",".join(kinds), "limit": max(1, min(limit, 10))})
    out: dict[str, list[dict[str, Any]]] = {}
    for kind in kinds:
        items = [i for i in ((body.get(kind + "s") or {}).get("items") or []) if i]
        out[kind] = [_summary(kind, i) for i in items]
    return out


def _summary(kind: str, i: dict[str, Any]) -> dict[str, Any]:
    s = {"kind": kind, "name": i.get("name", ""), "uri": i.get("uri", "")}
    if kind == "track":
        s["by"] = ", ".join(a.get("name", "") for a in i.get("artists") or [])
        s["album"] = (i.get("album") or {}).get("name", "")
    elif kind == "album":
        s["by"] = ", ".join(a.get("name", "") for a in i.get("artists") or [])
    elif kind == "playlist":
        s["by"] = (i.get("owner") or {}).get("display_name", "")
        s["tracks"] = (i.get("tracks") or {}).get("total")
    elif kind == "artist":
        s["genres"] = ", ".join((i.get("genres") or [])[:3])
    return s


async def ensure_device() -> str | None:
    """An active (or available) device id; opens the Spotify app on this Mac if there is none."""
    def pick(devices: list[dict[str, Any]]) -> str | None:
        active = [d for d in devices if d.get("is_active")]
        computer = [d for d in devices if d.get("type") == "Computer"]
        chosen = (active or computer or devices or [None])[0]
        return chosen.get("id") if chosen else None

    dev = pick((await api("GET", "/me/player/devices")).get("devices") or [])
    if dev:
        return dev
    if sys.platform == "darwin" and transport is None:
        subprocess.run(["open", "-a", "Spotify"], capture_output=True)
        for _ in range(15):
            await asyncio.sleep(1 if transport is None else 0)
            dev = pick((await api("GET", "/me/player/devices")).get("devices") or [])
            if dev:
                return dev
    return None


async def now_playing() -> dict[str, Any] | None:
    body = await api("GET", "/me/player")
    item = body.get("item") if body else None
    if not item:
        return None
    return {"name": item.get("name"), "by": ", ".join(a.get("name", "") for a in item.get("artists") or []),
            "uri": item.get("uri"), "is_playing": bool(body.get("is_playing")),
            "context": (body.get("context") or {}).get("uri"), "device": (body.get("device") or {}).get("name")}
