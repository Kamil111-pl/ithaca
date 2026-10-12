"""Deep research: plan queries → search → read sources → synthesize a cited report.

The pipeline is ordinary code; the model is only asked for well-scoped
sub-tasks (propose queries, write the report), which keeps it reliable even
on small local models.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterator

from . import documents, web
from .security import UNTRUSTED_POLICY, wrap_untrusted

DEPTHS = {
    "quick": {"queries": 2, "sources": 4, "chars": 4000},
    "standard": {"queries": 4, "sources": 8, "chars": 6000},
    "deep": {"queries": 6, "sources": 14, "chars": 7000},
}


def _parse_json_list(text: str) -> list[str]:
    match = re.search(r"\[[\s\S]*?\]", text)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return [str(i).strip() for i in items if str(i).strip()]


async def run(client, model: str, question: str, depth: str = "standard") -> AsyncIterator[dict[str, Any]]:
    cfg = DEPTHS.get(depth, DEPTHS["standard"])
    yield {"type": "phase", "phase": "planning"}

    plan = await client.complete(model, [
        {"role": "system", "content": "You plan web research. Reply with only a JSON array of search queries."},
        {"role": "user", "content": (
            f"Question: {question}\n\nWrite {cfg['queries']} diverse, specific web search queries that together "
            "would answer this question well. Output only a JSON array of strings."
        )},
    ], max_tokens=2048)
    queries = _parse_json_list(plan)[: cfg["queries"]] or [question]
    yield {"type": "queries", "queries": queries}

    yield {"type": "phase", "phase": "searching"}
    results = await asyncio.gather(*(web.search(q, limit=6) for q in queries), return_exceptions=True)
    seen: dict[str, dict[str, str]] = {}
    for q, res in zip(queries, results):
        if isinstance(res, Exception):
            yield {"type": "warning", "message": f"Search failed for “{q}”: {res}"}
            continue
        for r in res:
            url = r["url"].split("#")[0]
            if url and url not in seen:
                seen[url] = r
    candidates = list(seen.values())[: cfg["sources"] + 4]
    yield {"type": "candidates", "count": len(candidates)}

    yield {"type": "phase", "phase": "reading"}
    sources: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(4)

    async def read(r: dict[str, str]) -> dict[str, Any] | None:
        async with sem:
            try:
                page = await web.fetch(r["url"], max_chars=cfg["chars"])
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc), "url": r["url"]}
            if len(page["text"]) < 200:
                return {"error": "too little readable text", "url": r["url"]}
            return {"url": page["url"], "title": page["title"] or r["title"], "text": page["text"], "snippet": r["snippet"]}

    for coro in asyncio.as_completed([read(r) for r in candidates]):
        item = await coro
        if item is None:
            continue
        if "error" in item:
            yield {"type": "source_failed", "url": item["url"], "error": item["error"]}
            continue
        if len(sources) >= cfg["sources"]:
            continue
        sources.append(item)
        yield {"type": "source", "index": len(sources), "url": item["url"], "title": item["title"]}

    if not sources:
        yield {"type": "error", "message": "No sources could be read. Check your network or search settings."}
        return

    yield {"type": "phase", "phase": "writing"}
    material = "\n\n".join(
        wrap_untrusted(f"[{i}] {s['url']}", f"Title: {s['title']}\n\n{s['text']}") for i, s in enumerate(sources, 1)
    )
    report_parts: list[str] = []
    messages = [
        {"role": "system", "content": (
            "You write rigorous research reports in Markdown. " + UNTRUSTED_POLICY + " "
            "Cite sources inline as [n] using the numbers given. Only state what the sources support; "
            "say clearly where they disagree or where evidence is thin. Do not include a sources list; it is appended for you."
        )},
        {"role": "user", "content": f"Research question: {question}\n\nSources:\n\n{material}\n\n"
                                    "Write the report: a 2–3 sentence answer up front, then sections with headings, then a short 'Open questions' section."},
    ]
    async for ev in client.stream(model, messages, [], max_tokens=8000, effort="medium"):
        if ev["type"] == "text":
            report_parts.append(ev["text"])
            yield {"type": "delta", "text": ev["text"]}

    report = "".join(report_parts).strip()
    refs = "\n".join(f"{i}. [{s['title'] or s['url']}]({s['url']})" for i, s in enumerate(sources, 1))
    full = f"{report}\n\n## Sources\n\n{refs}\n"
    title = question if len(question) <= 80 else question[:77] + "…"
    doc = documents.create(f"Research: {title}", full)
    yield {"type": "done", "document_id": doc["id"], "report": full,
           "sources": [{"url": s["url"], "title": s["title"]} for s in sources]}
