"""Long-term memory: SQLite FTS5 plus vectors, fused with reciprocal rank."""

from __future__ import annotations

import re
from typing import Any

import numpy as np

from . import db, embeddings


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "content": row["content"],
        "tags": db.loads(row["tags"], []),
        "tainted": bool(row["tainted"]),
        "created_at": row["created_at"],
    }


async def save(content: str, tags: list[str] | None = None, tainted: bool = False) -> dict[str, Any]:
    content = content.strip()
    if not content:
        raise ValueError("Memory content is empty.")
    (vec,), model = await embeddings.embed([content])
    mid = db.new_id("m_")
    db.execute(
        "INSERT INTO memories (id, content, tags, tainted, embedding, embed_model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mid, content, db.dumps(tags or []), int(tainted), embeddings.to_blob(vec), model, db.now()),
    )
    return _public(db.query_one("SELECT * FROM memories WHERE id = ?", (mid,)))  # type: ignore[arg-type]


async def update(memory_id: str, content: str) -> dict[str, Any] | None:
    (vec,), model = await embeddings.embed([content])
    db.execute(
        "UPDATE memories SET content = ?, embedding = ?, embed_model = ?, tainted = 0 WHERE id = ?",
        (content.strip(), embeddings.to_blob(vec), model, memory_id),
    )
    row = db.query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
    return _public(row) if row else None


def delete(memory_id: str) -> bool:
    return db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)) > 0


def list_all(limit: int = 500) -> list[dict[str, Any]]:
    return [_public(r) for r in db.query("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,))]


def _fts_query(text: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9]{2,}", text) if w.lower() not in embeddings.STOPWORDS][:12]
    return " OR ".join(f'"{w}"' for w in words)


async def search(query: str, k: int = 5, min_score: float = 0.0) -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        return []
    ranks: dict[str, float] = {}

    fq = _fts_query(query)
    if fq:
        rows = db.query(
            "SELECT m.id FROM memories_fts f JOIN memories m ON m.rowid = f.rowid "
            "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT 30",
            (fq,),
        )
        for i, r in enumerate(rows):
            ranks[r["id"]] = ranks.get(r["id"], 0) + 1 / (60 + i)

    (qvec,), model = await embeddings.embed([query])
    rows = db.query("SELECT id, embedding FROM memories WHERE embed_model = ? AND embedding IS NOT NULL", (model,))
    if rows:
        mat = np.stack([embeddings.from_blob(r["embedding"]) for r in rows])
        sims = mat @ qvec
        order = np.argsort(-sims)[:30]
        for i, idx in enumerate(order):
            if sims[idx] < 0.15:
                break
            mid = rows[int(idx)]["id"]
            ranks[mid] = ranks.get(mid, 0) + 1 / (60 + i)

    best = sorted(ranks.items(), key=lambda kv: -kv[1])[:k]
    out = []
    for mid, score in best:
        if score < min_score:
            continue
        row = db.query_one("SELECT * FROM memories WHERE id = ?", (mid,))
        if row:
            item = _public(row)
            item["score"] = round(score, 4)
            out.append(item)
    return out
