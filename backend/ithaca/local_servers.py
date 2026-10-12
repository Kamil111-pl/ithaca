"""Restart the bundled local model servers when they crash.

mlx-lm/mlx-vlm servers stay broken after an out-of-memory error ("generation
thread died", then 503 on /health). Ithaca launched them with
model/ithaca-model.sh, so it can relaunch them the same way and retry.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from urllib.parse import urlparse

import httpx

from . import config

log = logging.getLogger("ithaca.local")
_locks: dict[int, asyncio.Lock] = {}

CRASH_MARKERS = ("generation thread died", "Insufficient Memory", "OutOfMemory", "503")


def is_crash(message: str) -> bool:
    return any(m in message for m in CRASH_MARKERS)


def port_of(base_url: str) -> int | None:
    p = urlparse(base_url)
    if p.hostname not in ("127.0.0.1", "localhost"):
        return None
    return p.port


async def _healthy(port: int) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            return (await c.get(f"http://127.0.0.1:{port}/health")).status_code == 200
    except httpx.HTTPError:
        return False


async def _pids_on(port: int) -> list[int]:
    proc = await asyncio.create_subprocess_exec("lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return [int(x) for x in out.decode().split() if x.strip().isdigit()]


async def restart(base_url: str, model: str) -> bool:
    """Kill whatever serves `base_url` and relaunch it. True when healthy again."""
    port = port_of(base_url)
    if port not in (8080, 8090):
        return False
    lock = _locks.setdefault(port, asyncio.Lock())
    async with lock:
        if await _healthy(port):
            return True  # someone else already restarted it
        for pid in await _pids_on(port):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        await asyncio.sleep(2)
        for pid in await _pids_on(port):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        script = str(config.ROOT / "model" / "ithaca-model.sh")
        args = [script, "vision"] if port == 8090 else [script, "serve", model]
        home = os.environ.get("ITHACA_HOME", os.path.expanduser("~/.ithaca"))
        log_path = os.path.join(home, "vision-server.log" if port == 8090 else "mlx-server.log")
        log.warning("Restarting local model server on :%s (%s)", port, model)
        with open(log_path, "ab") as logf:
            await asyncio.create_subprocess_exec(*args, stdout=logf, stderr=logf, start_new_session=True)
        for _ in range(90):
            await asyncio.sleep(1)
            if await _healthy(port):
                return True
        return False
