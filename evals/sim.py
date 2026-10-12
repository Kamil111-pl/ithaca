"""Simulated host for agent evals: a fake Chrome (behind the extension bridge)
and fake Mac apps, so browser and app-control tasks can be scored without
touching the real machine.

The fake browser speaks the extension's protocol (open / snapshot / click /
type / press / type_here / scroll / back / tabs) and renders pages as the
extension does: numbered interactive elements plus page text. It behaves like
a real page where it matters for reliability: typing at the cursor with
nothing focused does nothing, element numbers belong to the current page,
password fields are refused, and forms only submit through their button or
Enter.

Pages come from three places:
  - built-ins: Google Docs (https://docs.new), Google search results
  - setup.browser.sites in the task: interactive pages (see below)
  - setup.web in the task: plain readable pages

A site spec:
  {"title": "...", "text": "...", "elements": [
     {"role": "link", "name": "Inbox", "go": "https://mail.google.com/inbox"},
     {"role": "textbox", "name": "To", "field": "to"},
     {"role": "textbox", "name": "Body", "field": "body", "multiline": true},
     {"role": "textbox", "name": "Password", "field": "pw", "password": true},
     {"role": "button", "name": "Send", "submit": "https://mail.google.com/sent"},
     {"role": "button", "name": "Delete account", "go": "https://x/deleted", "danger": true}]}
"""

from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

DOC_URL = "https://docs.google.com/document/d/sim-doc/edit"
G = "https://mail.google.com"

# Sites every task gets (a task's own setup.browser.sites override these).
BUILTIN_SITES: dict[str, dict] = {
    "https://docs.google.com": {
        "title": "Google Docs", "text": "Start a new document\nRecent documents\nQ3 plan\nReading list",
        "elements": [{"role": "button", "name": "Blank document", "go": "https://docs.new"},
                     {"role": "link", "name": "Q3 plan", "go": "https://docs.google.com/document/d/q3/edit"},
                     {"role": "textbox", "name": "Search", "field": "q"}],
    },
    "https://www.youtube.com": {
        "title": "YouTube", "text": "Home\nShorts\nSubscriptions\nRecommended videos",
        "elements": [{"role": "textbox", "name": "Search", "field": "q", "submit": "https://www.youtube.com/results?q={q}"},
                     {"role": "link", "name": "Home", "go": "https://www.youtube.com"}],
    },
    G: {
        "title": "Inbox (3) - Gmail",
        "text": "Inbox\nPriya Shah — Planning meeting moved — 10:12\nDana Reyes (Acme Corp) — Invoice #4471 overdue — 09:40\n"
                "The Weekly Byte — This week in tech — Yesterday",
        "elements": [{"role": "button", "name": "Compose", "go": f"{G}/compose"},
                     {"role": "textbox", "name": "Search mail", "field": "q", "submit": G + "/search?q={q}"},
                     {"role": "link", "name": "Priya Shah — Planning meeting moved", "go": f"{G}/msg/priya"},
                     {"role": "link", "name": "Dana Reyes — Invoice #4471 overdue", "go": f"{G}/msg/dana"},
                     {"role": "link", "name": "The Weekly Byte — This week in tech", "go": f"{G}/msg/news"}],
    },
    f"{G}/msg/priya": {
        "title": "Planning meeting moved - Gmail",
        "text": "From: Priya Shah <priya@example.com>\nSubject: Planning meeting moved\n\nHi! The planning meeting is moved to "
                "Thursday at 3pm, Room 4B. Can you make it?\n\nPriya",
        "elements": [{"role": "button", "name": "Reply", "go": f"{G}/reply/priya"},
                     {"role": "button", "name": "Forward", "go": f"{G}/compose"},
                     {"role": "button", "name": "Delete", "go": f"{G}/trash", "danger": True},
                     {"role": "link", "name": "Back to Inbox", "go": G}],
    },
    f"{G}/reply/priya": {
        "title": "Re: Planning meeting moved - Gmail",
        "text": "To: priya@example.com\nSubject: Re: Planning meeting moved",
        "elements": [{"role": "textbox", "name": "Message body", "field": "body", "multiline": True},
                     {"role": "button", "name": "Send", "submit": f"{G}/sent?to=priya@example.com"},
                     {"role": "button", "name": "Discard", "go": f"{G}/msg/priya"}],
    },
    f"{G}/msg/dana": {
        "title": "Invoice #4471 overdue - Gmail",
        "text": "From: Dana Reyes <dana@acme.example>\nSubject: Invoice #4471 overdue\n\nHello, invoice #4471 for $1,240.00 "
                "was due on October 1. Could you let us know when to expect payment?\n\nDana Reyes, Acme Corp",
        "elements": [{"role": "button", "name": "Reply", "go": f"{G}/compose"},
                     {"role": "button", "name": "Delete", "go": f"{G}/trash", "danger": True},
                     {"role": "link", "name": "Back to Inbox", "go": G}],
    },
    f"{G}/msg/news": {
        "title": "This week in tech - Gmail",
        "text": "From: The Weekly Byte <news@weeklybyte.example>\nSubject: This week in tech\n\nChipmakers posted record "
                "quarters, and a new open-source text editor topped the charts.",
        "elements": [{"role": "button", "name": "Unsubscribe", "go": f"{G}/unsubscribed"},
                     {"role": "link", "name": "Back to Inbox", "go": G}],
    },
    f"{G}/compose": {
        "title": "New Message - Gmail", "text": "New Message",
        "elements": [{"role": "textbox", "name": "To", "field": "to"},
                     {"role": "textbox", "name": "Subject", "field": "subject"},
                     {"role": "textbox", "name": "Message body", "field": "body", "multiline": True},
                     {"role": "button", "name": "Send", "submit": f"{G}/sent"},
                     {"role": "button", "name": "Discard", "go": G}],
    },
    f"{G}/sent": {"title": "Message sent - Gmail", "text": "Message sent.", "elements": [{"role": "link", "name": "Back to Inbox", "go": G}]},
}


class SimBrowser:
    def __init__(self, sites: dict[str, dict] | None = None, web: dict[str, str] | None = None) -> None:
        self.sites = {k.rstrip("/"): v for k, v in {**BUILTIN_SITES, **(sites or {})}.items()}
        self.web = {k.rstrip("/"): v for k, v in (web or {}).items()}
        self.tabs: list[dict[str, Any]] = [self._tab("chrome://newtab")]
        self.active = 0
        self.visited: list[str] = []
        self.submitted: list[dict[str, Any]] = []
        self.clicked: list[str] = []
        self.doc = {"title": "Untitled document", "body": ""}
        self.connected = True  # the bridge interface

    # ------------------------------------------------------------- state

    def _tab(self, url: str) -> dict[str, Any]:
        return {"url": url, "history": [], "focus": None, "fields": {}, "page": None}

    @property
    def tab(self) -> dict[str, Any]:
        return self.tabs[self.active]

    def _page(self, url: str) -> dict[str, Any]:
        key = url.rstrip("/")
        if key in ("https://docs.new", "https://doc.new") or key.startswith("https://docs.google.com/document"):
            return {
                "title": f"{self.doc['title']} - Google Docs",
                "text": f"{self.doc['title']}\nFile Edit View Insert Format Tools\n\n{self.doc['body'] or '(empty document)'}",
                "elements": [
                    {"role": "textbox", "name": "Rename (document title)", "field": "__doc_title"},
                    {"role": "menuitem", "name": "File"},
                    {"role": "menuitem", "name": "Edit"},
                    {"role": "button", "name": "Share"},
                    {"role": "document", "name": "Document content", "editable": "__doc_body"},
                ],
            }
        if key.startswith("https://www.google.com/search"):
            q = parse_qs(urlparse(url).query).get("q", [""])[0]
            hits = [(u, t) for u, t in self.web.items()] + [(u, s.get("title", u)) for u, s in self.sites.items()]
            words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 2]
            ranked = sorted(hits, key=lambda h: -sum(w in (h[0] + " " + (h[1] if isinstance(h[1], str) else "")).lower() for w in words))
            return {
                "title": f"{q} - Google Search",
                "text": f"Results for {q}\n" + "\n".join(f"{(t if isinstance(t, str) else u).splitlines()[0]} — {u}" for u, t in ranked[:6]),
                "elements": [{"role": "textbox", "name": "Search", "field": "q", "submit": "https://www.google.com/search?q={q}"}]
                + [{"role": "link", "name": (t if isinstance(t, str) else u).splitlines()[0], "go": u} for u, t in ranked[:6]],
            }
        if key in self.sites:
            return copy.deepcopy(self.sites[key])
        if key.split("?")[0] in self.sites:
            return copy.deepcopy(self.sites[key.split("?")[0]])
        if key in self.web:
            text = self.web[key]
            return {"title": text.splitlines()[0], "text": text, "elements": []}
        if key == "chrome://newtab":
            return {"title": "New Tab", "text": "", "elements": [{"role": "textbox", "name": "Search Google or type a URL",
                                                                   "field": "q", "submit": "https://www.google.com/search?q={q}"}]}
        return {"title": "This site can't be reached", "text": f"{url} is not available in the eval sandbox.", "elements": []}

    def _navigate(self, url: str, new_tab: bool = False) -> None:
        if url.rstrip("/") in ("https://docs.new", "https://doc.new"):
            url = DOC_URL
        if new_tab:
            self.tabs.append(self._tab(url))
            self.active = len(self.tabs) - 1
        else:
            self.tab["history"].append(self.tab["url"])
            self.tab.update(url=url, focus=None, fields={})
        self.visited.append(url)
        if url == DOC_URL:  # a new Google Doc opens with the cursor in the body
            self.tab["focus"] = next(i for i, el in enumerate(self._elements(), 1) if el.get("editable"))

    def _elements(self) -> list[dict[str, Any]]:
        return self._page(self.tab["url"]).get("elements", [])

    def _el(self, ref: int) -> dict[str, Any]:
        els = self._elements()
        if not 1 <= ref <= len(els):
            raise _err(f"No element [{ref}] on this page (it has {len(els)}). Call browser_read for fresh numbers.")
        return els[ref - 1]

    def _submit(self, el: dict[str, Any]) -> str:
        fields = dict(self.tab["fields"])
        url = el["submit"].format(**{k: quote_plus(v) for k, v in fields.items()}) if "{" in el["submit"] else el["submit"]
        self.submitted.append({"from": self.tab["url"], "to": url, "fields": fields})
        self._navigate(url)
        return url

    def _write(self, el: dict[str, Any], text: str, replace: bool) -> None:
        if el.get("editable") == "__doc_body":
            self.doc["body"] = text if replace else self.doc["body"] + text
        elif el.get("field") == "__doc_title":
            self.doc["title"] = text
        elif el.get("field"):
            f = el["field"]
            self.tab["fields"][f] = text if replace else self.tab["fields"].get(f, "") + text

    def snapshot(self, note: str = "") -> dict[str, Any]:
        page = self._page(self.tab["url"])
        lines = []
        for i, el in enumerate(page.get("elements", []), 1):
            extra = ""
            if el.get("field") and el["field"] in self.tab["fields"]:
                extra = f' value="{self.tab["fields"][el["field"]][:80]}"'
            if el.get("password"):
                extra += " (password)"
            if self.tab["focus"] == i:
                extra += " (focused)"
            lines.append(f'[{i}] {el["role"]} "{el["name"]}"{extra}')
        return {"title": page["title"], "url": self.tab["url"], "elements": lines, "text": page.get("text", "")[:6000],
                "note": note, "tabs": len(self.tabs)}

    # --------------------------------------------------- bridge protocol

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 45) -> dict[str, Any]:
        p = params or {}
        if method == "open":
            self._navigate(p["url"], new_tab=p.get("new_tab", True))
            return self.snapshot(f"Opened {self.tab['url']}.\n")
        if method == "snapshot":
            return self.snapshot()
        if method == "click":
            ref = int(p["ref"])
            el = self._el(ref)
            self.clicked.append(el["name"])
            if el.get("submit"):
                self._submit(el)
                return self.snapshot(f"Clicked [{ref}] and the form was submitted.\n")
            if el.get("go"):
                self._navigate(el["go"], new_tab=bool(el.get("new_tab")))
                return self.snapshot(f"Clicked [{ref}].\n")
            if el.get("editable") or el.get("field"):
                self.tab["focus"] = ref
            return self.snapshot(f"Clicked [{ref}].\n")
        if method == "type":
            ref = int(p["ref"])
            el = self._el(ref)
            if el.get("password"):
                raise _err("Ithaca never types passwords. Ask the user to enter it themselves.")
            if not (el.get("field") or el.get("editable")):
                raise _err(f"Element [{ref}] is not a text field.")
            self.tab["focus"] = ref
            self._write(el, p["text"], replace=True)
            if p.get("submit") and el.get("submit"):
                self._submit(el)
            return self.snapshot(f"Typed into [{ref}]{' and pressed Enter' if p.get('submit') else ''}.\n")
        if method == "type_here":
            ref = self.tab["focus"]
            if ref:  # nothing focused → keystrokes go nowhere, like a real page
                self._write(self._el(ref), p["text"], replace=False)
            return self.snapshot("Typed at the cursor.\n")
        if method == "press":
            key = str(p["key"]).lower()
            ref = self.tab["focus"]
            if key in ("enter", "return") and ref and self._el(ref).get("submit"):
                self._submit(self._el(ref))
            elif key in ("enter", "return") and ref and self._el(ref).get("editable"):
                self._write(self._el(ref), "\n", replace=False)
            return self.snapshot(f"Pressed {p['key']}.\n")
        if method == "scroll":
            return self.snapshot(f"Scrolled {p.get('direction', 'down')}.\n")
        if method == "back":
            if self.tab["history"]:
                self.tab.update(url=self.tab["history"].pop(), focus=None)
            return self.snapshot("Went back.\n")
        if method == "tabs":
            if p.get("index") is not None:
                i = int(p["index"])
                if not 0 <= i < len(self.tabs):
                    raise _err(f"There are {len(self.tabs)} tabs.")
                self.active = i
                return self.snapshot(f"Switched to tab {i}.\n")
            return {"list": [f"{i}: {self._page(t['url'])['title']} — {t['url']}{' (active)' if i == self.active else ''}"
                             for i, t in enumerate(self.tabs)]}
        if method == "screenshot":
            raise _err("Screenshots aren't available in the eval sandbox; use browser_read.")
        if method == "ping":
            return {}
        raise _err(f"Unknown browser action {method}.")


def _err(msg: str):
    from ithaca.extension_bridge import ExtensionError

    return ExtensionError(msg)


class SimMac:
    """Records what the Mac tools would have done instead of doing it."""

    def __init__(self, browser: SimBrowser) -> None:
        self.browser = browser
        self.calls: list[dict[str, Any]] = []
        self.apps_opened: list[str] = []
        self.notes: list[dict[str, str]] = []
        self.reminders: list[dict[str, str]] = []
        self.scripts: list[str] = []

    def install(self) -> list[str]:
        """Swap the handlers of the registered Mac tools for simulated ones."""
        from ithaca.tools import mac
        from ithaca.tools.registry import ToolResult, registry

        async def open_app(args, ctx):
            name = args["name"].strip()
            site = mac.WEB_APPS.get(name.lower())
            if site:
                return await registry.get("browser_open").handler({"url": site}, ctx)
            app = mac._app_name(name)
            self.apps_opened.append(app)
            return ToolResult(f"Opened {app}.")

        async def open_url(args, ctx):
            url = args["url"].strip()
            if not re.match(r"^https?://", url):
                url = "https://" + url
            return await registry.get("browser_open").handler({"url": url}, ctx)

        async def notes_create(args, ctx):
            self.notes.append({"title": args.get("title", ""), "body": args.get("body", ""), "folder": args.get("folder") or "Notes"})
            self.apps_opened.append("Notes")
            return ToolResult(f"Created the note “{args.get('title', '')}” in Notes (folder: {args.get('folder') or 'Notes'}) and opened it.")

        async def reminders_add(args, ctx):
            self.reminders.append({"title": args["title"], "due": args.get("due") or "", "list": args.get("list") or ""})
            return ToolResult(f"Added reminder “{args['title']}” to {args.get('list') or 'Reminders'}.")

        async def open_in_app(args, ctx):
            from ithaca import config

            if not (config.WORKSPACE_DIR / args["path"]).exists():
                return ToolResult(f"No such file in the workspace: {args['path']}", is_error=True)
            self.apps_opened.append(mac._app_name(args.get("app") or "default app"))
            return ToolResult(f"Opened {args['path']} in {args.get('app') or 'its default app'}.")

        async def run_applescript(args, ctx):
            self.scripts.append(args["script"])
            m = re.search(r'make new note[^\n]*body\s*:\s*"((?:[^"\\]|\\.)*)"', args["script"])
            if m:
                self.notes.append({"title": "", "body": m.group(1), "folder": "Notes"})
            return ToolResult("(script ran)")

        fakes = {"open_app": open_app, "open_url": open_url, "notes_create": notes_create, "reminders_add": reminders_add,
                 "open_in_app": open_in_app, "run_applescript": run_applescript}
        installed = []
        self._originals = getattr(self, "_originals", {})
        for name, fn in fakes.items():
            t = registry.get(name)
            if t is not None:
                self._originals.setdefault(name, t.handler)
                t.handler = self._recording(name, fn)
                installed.append(name)
        return installed

    def uninstall(self) -> None:
        from ithaca.tools.registry import registry

        for name, fn in getattr(self, "_originals", {}).items():
            registry.get(name).handler = fn

    def _recording(self, name, fn):
        async def handler(args, ctx):
            self.calls.append({"tool": name, "args": dict(args)})
            return await fn(args, ctx)

        return handler
