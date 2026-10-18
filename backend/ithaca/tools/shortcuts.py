"""Apple Shortcuts and app links: reach the user's apps through the ways macOS provides.

Shortcuts is Apple's automation layer; most apps add actions to it (App
Intents), and the user's own shortcuts chain them however they like. Ithaca
lists and runs them with the `shortcuts` command. App links (spotify:, music:,
slack:, …) open an app straight at the right place.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .registry import ToolContext, ToolResult, obj, tool

# app link schemes that only open an app; nothing that calls, texts or emails someone
APP_SCHEMES = {"spotify", "music", "itms", "itmss", "podcasts", "slack", "discord", "zoommtg", "msteams", "notion",
               "obsidian", "things", "shortcuts", "maps", "x-apple.systempreferences", "vscode", "figma", "linear",
               "raycast", "craftdocs", "bear", "drafts", "omnifocus", "todoist", "fantastical", "x-fantastical3",
               "calshow", "photos-redirect", "news", "books", "ibooks", "stocks", "weather", "notes", "x-apple-reminderkit"}


async def _run(*argv: str, timeout: int = 120, stdin: bytes | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE if stdin else None,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


if sys.platform == "darwin" and shutil.which("shortcuts"):

    @tool("shortcuts_list", "List the user's Apple Shortcuts (automations that can control many of their apps).",
          effects={"read"}, group="mac", keywords="shortcuts shortcut automation siri app intents list routine")
    async def shortcuts_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code, out = await _run("shortcuts", "list", timeout=30)
        if code:
            return ToolResult(f"Couldn't list Shortcuts: {out}", is_error=True)
        names = [n for n in out.splitlines() if n.strip()]
        if not names:
            return ToolResult("The user has no Shortcuts yet. They can make some in the Shortcuts app.")
        return ToolResult("The user's Shortcuts:\n" + "\n".join(f"- {n}" for n in names), data={"shortcuts": names})

    @tool("shortcuts_run",
          "Run one of the user's Apple Shortcuts by its exact name (see shortcuts_list), optionally with text input. "
          "Returns the shortcut's output.",
          obj({"name": {"type": "string"}, "input": {"type": "string", "description": "text passed to the shortcut"}},
              ["name"]),
          effects={"app_script"}, group="mac", keywords="shortcuts shortcut run automation routine siri app intents")
    async def shortcuts_run(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code, listing = await _run("shortcuts", "list", timeout=30)
        names = [n for n in listing.splitlines() if n.strip()]
        name = args["name"].strip()
        if name not in names:
            close = [n for n in names if name.lower() in n.lower() or n.lower() in name.lower()]
            return ToolResult(f"No shortcut named “{name}”." + (f" Did you mean: {', '.join(close)}?" if close else
                              " Use shortcuts_list to see their names."), is_error=True)
        with tempfile.TemporaryDirectory() as d:
            argv = ["shortcuts", "run", name, "--output-path", str(Path(d) / "out")]
            if args.get("input"):
                inp = Path(d) / "in.txt"
                inp.write_text(args["input"])
                argv += ["--input-path", str(inp)]
            code, out = await _run(*argv, timeout=300)
            result = Path(d) / "out"
            output = result.read_text(errors="replace")[:4000] if result.exists() else ""
        if code:
            return ToolResult(f"The shortcut “{name}” failed: {out or 'no details'}", is_error=True)
        return ToolResult(f"Ran “{name}”." + (f" Output:\n{output}" if output else ""))

    @tool("open_app_link",
          "Open an app link that takes an app straight to a screen, e.g. spotify:search:volen%20sentir, "
          "music://, slack://open, notion://, obsidian://open?vault=…, zoommtg://… (web pages: use browser_open).",
          obj({"url": {"type": "string"}}, ["url"]),
          effects={"host"}, group="mac", keywords="open app link deep link uri scheme spotify music slack notion zoom")
    async def open_app_link(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = args["url"].strip()
        scheme = urlparse(url).scheme.lower()
        if scheme in ("http", "https"):
            return ToolResult("That's a web address; use browser_open for web pages.", is_error=True)
        if scheme not in APP_SCHEMES:
            return ToolResult(f"Ithaca doesn't open “{scheme}:” links. Supported: {', '.join(sorted(APP_SCHEMES))}.",
                              is_error=True)
        code, out = await _run("open", url, timeout=20)
        if code:
            return ToolResult(f"macOS couldn't open that link: {out or 'is the app installed?'}", is_error=True)
        return ToolResult(f"Opened {url}.")
