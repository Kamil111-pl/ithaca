"""Bridge to the Ithaca Chrome extension (extension/): drive the user's own browser.

The extension opens a WebSocket to /api/browser-ext/ws with a pairing token
from data/extension.token. Ithaca sends {"type": "call", "id", "method",
"params"} and the extension replies {"type": "result", "id", "ok", ...}.

Robustness:
- Each pending call is bound to the socket it was sent on; if that socket
  closes or is replaced, the call fails at once instead of timing out.
- Read-only calls are retried once after a reconnect.
- An extension older than the copy on disk is told to reload itself.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import secrets
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from . import config

log = logging.getLogger("ithaca.extension")

READ_ONLY = {"snapshot", "screenshot", "ping"}
RECONNECT_GRACE_S = 6


class ExtensionError(RuntimeError):
    pass


class _Dropped(ExtensionError):
    """The socket carrying a call went away before it answered."""


def pairing_token() -> str:
    p = config.DATA_DIR / "extension.token"
    if p.exists():
        return p.read_text().strip()
    config.ensure_dirs()
    tok = secrets.token_urlsafe(24)
    p.write_text(tok)
    os.chmod(p, 0o600)
    return tok


def bundled_version() -> str | None:
    try:
        return json.loads((config.ROOT / "extension" / "manifest.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return None


def _older(a: str | None, b: str | None) -> bool:
    try:
        return tuple(int(x) for x in (a or "").split(".")) < tuple(int(x) for x in (b or "").split("."))
    except ValueError:
        return False


class ExtensionBridge:
    def __init__(self) -> None:
        self.ws: WebSocket | None = None
        self.version: str | None = None
        self.pending: dict[int, tuple[asyncio.Future, WebSocket]] = {}
        self._ids = itertools.count(1)
        self.lock = asyncio.Lock()
        self.connected_event = asyncio.Event()
        self.reconnects = 0

    @property
    def connected(self) -> bool:
        return self.ws is not None

    def _fail_pending(self, websocket: WebSocket, reason: str) -> None:
        for cid, (fut, sock) in list(self.pending.items()):
            if sock is websocket:
                self.pending.pop(cid, None)
                if not fut.done():
                    fut.set_exception(_Dropped(reason))

    async def serve(self, websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin", "")
        token = websocket.query_params.get("token", "")
        # Only the extension (chrome-extension:// origin) with the right token may connect.
        if not origin.startswith("chrome-extension://") or not secrets.compare_digest(token, pairing_token()):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        old = self.ws
        self.ws = websocket
        self.connected_event.set()
        if old is not None:
            self.reconnects += 1
            self._fail_pending(old, "The Chrome extension reconnected during the action.")
            try:
                await old.close(code=4000)
            except Exception:  # noqa: BLE001
                pass
        log.info("Chrome extension connected")
        try:
            while True:
                msg = json.loads(await websocket.receive_text())
                kind = msg.get("type")
                if kind == "hello":
                    self.version = msg.get("version")
                    if _older(self.version, bundled_version()):
                        log.info("Extension %s is older than %s on disk; asking it to reload", self.version, bundled_version())
                        await websocket.send_text(json.dumps({"type": "reload"}))
                elif kind == "ping":
                    await websocket.send_text('{"type":"pong"}')
                elif kind == "result":
                    entry = self.pending.pop(int(msg.get("id", 0)), None)
                    if entry and not entry[0].done():
                        entry[0].set_result(msg)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            self._fail_pending(websocket, "The Chrome extension disconnected during the action.")
            if self.ws is websocket:
                self.ws = None
                self.connected_event.clear()
                log.info("Chrome extension disconnected")

    async def _wait_connected(self, timeout: float) -> bool:
        if self.ws is not None:
            return True
        try:
            await asyncio.wait_for(self.connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self.ws is not None

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 45) -> dict[str, Any]:
        params = params or {}
        attempts = 2 if method in READ_ONLY or (method == "tabs" and params.get("index") is None) else 1
        last: Exception | None = None
        for _ in range(attempts):
            if not await self._wait_connected(RECONNECT_GRACE_S if last else 0):
                break
            try:
                return await self._call_once(method, params, timeout)
            except _Dropped as exc:
                last = exc
        if last is not None:
            raise ExtensionError(f"{last} Please try again.")
        raise ExtensionError("Your Chrome isn't connected. Install the Ithaca extension and pair it in Settings → Browser.")

    async def _call_once(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        async with self.lock:  # one browser action at a time
            ws = self.ws
            if ws is None:
                raise _Dropped("The Chrome extension disconnected.")
            cid = next(self._ids)
            fut = asyncio.get_running_loop().create_future()
            self.pending[cid] = (fut, ws)
            try:
                await ws.send_text(json.dumps({"type": "call", "id": cid, "method": method, "params": params}))
                reply = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise ExtensionError(f"Chrome didn't answer within {int(timeout)}s (the tab may be busy or closed).") from exc
            except (RuntimeError, WebSocketDisconnect) as exc:
                raise _Dropped("The Chrome extension disconnected.") from exc
            finally:
                self.pending.pop(cid, None)
        if not reply.get("ok"):
            raise ExtensionError(reply.get("error") or "The browser action failed.")
        return reply.get("result") or {}


bridge = ExtensionBridge()
