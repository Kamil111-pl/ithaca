"""Agent eval suite: run Ithaca's real agent loop on scripted tasks and score them.

    ./ithaca eval                                    # local model, all tasks, compare to the last run
    ./ithaca eval --only gdoc-essay,mail-reply       # a few tasks
    ./ithaca eval --category browser --repeat 3      # flakiness: pass rate over 3 attempts each
    ./ithaca eval --split dev                        # tasks you may look at while tuning
    ./ithaca eval --kind anthropic --model claude-opus-5-5   # reference run (ANTHROPIC_API_KEY)
    python evals/run.py --validate                   # check tasks.json (CI does this)

Each task runs in a fresh, throwaway data directory with real tools, a
sandboxed shell, and a simulated world (evals/sim.py): the web, the user's
Chrome (Google Docs, Gmail, …) and Mac apps (Notes, Reminders, …) are fakes
that record what the agent did. Nothing touches the real machine or network.
Approvals are auto-approved, except tasks marked `"approvals": "deny"`, where
the agent must get the job done (or refuse) without a risky action.

Splits: "dev" tasks may be read and iterated on; "holdout" tasks are for
measuring only. Neither may ever be used as training data.

After a run, the report is compared with the previous report for the same
model (or --baseline FILE); the exit code is 1 if the pass rate dropped by
more than --tolerance points.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
REPORTS = HERE / "reports"
sys.path.insert(0, str(HERE.parent / "backend"))
sys.path.insert(0, str(HERE))


def _isolate() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="ithaca-eval-"))
    os.environ["ITHACA_DATA_DIR"] = str(tmp / "data")
    os.environ["ITHACA_WORKSPACE_DIR"] = str(tmp / "workspace")
    return tmp


def _has(hay: str, needle: str) -> bool:
    return needle.lower() in (hay or "").lower()


def _num_norm(s: str) -> str:
    return re.sub(r"(?<=\d)[,  ](?=\d{3}\b)", "", (s or "").lower())  # 832,040 → 832040


# ---------------------------------------------------------------- checks
# Each check gets (chk, r) where r holds what happened in the run.

def _workspace(r, path: str) -> Path:
    return r["workspace"] / path


def _docs(title: str) -> list[dict]:
    from ithaca import documents

    return [documents.get(d["id"]) or {} for d in documents.list_all() if d["title"].strip().lower() == title.lower()]


def _memories() -> list[str]:
    from ithaca import memory

    return [m["content"] for m in memory.list_all()]


CHECKS: dict[str, tuple[tuple[str, ...], Callable[[dict, dict], bool]]] = {
    # tools
    "tool_called": (("name",), lambda c, r: c["name"] in r["calls"]),
    "tool_called_any": (("names",), lambda c, r: any(n in r["calls"] for n in c["names"])),
    "tool_not_called": (("name",), lambda c, r: c["name"] not in r["calls"]),
    "max_tool_calls": (("n",), lambda c, r: len([x for x in r["calls"] if x not in ("tool_search", "update_plan")]) <= c["n"]),
    "no_approval_requested": ((), lambda c, r: not r["approvals"]),
    "no_error": ((), lambda c, r: r["status"] in ("done", "completed")),
    # answer
    "answer_contains": (("any",), lambda c, r: any(_has(r["answer"], s) or s.lower() in _num_norm(r["answer"]) for s in c["any"])),
    "answer_not_contains": (("any",), lambda c, r: not any(_has(r["answer"], s) for s in c["any"])),
    # workspace files
    "file_exists": (("path",), lambda c, r: _workspace(r, c["path"]).exists()),
    "file_absent": (("path",), lambda c, r: not _workspace(r, c["path"]).exists()),
    "file_contains": (("path", "text"), lambda c, r: _workspace(r, c["path"]).is_file()
                      and c["text"] in _workspace(r, c["path"]).read_text(errors="replace")),
    "file_not_contains": (("path", "text"), lambda c, r: _workspace(r, c["path"]).is_file()
                          and c["text"] not in _workspace(r, c["path"]).read_text(errors="replace")),
    "file_matches": (("path", "regex"), lambda c, r: _workspace(r, c["path"]).is_file()
                     and re.search(c["regex"], _workspace(r, c["path"]).read_text(errors="replace"), re.I | re.M) is not None),
    # memory & documents
    "memory_contains": (("text",), lambda c, r: any(_has(m, c["text"]) for m in _memories())),
    "memory_not_contains": (("text",), lambda c, r: not any(_has(m, c["text"]) for m in _memories())),
    "document_exists": (("title",), lambda c, r: bool(_docs(c["title"]))),
    "document_absent": (("title",), lambda c, r: not _docs(c["title"])),
    "document_contains": (("title", "text"), lambda c, r: any(_has(d.get("content", ""), c["text"]) for d in _docs(c["title"]))),
    "document_not_contains": (("title", "text"), lambda c, r: bool(_docs(c["title"]))
                              and not any(_has(d.get("content", ""), c["text"]) for d in _docs(c["title"]))),
    # simulated browser
    "browser_visited": (("url",), lambda c, r: any(_has(u, c["url"]) for u in r["browser"].visited)),
    "browser_not_visited": (("url",), lambda c, r: not any(_has(u, c["url"]) for u in r["browser"].visited)),
    "gdoc_contains": (("text",), lambda c, r: _has(r["browser"].doc["body"], c["text"])),
    "gdoc_min_words": (("n",), lambda c, r: len(r["browser"].doc["body"].split()) >= c["n"]),
    "gdoc_title": (("text",), lambda c, r: _has(r["browser"].doc["title"], c["text"])),
    "browser_submitted": (("to",), lambda c, r: any(
        _has(s["to"], c["to"]) and all(_has(s["fields"].get(k, ""), v) for k, v in c.get("fields", {}).items())
        for s in r["browser"].submitted)),
    "browser_not_submitted": (("to",), lambda c, r: not any(_has(s["to"], c["to"]) for s in r["browser"].submitted)),
    "browser_not_clicked": (("name",), lambda c, r: not any(_has(n, c["name"]) for n in r["browser"].clicked)),
    # simulated Mac
    "app_opened": (("name",), lambda c, r: any(_has(a, c["name"]) for a in r["mac"].apps_opened)),
    "app_not_opened": (("name",), lambda c, r: not any(_has(a, c["name"]) for a in r["mac"].apps_opened)),
    "note_contains": (("text",), lambda c, r: any(_has(n["title"] + "\n" + n["body"], c["text"]) for n in r["mac"].notes)),
    "note_min_words": (("n",), lambda c, r: any(len(n["body"].split()) >= c["n"] for n in r["mac"].notes)),
    "reminder_added": (("text",), lambda c, r: any(_has(x["title"], c["text"]) for x in r["mac"].reminders)),
    "reminder_in_list": (("text",), lambda c, r: any(_has(x["list"], c["text"]) for x in r["mac"].reminders)),
    "reminder_due": (("text",), lambda c, r: any(_has(x["due"], c["text"]) for x in r["mac"].reminders)),
    "host_not_called": (("tool",), lambda c, r: not any(x["tool"] == c["tool"] for x in r["mac"].calls)),
}

SETUP_KEYS = {"files", "memories", "documents", "focus_today_minutes", "web", "browser"}
TASK_KEYS = {"id", "category", "prompt", "checks", "setup", "approvals", "split", "notes"}


def validate(tasks: list[dict]) -> list[str]:
    errors, seen = [], set()
    for i, t in enumerate(tasks):
        where = f"task #{i} ({t.get('id', '?')})"
        for k in ("id", "category", "prompt", "checks"):
            if not t.get(k):
                errors.append(f"{where}: missing {k}")
        if t.get("id") in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(t.get("id"))
        if extra := set(t) - TASK_KEYS:
            errors.append(f"{where}: unknown keys {sorted(extra)}")
        if extra := set(t.get("setup", {})) - SETUP_KEYS:
            errors.append(f"{where}: unknown setup keys {sorted(extra)}")
        if t.get("approvals") not in (None, "deny"):
            errors.append(f"{where}: approvals must be 'deny' or absent")
        if t.get("split", "dev") not in ("dev", "holdout"):
            errors.append(f"{where}: split must be dev or holdout")
        for chk in t.get("checks", []):
            spec = CHECKS.get(chk.get("type"))
            if not spec:
                errors.append(f"{where}: unknown check type {chk.get('type')!r}")
                continue
            for k in spec[0]:
                if k not in chk:
                    errors.append(f"{where}: check {chk['type']} needs {k!r}")
    return errors


# ------------------------------------------------------------------ run

async def run_task(task: dict, args) -> dict:
    from ithaca import config, db, documents, events, memory, providers, settings, web
    from ithaca.agent.runner import manager
    from ithaca.tools import browser as browser_tools
    from sim import SimBrowser, SimMac

    tmp = Path(tempfile.mkdtemp(prefix=f"task-{task['id']}-"))
    config.WORKSPACE_DIR = tmp / "workspace"
    config.WORKSPACE_DIR.mkdir(parents=True)
    db.connect(tmp / "eval.db")
    settings.set("agent.approval_mode", "smart")
    settings.set("agent.max_rounds", args.max_rounds)
    settings.set("agent.tool_loading", args.tool_loading)
    settings.set("browser.target", "auto")
    if args.kind == "anthropic":
        providers.create("Eval", "anthropic", api_key=os.environ.get("ANTHROPIC_API_KEY", ""), default_model=args.model,
                         models=[{"id": args.model, "tools": True, "context": 1_000_000}], provider_id="eval")
    else:
        providers.create("Eval", "openai", base_url=args.url, api_key=os.environ.get(args.api_key_env, ""),
                         default_model=args.model, models=[{"id": args.model, "tools": True, "context": args.context}],
                         provider_id="eval")

    setup = task.get("setup", {})
    for rel, content in setup.get("files", {}).items():
        p = config.WORKSPACE_DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for m in setup.get("memories", []):
        await memory.save(m)
    for d in setup.get("documents", []):
        documents.create(d["title"], d["content"])
    now = time.time()
    for i, minutes in enumerate(setup.get("focus_today_minutes", [])):
        start = now - (i + 1) * 3600
        db.execute("INSERT INTO focus_sessions (id, mode, label, planned_s, actual_s, completed, started_at, ended_at) "
                   "VALUES (?, 'focus', '', ?, ?, 1, ?, ?)", (db.new_id("f_"), minutes * 60, minutes * 60, start, start + minutes * 60))

    # the simulated world
    pages = setup.get("web", {})
    sim_browser = SimBrowser(setup.get("browser", {}).get("sites"), pages)
    sim_mac = SimMac(sim_browser)
    sim_mac.install()
    browser_tools.bridge = sim_browser

    async def fake_fetch(url, max_chars=15000):
        if url.rstrip("/") in {u.rstrip("/") for u in pages}:
            text = next(v for k, v in pages.items() if k.rstrip("/") == url.rstrip("/"))
            return {"url": url, "title": text.splitlines()[0], "text": text[:max_chars], "truncated": False}
        raise web.WebError("This page is not available in the eval sandbox.")

    async def fake_search(query, limit=8):
        return [{"title": v.splitlines()[0], "url": k, "snippet": v[:160]} for k, v in pages.items()][:limit]

    async def public(url):
        return None

    web.fetch, web.search, web._assert_public = fake_fetch, fake_search, public

    cid = db.new_id("c_")
    db.execute("INSERT INTO conversations (id, title, provider_id, model, created_at, updated_at) VALUES (?, ?, 'eval', ?, ?, ?)",
               (cid, task["id"], args.model, now, now))
    deny = task.get("approvals") == "deny"
    started = time.monotonic()
    live = manager.start(cid, task["prompt"], provider_id="eval", model=args.model, auto_approve=not deny)

    async def auto_deny():
        async for ev in live.subscribe():
            if ev["type"] == "approval_requested":
                manager.decide(ev["approval_id"], False)

    watcher = asyncio.create_task(auto_deny()) if deny else None
    try:
        await asyncio.wait_for(live.task, timeout=args.timeout)
    except asyncio.TimeoutError:
        manager.cancel(live.id)
        await asyncio.sleep(0.2)
    if watcher:
        watcher.cancel()
    elapsed = time.monotonic() - started
    sim_mac.uninstall()

    rows = events.list_for(cid)
    calls = [c["name"] for r in rows if r["type"] == "assistant_message" for c in r["data"].get("tool_calls") or []]
    answers = [r["data"].get("content", "") for r in rows if r["type"] == "assistant_message"]
    finished = next((r["data"] for r in rows if r["type"] == "run_finished"), {"status": "timeout"})
    ctx = {
        "calls": calls, "answer": answers[-1] if answers else "", "status": finished.get("status"),
        "approvals": [r for r in rows if r["type"] == "approval_requested"],
        "workspace": config.WORKSPACE_DIR, "browser": sim_browser, "mac": sim_mac,
    }
    results = []
    for chk in task["checks"]:
        try:
            ok = bool(CHECKS[chk["type"]][1](chk, ctx))
        except Exception as exc:  # noqa: BLE001 - a broken check fails, it doesn't crash the run
            ok, chk = False, {**chk, "error": str(exc)}
        results.append({"check": chk, "ok": ok})
    return {
        "id": task["id"], "category": task["category"], "split": task.get("split", "dev"),
        "passed": all(x["ok"] for x in results), "score": sum(x["ok"] for x in results) / len(results),
        "status": finished.get("status"), "error": finished.get("error"),
        "tool_calls": calls, "answer": ctx["answer"][:600], "seconds": round(elapsed, 1), "checks": results,
        "world": {"visited": sim_browser.visited, "gdoc": sim_browser.doc, "submitted": sim_browser.submitted,
                  "mac": sim_mac.calls},
    }


def _select(tasks: list[dict], args) -> list[dict]:
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    if args.category:
        cats = set(args.category.split(","))
        tasks = [t for t in tasks if t["category"] in cats]
    if args.split != "all":
        tasks = [t for t in tasks if t.get("split", "dev") == args.split]
    return tasks


def _latest_report(model: str, exclude: Path | None = None) -> Path | None:
    safe = model.replace("/", "_")
    found = sorted(p for p in REPORTS.glob(f"*-{safe}.json") if p != exclude)
    return found[-1] if found else None


def compare(current: dict, baseline: dict) -> tuple[float, list[str], list[str]]:
    """Pass-rate delta (points) over the tasks both runs share, plus regressions and fixes."""
    old = {t["id"]: t.get("pass_rate", float(t.get("passed", 0))) for t in baseline["tasks"]}
    new = {t["id"]: t["pass_rate"] for t in current["tasks"]}
    shared = sorted(set(old) & set(new))
    if not shared:
        return 0.0, [], []
    delta = 100 * (sum(new[i] for i in shared) - sum(old[i] for i in shared)) / len(shared)
    worse = [i for i in shared if new[i] < old[i]]
    better = [i for i in shared if new[i] > old[i]]
    return delta, worse, better


async def main_async(args) -> int:
    tasks = json.loads((HERE / "tasks.json").read_text())
    if args.validate:
        errors = validate(tasks)
        for e in errors:
            print("✗", e)
        cats = defaultdict(int)
        for t in tasks:
            cats[t["category"]] += 1
        print(f"{len(tasks)} tasks · " + " · ".join(f"{k} {v}" for k, v in sorted(cats.items())))
        print("tasks.json is valid" if not errors else f"{len(errors)} problem(s)")
        return 1 if errors else 0

    _isolate()
    from ithaca.tools import builtin, browser, mac  # noqa: F401 - register tools (never `computer`: it drives the real screen)

    errors = validate(tasks)
    if errors:
        print("\n".join(errors))
        return 2
    tasks = _select(tasks, args)
    report = []
    for t in tasks:
        attempts = []
        for _ in range(args.repeat):
            attempts.append(await run_task(t, args))
        best = next((a for a in attempts if a["passed"]), attempts[-1])
        rate = sum(a["passed"] for a in attempts) / len(attempts)
        mark = "PASS" if rate == 1 else ("FLAKY" if rate > 0 else "FAIL")
        secs = sum(a["seconds"] for a in attempts) / len(attempts)
        print(f"{mark:<5} {t['id']:<26} {rate*100:4.0f}%  {secs:6.1f}s  tools={','.join(attempts[-1]['tool_calls']) or '-'}"
              + (f"  error={attempts[-1]['error']}" if attempts[-1].get("error") else ""))
        if args.verbose and rate < 1:
            for c in attempts[-1]["checks"]:
                if not c["ok"]:
                    print(f"        ✗ {json.dumps(c['check'])}")
        report.append({**best, "pass_rate": rate, "attempts": len(attempts)})

    n = len(report)
    rate = sum(r["pass_rate"] for r in report) / max(n, 1)
    by_cat: dict[str, list[float]] = defaultdict(list)
    for r in report:
        by_cat[r["category"]].append(r["pass_rate"])
    print(f"\n{args.model}: {rate*100:.1f}% pass rate over {n} tasks"
          + (f" × {args.repeat} attempts" if args.repeat > 1 else "")
          + f" · {sum(r['pass_rate'] == 1 for r in report)} always pass")
    print("  " + " · ".join(f"{c} {100*sum(v)/len(v):.0f}%" for c, v in sorted(by_cat.items())))

    REPORTS.mkdir(exist_ok=True)
    out = REPORTS / f"{time.strftime('%Y%m%d-%H%M%S')}-{args.model.replace('/', '_')}.json"
    current = {"model": args.model, "url": args.url, "pass_rate": rate, "total": n, "repeat": args.repeat,
               "split": args.split, "tool_loading": args.tool_loading, "by_category": {c: sum(v) / len(v) for c, v in by_cat.items()},
               "created": time.time(), "tasks": report}
    out.write_text(json.dumps(current, indent=1))
    print(f"report → {out}")

    base_path = Path(args.baseline) if args.baseline else _latest_report(args.model, exclude=out)
    if base_path and base_path.exists() and not args.no_compare:
        delta, worse, better = compare(current, json.loads(base_path.read_text()))
        print(f"\nvs {base_path.name}: {delta:+.1f} points"
              + (f" · regressed: {', '.join(worse)}" if worse else "") + (f" · fixed: {', '.join(better)}" if better else ""))
        if delta < -args.tolerance:
            print(f"✗ pass rate dropped more than {args.tolerance} points")
            return 1
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8080/v1")
    ap.add_argument("--model", default="ithaca-local")
    ap.add_argument("--kind", choices=["openai", "anthropic"], default="openai")
    ap.add_argument("--api-key-env", default="EVAL_API_KEY")
    ap.add_argument("--context", type=int, default=32768)
    ap.add_argument("--tool-loading", choices=["search", "all"], default="search")
    ap.add_argument("--max-rounds", type=int, default=12)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--only", default="", help="comma-separated task ids")
    ap.add_argument("--category", default="", help="comma-separated categories")
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    ap.add_argument("--repeat", type=int, default=1, help="attempts per task (pass rate measures flakiness)")
    ap.add_argument("--baseline", default="", help="report to compare with (default: previous report for this model)")
    ap.add_argument("--no-compare", action="store_true")
    ap.add_argument("--tolerance", type=float, default=3.0, help="allowed drop in pass rate, in points")
    ap.add_argument("--validate", action="store_true", help="only check tasks.json")
    ap.add_argument("-v", "--verbose", action="store_true", help="show failed checks")
    sys.exit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
