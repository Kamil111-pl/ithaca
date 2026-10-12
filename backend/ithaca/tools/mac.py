"""macOS app automation: open apps and drive scriptable ones (Notes, Reminders, TextEdit).

These tools act on the real Mac, outside the sandbox, so they carry the
`host` effect: allowed directly in a clean run, approval required once the run
has read untrusted content. `run_applescript` can do anything, so it always
asks. Text is passed to AppleScript as arguments, never spliced into source.

The first time Ithaca controls an app, macOS asks for permission (System
Settings → Privacy & Security → Automation).
"""

from __future__ import annotations

import asyncio
import difflib
import html
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from .. import config
from .registry import ToolContext, ToolResult, obj, tool

AUTOMATION_HINT = (
    "macOS blocked the automation. Allow it in System Settings → Privacy & Security → Automation "
    "(enable the app for the program running Ithaca, e.g. Terminal), then try again."
)


async def _osascript(script: str, *args: str, timeout: int = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "Timed out waiting for the app (a permission dialog may be open)."
    text = (out or b"").decode().strip() or (err or b"").decode().strip()
    if proc.returncode and ("-1743" in text or "Not authorized" in text):
        text = AUTOMATION_HINT
    return proc.returncode or 0, text


# Websites people call "apps". Asked to open one, Ithaca opens the site instead.
WEB_APPS = {
    "gmail": "https://mail.google.com", "google mail": "https://mail.google.com",
    "google docs": "https://docs.google.com", "docs": "https://docs.google.com",
    "google sheets": "https://sheets.google.com", "sheets": "https://sheets.google.com",
    "google slides": "https://slides.google.com", "google drive": "https://drive.google.com", "drive": "https://drive.google.com",
    "google calendar": "https://calendar.google.com", "youtube": "https://www.youtube.com",
    "google maps": "https://maps.google.com", "linkedin": "https://www.linkedin.com", "notion": "https://www.notion.so",
    "chatgpt": "https://chatgpt.com", "twitter": "https://x.com", "x": "https://x.com", "github": "https://github.com",
    "outlook": "https://outlook.live.com", "netflix": "https://www.netflix.com",
}
APP_ALIASES = {"chrome": "Google Chrome", "google chrome": "Google Chrome", "vscode": "Visual Studio Code",
               "vs code": "Visual Studio Code", "word": "Microsoft Word", "excel": "Microsoft Excel"}


def _app_name(name: str) -> str:
    return APP_ALIASES.get(name.strip().lower(), name.strip())


APP_DIRS = ("/Applications", "/System/Applications", "/System/Applications/Utilities", "/Applications/Utilities",
            str(Path.home() / "Applications"))


def installed_apps() -> list[str]:
    names = set()
    for d in APP_DIRS:
        try:
            names.update(p.stem for p in Path(d).glob("*.app"))
        except OSError:
            pass
    return sorted(names)


def suggest_apps(name: str, n: int = 3) -> list[str]:
    """Installed apps whose names are close to `name` ('teams' → 'Microsoft Teams')."""
    apps = installed_apps()
    low = name.strip().lower()
    contains = [a for a in apps if low and (low in a.lower() or a.lower() in low)]
    close = difflib.get_close_matches(name, apps, n=n, cutoff=0.5)
    return list(dict.fromkeys(contains + close))[:n]


async def app_running(app: str, wait_s: float = 4.0) -> bool:
    """Poll until `app` is running (open -a returns before the app is up)."""
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        code, out = await _osascript(f'return application "{app}" is running', timeout=5)
        if code == 0 and out.strip() == "true":
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.4)


async def _open_site(url: str, ctx: ToolContext, preferred_app: str | None = None) -> ToolResult:
    """Open a website in the user's own browser — through the Chrome extension
    when connected (so the page can be read and used), else as a new tab."""
    from . import browser  # noqa: F401 - make sure the browser tools are registered
    from .registry import registry

    if preferred_app in (None, "", "Google Chrome"):
        tool = registry.get("browser_open")
        return await tool.handler({"url": url, "new_tab": True}, ctx)
    proc = await asyncio.create_subprocess_exec("open", "-a", preferred_app, url,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        return ToolResult(f"Could not open it in {preferred_app}: {err.decode().strip()}", is_error=True)
    return ToolResult(f"Opened {url} in {preferred_app}.")


def markdown_to_notes_html(md: str) -> str:
    """Small Markdown → HTML converter for Apple Notes bodies."""

    def inline(t: str) -> str:
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", t)
        t = re.sub(r"`(.+?)`", r"<tt>\1</tt>", t)
        return t

    out: list[str] = []
    in_list: str | None = None
    for raw in md.splitlines():
        line = raw.rstrip()
        bullet = re.match(r"^\s*[-*•]\s+(.*)", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)", line)
        kind = "ul" if bullet else "ol" if numbered else None
        if in_list and kind != in_list:
            out.append(f"</{in_list}>")
            in_list = None
        if kind:
            if not in_list:
                out.append(f"<{kind}>")
                in_list = kind
            out.append(f"<li>{inline((bullet or numbered).group(1))}</li>")
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)", line)
        if heading:
            size = {1: "h1", 2: "h2", 3: "h3"}[len(heading.group(1))]
            out.append(f"<{size}>{inline(heading.group(2))}</{size}>")
        elif line.strip():
            out.append(f"<div>{inline(line)}</div>")
        else:
            out.append("<div><br></div>")
    if in_list:
        out.append(f"</{in_list}>")
    return "".join(out)


if sys.platform == "darwin" and shutil.which("osascript"):

    @tool(
        "open_app",
        "Open (launch or bring to front) an application on the user's Mac by name, e.g. 'Notes', 'Safari', "
        "'Calendar', 'Spotify'.",
        obj({"name": {"type": "string"}}, ["name"]),
        effects={"host"},
        group="mac",
        keywords="launch start application program computer laptop desktop",
    )
    async def open_app(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        site = WEB_APPS.get(args["name"].strip().lower())
        if site:
            return await _open_site(site, ctx)
        app = _app_name(args["name"])
        proc = await asyncio.create_subprocess_exec(
            "open", "-a", app, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode:
            hint = suggest_apps(args["name"])
            return ToolResult(
                f"Could not open “{args['name']}”: no app by that name is installed."
                + (f" Did you mean: {', '.join(hint)}? Call open_app again with the exact name." if hint else
                   " If it's a website, use browser_open with its URL instead."),
                is_error=True)
        # verify: `open` returns before the app is up, and can "succeed" without launching it
        if not await app_running(app):
            return ToolResult(f"Asked macOS to open {app}, but it isn't running yet. It may still be starting, or "
                              "it quit. Check before continuing (computer_screenshot), or try open_app once more.",
                              is_error=True)
        return ToolResult(f"Opened {app} (checked: it's running).")

    @tool(
        "open_url",
        "Open a web address in the user's browser (their Chrome by default; pass app='Safari' for Safari). "
        "To read or interact with the page afterwards, use the browser_* tools.",
        obj({"url": {"type": "string"}, "app": {"type": "string", "description": "Safari, Google Chrome, …"}}, ["url"]),
        effects={"host"},
        group="mac",
        keywords="safari chrome open website url link browser show",
    )
    async def open_url(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = args["url"].strip()
        if "://" not in url:
            url = "https://" + url
        if not url.startswith(("http://", "https://")):
            return ToolResult("Only web addresses can be opened.", is_error=True)
        # Default to the user's own browser (Chrome via the extension when connected).
        return await _open_site(url, ctx, _app_name(args["app"]) if args.get("app") else None)

    @tool(
        "notes_create",
        "Create a note in the Apple Notes app on the user's Mac and show it. Use for any request to write "
        "something (essay, list, plan, draft) in Notes. `body` is Markdown; write the full content.",
        obj({"title": {"type": "string"}, "body": {"type": "string"},
             "folder": {"type": "string", "description": "Notes folder, default 'Notes'"}}, ["title", "body"]),
        effects={"host"},
        group="mac",
        keywords="notes app apple note write essay save jot mac laptop",
    )
    async def notes_create(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = args["title"].strip() or "Untitled"
        body = f"<h1>{html.escape(title)}</h1>" + markdown_to_notes_html(args["body"])
        script = """
on run argv
  set noteTitle to item 1 of argv
  set noteBody to item 2 of argv
  set folderName to item 3 of argv
  tell application "Notes"
    activate
    set targetFolder to missing value
    repeat with f in folders
      if name of f is folderName then set targetFolder to f
    end repeat
    if targetFolder is missing value then set targetFolder to folder 1
    set newNote to make new note at targetFolder with properties {name:noteTitle, body:noteBody}
    show newNote
    return name of targetFolder
  end tell
end run
"""
        code, out = await _osascript(script, title, body, args.get("folder") or "Notes")
        if code:
            return ToolResult(f"Notes failed: {out}", is_error=True)
        return ToolResult(f"Created the note “{title}” in Notes (folder: {out}) and opened it.")

    @tool(
        "reminders_add",
        "Add a reminder to the Apple Reminders app, optionally with a due date/time (natural text like "
        "'2026-10-05 09:00').",
        obj({"title": {"type": "string"}, "due": {"type": "string"},
             "list": {"type": "string", "description": "Reminders list, default list if omitted"}}, ["title"]),
        effects={"host"},
        group="mac",
        keywords="reminder todo task remind me apple reminders",
    )
    async def reminders_add(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        script = """
on run argv
  set t to item 1 of argv
  set d to item 2 of argv
  set l to item 3 of argv
  tell application "Reminders"
    if l is "" then
      set targetList to default list
    else
      set targetList to list l
    end if
    set r to make new reminder at end of targetList with properties {name:t}
    if d is not "" then set due date of r to date d
    return name of targetList
  end tell
end run
"""
        code, out = await _osascript(script, args["title"], args.get("due") or "", args.get("list") or "")
        if code:
            return ToolResult(f"Reminders failed: {out}", is_error=True)
        return ToolResult(f"Added reminder “{args['title']}” to {out}.")

    @tool(
        "open_in_app",
        "Open a workspace file in a Mac app (e.g. open an essay.md you wrote in TextEdit, or a .html in Safari). "
        "Write the file first with write_file.",
        obj({"path": {"type": "string"}, "app": {"type": "string", "description": "e.g. TextEdit; default app if omitted"}},
            ["path"]),
        effects={"host"},
        group="mac",
        keywords="open file textedit pages preview show document app",
    )
    async def open_in_app(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        root = config.WORKSPACE_DIR.resolve()
        target = (root / args["path"]).resolve()
        if root not in target.parents or not target.exists():
            return ToolResult("That file isn't in the workspace.", is_error=True)
        cmd = ["open", "-a", args["app"], str(target)] if args.get("app") else ["open", str(target)]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode:
            return ToolResult(f"Could not open it: {err.decode().strip()}", is_error=True)
        return ToolResult(f"Opened {args['path']}" + (f" in {args['app']}." if args.get("app") else "."))

    @tool(
        "run_applescript",
        "Run an AppleScript to automate any scriptable Mac app (Mail, Calendar, Safari, Finder, Music…). "
        "Powerful: the user approves every script.",
        obj({"script": {"type": "string"}}, ["script"]),
        effects={"host", "host_script"},
        group="mac",
        keywords="applescript automate mac app control calendar mail safari finder music",
    )
    async def run_applescript(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code, out = await _osascript(args["script"], timeout=60)
        return ToolResult(out or "(done)", is_error=bool(code))
