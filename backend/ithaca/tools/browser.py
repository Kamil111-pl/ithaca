"""Browser control: a real Chrome window Ithaca drives through Playwright.

Pages are presented to the model as a numbered list of interactive elements
plus readable text, so even text-only models can browse: open a URL, read,
click [n], type into [n], press keys, scroll, go back, switch tabs. Vision
models can also ask for a screenshot.

Safety:
- Uses its own profile (data/browser-profile), never your main Chrome profile.
  Logins you do there persist for Ithaca.
- Page content is untrusted and taints the run.
- Clicking and typing need one approval per run.
- Private/local addresses and non-web schemes are blocked (so pages can't
  reach Ithaca itself), and it refuses to type into password fields.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

from .. import config, settings, web
from ..extension_bridge import ExtensionError, bridge
from .registry import ToolContext, ToolResult, obj, tool

log = logging.getLogger("ithaca.browser")

SNAPSHOT_JS = r"""
() => {
  const sel = 'a[href],button,input,textarea,select,summary,[role=button],[role=link],[role=tab],[role=menuitem],' +
              '[role=checkbox],[role=radio],[role=switch],[role=textbox],[role=combobox],[role=option],[contenteditable=true]';
  document.querySelectorAll('[data-ithaca-ref]').forEach(e => e.removeAttribute('data-ithaca-ref'));
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < -200 || r.top > innerHeight * 2.5) continue;
    n += 1;
    el.setAttribute('data-ithaca-ref', String(n));
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'input' ? (el.type || 'text') : tag);
    let name = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') ||
                el.getAttribute('alt') || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    const ph = el.getAttribute('placeholder');
    let extra = '';
    if (ph) extra += ` placeholder="${ph.slice(0, 40)}"`;
    if (tag === 'a') { try { extra += ` → ${new URL(el.href).hostname}` } catch (e) {} }
    if (el.disabled) extra += ' (disabled)';
    out.push(`[${n}] ${role} "${name}"${extra}`);
    if (n >= 150) break;
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 6000);
  return {title: document.title, url: location.href, elements: out, text};
}
"""


class BrowserManager:
    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self.page = None
        self.lock = asyncio.Lock()

    async def ensure(self):
        if self._ctx is not None:
            try:
                if self.page is None or self.page.is_closed():
                    pages = self._ctx.pages
                    self.page = pages[-1] if pages else await self._ctx.new_page()
                return self.page
            except Exception:  # noqa: BLE001 - window closed by the user
                await self.close()
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        profile = config.DATA_DIR / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"headless": False, "viewport": None, "args": ["--no-first-run", "--no-default-browser-check"]}
        if shutil.which("google-chrome") or Path("/Applications/Google Chrome.app").exists():
            kwargs["channel"] = "chrome"
        self._ctx = await self._pw.chromium.launch_persistent_context(str(profile), **kwargs)
        self._ctx.on("close", lambda *_: setattr(self, "_ctx", None))
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        return self.page

    async def close(self) -> None:
        try:
            if self._ctx is not None:
                await self._ctx.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._ctx = self._pw = self.page = None

    async def snapshot(self, note: str = "") -> ToolResult:
        page = self.page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.4)
        snap = await page.evaluate(SNAPSHOT_JS)
        tabs = len(self._ctx.pages) if self._ctx else 1
        body = (
            f"{note}Page: {snap['title']}\nURL: {snap['url']}\nTabs open: {tabs}\n\n"
            f"Interactive elements (use the number with browser_click / browser_type):\n"
            + ("\n".join(snap["elements"]) or "(none visible)")
            + f"\n\nPage text:\n{snap['text']}"
        )
        return ToolResult(body, untrusted_source=snap["url"], data={"url": snap["url"], "title": snap["title"]})


manager = BrowserManager()


async def _safe_url(url: str) -> str:
    url = url.strip()
    if not urlparse(url).scheme:
        url = "https://" + url
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http(s) pages can be opened.")
    await web._assert_public(url)  # blocks localhost / private networks
    return url


NEEDS_EXTENSION = (
    "Reading or acting on pages in the user's Chrome needs the Ithaca Chrome extension, which isn't connected yet. "
    "Tell the user: in Ithaca open Settings → Browser and follow the 3 setup steps (load the extension, paste the "
    "pairing code). Do not open a different browser instead."
)


def _use_chrome() -> bool:
    """True → drive the user's own Chrome via the extension. False → Ithaca's
    separate browser (only when the user chose it). Never silently switches
    to a different browser: without the extension, actions raise instead."""
    if settings.get("browser.target") == "separate":
        return False
    if not bridge.connected:
        raise ExtensionError(NEEDS_EXTENSION)
    return True


async def _open_in_users_browser(url: str) -> ToolResult:
    """Without the extension: open a new tab in the user's running, signed-in
    browser via macOS automation (same app, same profile — no new process)."""
    import sys

    app = str(settings.get("browser.app") or "Google Chrome")
    if sys.platform != "darwin":
        return ToolResult(NEEDS_EXTENSION, is_error=True)
    script = """
on run argv
  set theURL to item 1 of argv
  tell application (item 2 of argv)
    activate
    if (count of windows) = 0 then make new window
    tell front window to make new tab with properties {URL:theURL}
  end tell
end run
"""
    proc = await asyncio.create_subprocess_exec("osascript", "-e", script, url, app,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        detail = err.decode().strip()
        if "-1743" in detail or "Not authorized" in detail:
            detail = ("macOS blocked it. Allow it in System Settings → Privacy & Security → Automation "
                      f"(enable {app} for the app running Ithaca, e.g. Terminal).")
        # Fall back to the default browser (still the user's own, signed-in browser).
        proc = await asyncio.create_subprocess_exec("open", url)
        await proc.wait()
        if proc.returncode:
            return ToolResult(f"Could not open the page: {detail}", is_error=True)
    return ToolResult(
        f"Opened {url} in a new tab of the user's {app} (their signed-in browser). "
        "The Ithaca extension isn't connected, so you can't read or click in that tab yet — if the task needs more "
        "than opening the page, tell the user to connect it in Settings → Browser.",
        data={"url": url, "browser": "users-browser"},
    )


SIGN_IN = ("accounts.google.com", "login.", "/login", "/signin", "/sign-in", "/sign_in", "auth.", "/auth/", "sso.")


def _redirect_note(requested: str, landed: str) -> str:
    """Opening a site that bounced to a sign-in page is the commonest silent failure."""
    want, got = urlparse(requested).hostname or "", urlparse(landed or "").hostname or ""
    if not got or got == want or got.endswith("." + want) or want.endswith("." + got):
        return ""
    if any(m in (landed or "").lower() for m in SIGN_IN):
        return (f"⚠ Opening {want} redirected to a sign-in page ({got}). The user isn't signed in there. Ask them to "
                "sign in in their browser (never type passwords yourself), then continue.\n")
    return f"Note: {want} redirected to {got}.\n"


def _format(snap: dict[str, Any], note: str = "") -> ToolResult:
    body = (
        f"{note or snap.get('note') or ''}Page: {snap.get('title', '')}\nURL: {snap.get('url', '')}\n"
        f"Tabs open: {snap.get('tabs', 1)}\n\nInteractive elements (use the number with browser_click / browser_type):\n"
        + ("\n".join(snap.get("elements") or []) or "(none visible)")
        + f"\n\nPage text:\n{snap.get('text', '')}"
    )
    # the extension checks its own actions; a typed text that didn't land is a failure, not a success
    failed = snap.get("verified") is False and "Typed" in (snap.get("note") or "")
    return ToolResult(body, untrusted_source=snap.get("url") or "browser", is_error=failed,
                      data={"url": snap.get("url"), "title": snap.get("title"), "browser": "chrome",
                            "verified": snap.get("verified")})


async def _chrome(method: str, params: dict[str, Any] | None = None) -> ToolResult:
    try:
        return _format(await bridge.call(method, params or {}))
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)


def _el(page, ref: Any):
    return page.locator(f'[data-ithaca-ref="{int(ref)}"]').first


# element roles that accept typed text, as the extension names them (inputs are named by their type)
TEXT_ROLES = re.compile(r'^\[\d+\] (textbox|searchbox|combobox|textarea|document|text|email|search|url|tel|number)\b', re.I)

_ACT = dict(effects={"browser_act"}, group="browser",
            keywords="browser chrome website click button link form login fill page interact navigate")


@tool("browser_open",
      "Open a web page in the browser — the user's own Chrome (new tab, their sign-ins) when the Ithaca extension "
      "is connected, otherwise Ithaca's separate browser window. Returns the page's interactive elements as numbered "
      "items plus its text. Use for anything interactive on the web (Google Docs: https://docs.new makes a new doc).",
      obj({"url": {"type": "string"}, "new_tab": {"type": "boolean"}}, ["url"]),
      effects={"network", "browser"}, group="browser", untrusted_output=True,
      keywords="browser chrome safari website open url go to visit navigate page web")
async def browser_open(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        url = await _safe_url(args["url"])
    except (ValueError, web.WebError) as exc:
        return ToolResult(str(exc), is_error=True)
    try:
        if _use_chrome():
            try:
                snap = await bridge.call("open", {"url": url, "new_tab": args.get("new_tab", True)})
            except ExtensionError as exc:
                return ToolResult(str(exc), is_error=True)
            return _format(snap, _redirect_note(url, snap.get("url", "")) + (snap.get("note") or ""))
    except ExtensionError:
        return await _open_in_users_browser(url)
    async with manager.lock:
        page = await manager.ensure()
        if args.get("new_tab"):
            page = manager.page = await manager._ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.bring_to_front()
        return await manager.snapshot()


@tool("browser_search", "Search the web in the browser window and show the results page.",
      obj({"query": {"type": "string"}}, ["query"]),
      effects={"network", "browser"}, group="browser", untrusted_output=True,
      keywords="browser search google results web")
async def browser_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    url = f"https://www.google.com/search?q={quote_plus(args['query'])}"
    try:
        if _use_chrome():
            return await _chrome("open", {"url": url, "new_tab": True})
    except ExtensionError:
        return await _open_in_users_browser(url)
    async with manager.lock:
        page = await manager.ensure()
        await page.goto(f"https://duckduckgo.com/?q={quote_plus(args['query'])}", wait_until="domcontentloaded")
        await page.bring_to_front()
        return await manager.snapshot()


@tool("browser_read", "Re-read the current browser page: interactive elements and text.",
      effects={"browser"}, group="browser", untrusted_output=True, keywords="browser read page current elements")
async def browser_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        if _use_chrome():
            return await _chrome("snapshot", {})
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)
    async with manager.lock:
        await manager.ensure()
        return await manager.snapshot()


@tool("browser_click", "Click element [ref] from the latest browser snapshot. Returns the updated page.",
      obj({"ref": {"type": "integer"}}, ["ref"]), untrusted_output=True, **_ACT)
async def browser_click(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        if _use_chrome():
            return await _chrome("click", {"ref": int(args["ref"])})
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)
    async with manager.lock:
        page = await manager.ensure()
        el = _el(page, args["ref"])
        if await el.count() == 0:
            return ToolResult(f"No element [{args['ref']}] on the page; call browser_read for fresh numbers.", is_error=True)
        pages_before = len(manager._ctx.pages)
        await el.scroll_into_view_if_needed(timeout=5000)
        await el.click(timeout=10000)
        await asyncio.sleep(0.8)
        if len(manager._ctx.pages) > pages_before:  # link opened a new tab
            manager.page = manager._ctx.pages[-1]
            await manager.page.bring_to_front()
        return await manager.snapshot(note=f"Clicked [{args['ref']}].\n")


@tool("browser_type",
      "Type text into input element [ref] (replaces its content). Set submit=true to press Enter afterwards.",
      obj({"ref": {"type": "integer"}, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["ref", "text"]),
      untrusted_output=True, **_ACT)
async def browser_type(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        if _use_chrome():
            return await _chrome("type", {"ref": int(args["ref"]), "text": args["text"], "submit": bool(args.get("submit"))})
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)
    async with manager.lock:
        page = await manager.ensure()
        el = _el(page, args["ref"])
        if await el.count() == 0:
            return ToolResult(f"No element [{args['ref']}]; call browser_read for fresh numbers.", is_error=True)
        if (await el.get_attribute("type") or "").lower() == "password":
            return ToolResult("Ithaca never types passwords. Ask the user to enter it in the browser window themselves.",
                              is_error=True)
        await el.scroll_into_view_if_needed(timeout=5000)
        try:
            await el.fill(args["text"], timeout=10000)
        except Exception:  # noqa: BLE001 - contenteditable or custom widgets
            await el.click(timeout=5000)
            await page.keyboard.type(args["text"], delay=8)
        if args.get("submit"):
            await el.press("Enter")
            await asyncio.sleep(1.0)
        return await manager.snapshot(note=f"Typed into [{args['ref']}]{' and pressed Enter' if args.get('submit') else ''}.\n")


@tool("browser_press", "Press a key in the browser, e.g. Enter, Escape, Tab, PageDown, ArrowDown, Meta+A.",
      obj({"key": {"type": "string"}}, ["key"]), untrusted_output=True, **_ACT)
async def browser_press(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        if _use_chrome():
            return await _chrome("press", {"key": args["key"]})
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)
    async with manager.lock:
        page = await manager.ensure()
        await page.keyboard.press(args["key"])
        await asyncio.sleep(0.6)
        return await manager.snapshot(note=f"Pressed {args['key']}.\n")


@tool("browser_scroll", "Scroll the browser page down or up to reveal more content.",
      obj({"direction": {"type": "string", "enum": ["down", "up"]}}, ["direction"]),
      effects={"browser"}, group="browser", untrusted_output=True, keywords="browser scroll more page down")
async def browser_scroll(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        if _use_chrome():
            return await _chrome("scroll", {"direction": args["direction"]})
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)
    async with manager.lock:
        page = await manager.ensure()
        await page.mouse.wheel(0, 700 if args["direction"] == "down" else -700)
        await asyncio.sleep(0.5)
        return await manager.snapshot(note=f"Scrolled {args['direction']}.\n")


@tool("browser_back", "Go back to the previous page in the browser.", effects={"browser"}, group="browser",
      untrusted_output=True, keywords="browser back previous page history")
async def browser_back(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        if _use_chrome():
            return await _chrome("back", {})
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)
    async with manager.lock:
        page = await manager.ensure()
        await page.go_back(wait_until="domcontentloaded")
        return await manager.snapshot(note="Went back.\n")


@tool("browser_tabs", "List open browser tabs, or switch to tab `index` (0-based).",
      obj({"index": {"type": "integer"}}), effects={"browser"}, group="browser", untrusted_output=True,
      keywords="browser tabs switch tab window")
async def browser_tabs(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        if _use_chrome():
            if args.get("index") is not None:
                return await _chrome("tabs", {"index": int(args["index"])})
            res = await bridge.call("tabs", {})
            return ToolResult("\n".join(res.get("list", [])), untrusted_source="browser tabs")
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)
    async with manager.lock:
        await manager.ensure()
        pages = manager._ctx.pages
        if args.get("index") is not None:
            i = int(args["index"])
            if not 0 <= i < len(pages):
                return ToolResult(f"There are {len(pages)} tabs.", is_error=True)
            manager.page = pages[i]
            await manager.page.bring_to_front()
            return await manager.snapshot(note=f"Switched to tab {i}.\n")
        lines = [f"{i}: {await p.title()} — {p.url}" for i, p in enumerate(pages)]
        return ToolResult("\n".join(lines), untrusted_source="browser tabs")


@tool("browser_screenshot", "Screenshot the browser page (for models that can see images).",
      effects={"browser"}, group="browser", untrusted_output=True, keywords="browser screenshot see look image")
async def browser_screenshot(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if not ctx.services.get("vision"):
        return ToolResult("This model can't see images; use browser_read instead.", is_error=True)
    try:
        if _use_chrome():
            res = await bridge.call("screenshot", {})
            return ToolResult(f"Screenshot of {res.get('url')}", images=[{"media_type": "image/jpeg", "data": res["image"]}],
                              untrusted_source=res.get("url") or "browser")
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)
    async with manager.lock:
        page = await manager.ensure()
        data = await page.screenshot(type="jpeg", quality=70)
        return ToolResult(f"Screenshot of {page.url}", images=[{"media_type": "image/jpeg", "data": base64.b64encode(data).decode()}],
                          untrusted_source=page.url)


@tool("browser_type_here",
      "Type text at the current cursor in the browser — e.g. into a Google Doc after clicking in the document body. "
      "Use \\n for new lines. Only works in the user's Chrome (extension).",
      obj({"text": {"type": "string"}}, ["text"]), untrusted_output=True, **_ACT)
async def browser_type_here(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        if not _use_chrome():
            return ToolResult("Typing at the cursor needs the Ithaca Chrome extension; use browser_type with an element number.",
                              is_error=True)
    except ExtensionError as exc:
        return ToolResult(str(exc), is_error=True)
    try:
        return _format(await bridge.call("type_here", {"text": args["text"]}))
    except ExtensionError as exc:
        if "keyboard focus" not in str(exc):
            return ToolResult(str(exc), is_error=True)
        # Recover once: if the page has exactly one text field, that's where the text was meant to go.
        try:
            snap = await bridge.call("snapshot", {})
        except ExtensionError:
            return ToolResult(str(exc), is_error=True)
        fields = [i for i, line in enumerate(snap.get("elements") or [], 1) if TEXT_ROLES.search(line) and "(password)" not in line]
        if len(fields) != 1:
            return ToolResult(f"{exc} Candidates: {', '.join(f'[{i}]' for i in fields) or 'none visible'}.", is_error=True)
        try:
            await bridge.call("click", {"ref": fields[0]})
            snap = await bridge.call("type_here", {"text": args["text"]})
        except ExtensionError as exc2:
            return ToolResult(str(exc2), is_error=True)
        return _format(snap, f"Nothing had focus, so I clicked [{fields[0]}] (the only text field) first. " + (snap.get("note") or ""))
