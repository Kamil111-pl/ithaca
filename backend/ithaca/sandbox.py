"""Confined command execution for the agent's shell tool.

Backends, picked by the `sandbox.mode` setting ("auto" tries them in order):

- seatbelt: macOS `sandbox-exec`. Writes only inside the workspace (plus the
  system temp area), no reads of $HOME or /Volumes except the workspace, and
  no network unless `sandbox.allow_network` is on.
- docker: `docker run --network none` with only the workspace mounted.
- off: runs on the host. The security layer then requires approval for every
  command (see security.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config, settings

log = logging.getLogger("ithaca.sandbox")
MAX_OUTPUT = 30_000


@dataclass
class ExecResult:
    exit_code: int
    output: str
    backend: str
    timed_out: bool = False


_seatbelt_checked: bool | None = None


def _seatbelt_available() -> bool:
    """sandbox-exec exists *and* still enforces a profile (it is deprecated, so a
    future macOS could keep the binary but stop enforcing). Checked once: a
    write outside an allowed folder must be refused."""
    global _seatbelt_checked
    if _seatbelt_checked is not None:
        return _seatbelt_checked
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        _seatbelt_checked = False
        return False
    import subprocess

    with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as forbidden:
        target = Path(forbidden) / "canary"
        profile = f'(version 1)(allow default)(deny file-write*)(allow file-write* (subpath {_sb_quote(os.path.realpath(allowed))}))'
        try:
            subprocess.run(["sandbox-exec", "-p", profile, "/bin/sh", "-c", f"echo x > {target}"],
                           capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            _seatbelt_checked = False
            return False
        _seatbelt_checked = not target.exists()
    if not _seatbelt_checked:
        log.warning("sandbox-exec ran but did not enforce its profile; treating Seatbelt as unavailable")
    return _seatbelt_checked


async def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", "--format", "{{.ServerVersion}}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(proc.wait(), timeout=5) == 0
    except (OSError, asyncio.TimeoutError):
        return False


async def resolve_backend() -> str:
    mode = settings.get("sandbox.mode")
    if mode == "seatbelt":
        return "seatbelt" if _seatbelt_available() else "unavailable"
    if mode == "docker":
        return "docker" if await _docker_available() else "unavailable"
    if mode == "off":
        return "off"
    if _seatbelt_available():
        return "seatbelt"
    if await _docker_available():
        return "docker"
    return "off"


def _sb_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(workspace: Path, allow_network: bool) -> str:
    ws_real = os.path.realpath(workspace)
    home = os.path.realpath(Path.home())
    ws_paths = {str(workspace), ws_real}
    allow_ws = " ".join(f"(subpath {_sb_quote(p)})" for p in sorted(ws_paths))
    # Writes go only to the workspace (TMPDIR points inside it). The one
    # exception is the xcrun cache that /usr/bin developer-tool shims (python3,
    # git, clang) keep in the per-user temp dir. /tmp is NOT writable, so a
    # symlink into it can't be used to write outside the project.
    user_tmp = os.path.realpath(tempfile.gettempdir())
    # …and the files `mktemp` makes there by default (it ignores TMPDIR on macOS).
    xcrun_cache = "^" + re.escape(user_tmp.rstrip("/")) + "/(xcrun_db|tmp\\.[A-Za-z0-9]+(/|$))"
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(allow file-write* {allow_ws} (regex {_sb_quote(xcrun_cache)})"
        " (literal \"/dev/null\") (literal \"/dev/zero\") (literal \"/dev/tty\") (subpath \"/dev/fd\")"
        " (literal \"/dev/dtracehelper\"))",
        f"(deny file-read* (subpath {_sb_quote(home)}) (subpath \"/Volumes\"))",
        f"(allow file-read* {allow_ws})",
        # no driving other apps (Finder, System Events…) from inside the sandbox
        "(deny appleevent-send)",
    ]
    if not allow_network:
        lines += ["(deny network*)", "(allow network* (remote unix-socket))"]
    return "\n".join(lines) + "\n"


async def run(command: str, *, timeout: int | None = None, network: bool | None = None) -> ExecResult:
    """Run a shell command with the workspace as cwd."""
    config.ensure_dirs()
    backend = await resolve_backend()
    if backend == "unavailable":
        return ExecResult(126, "The configured sandbox backend is not available on this machine.", backend)
    timeout = int(timeout or settings.get("sandbox.timeout_s") or 60)
    allow_network = bool(settings.get("sandbox.allow_network")) if network is None else network
    ws = config.WORKSPACE_DIR
    tmp = ws / ".tmp"
    tmp.mkdir(exist_ok=True)
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(ws),
        "TMPDIR": str(tmp),
        "LANG": "en_US.UTF-8",
        "TERM": "dumb",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    profile_file = None
    if backend == "seatbelt":
        profile_file = tempfile.NamedTemporaryFile("w", suffix=".sb", delete=False)
        profile_file.write(seatbelt_profile(ws, allow_network))
        profile_file.close()
        argv = ["sandbox-exec", "-f", profile_file.name, "/bin/bash", "--noprofile", "--norc", "-c", command]
    elif backend == "docker":
        argv = [
            "docker", "run", "--rm", "-i",
            "--network", "bridge" if allow_network else "none",
            "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
            "-v", f"{ws}:/workspace", "-w", "/workspace",
            "-e", "HOME=/workspace",
            str(settings.get("sandbox.docker_image")),
            "sh", "-c", command,
        ]
        env = {**os.environ}
    else:
        argv = ["/bin/bash", "--noprofile", "--norc", "-c", command]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(ws), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            _kill_group(proc)
            await proc.wait()
            out = b""
            timed_out = True
        text = out.decode("utf-8", "replace")
        if len(text) > MAX_OUTPUT:
            text = text[: MAX_OUTPUT // 2] + f"\n… [{len(text) - MAX_OUTPUT} chars truncated] …\n" + text[-MAX_OUTPUT // 2:]
        if timed_out:
            text += f"\n[timed out after {timeout}s]"
        return ExecResult(proc.returncode if proc.returncode is not None else 124, text, backend, timed_out)
    finally:
        if profile_file is not None:
            try:
                os.unlink(profile_file.name)
            except OSError:
                pass


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    import signal

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def status() -> dict:
    backend = await resolve_backend()
    return {
        "mode": settings.get("sandbox.mode"),
        "backend": backend,
        "seatbelt_available": _seatbelt_available(),
        "docker_available": await _docker_available(),
        "network": bool(settings.get("sandbox.allow_network")),
        "workspace": str(config.WORKSPACE_DIR),
    }
