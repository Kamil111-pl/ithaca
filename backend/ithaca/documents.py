"""Markdown documents with full-text search."""

from __future__ import annotations

import re
from typing import Any

from . import db
from .embeddings import STOPWORDS


def create(title: str, content: str = "") -> dict[str, Any]:
    did = db.new_id("d_")
    t = db.now()
    db.execute(
        "INSERT INTO documents (id, title, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (did, title.strip() or "Untitled", content, t, t),
    )
    return get(did)  # type: ignore[return-value]


def get(doc_id: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM documents WHERE id = ?", (doc_id,))


def update(doc_id: str, *, title: str | None = None, content: str | None = None) -> dict[str, Any] | None:
    row = get(doc_id)
    if row is None:
        return None
    db.execute(
        "UPDATE documents SET title = ?, content = ?, updated_at = ? WHERE id = ?",
        (title if title is not None else row["title"], content if content is not None else row["content"], db.now(), doc_id),
    )
    return get(doc_id)


def delete(doc_id: str) -> bool:
    return db.execute("DELETE FROM documents WHERE id = ?", (doc_id,)) > 0


def list_all() -> list[dict[str, Any]]:
    rows = db.query("SELECT id, title, substr(content, 1, 240) AS preview, length(content) AS size, created_at, updated_at FROM documents ORDER BY updated_at DESC")
    return rows


def search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    words = [w for w in re.findall(r"[A-Za-z0-9]{2,}", query) if w.lower() not in STOPWORDS][:10]
    if not words:
        return []
    fq = " OR ".join(f'"{w}"' for w in words)
    return db.query(
        "SELECT d.id, d.title, snippet(documents_fts, 1, '[', ']', '…', 16) AS snippet, d.updated_at "
        "FROM documents_fts JOIN documents d ON d.rowid = documents_fts.rowid "
        "WHERE documents_fts MATCH ? ORDER BY bm25(documents_fts) LIMIT ?",
        (fq, limit),
    )


def replace_text(doc_id: str, old: str, new: str) -> tuple[bool, str]:
    row = get(doc_id)
    if row is None:
        return False, "Document not found."
    from .textedit import apply_edit

    updated, msg = apply_edit(row["content"], old, new)
    if updated is None:
        return False, msg
    update(doc_id, content=updated)
    return True, msg
