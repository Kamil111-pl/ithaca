"""Text embeddings.

If an embeddings provider is configured (any OpenAI-compatible /embeddings
endpoint, e.g. Ollama with nomic-embed-text), it is used. Otherwise a
built-in signed feature-hashing embedding is used: no download, instant,
and good at lexical overlap, though it has no real semantic understanding.
"""

from __future__ import annotations

import hashlib
import re

import numpy as np

from . import settings

HASH_DIM = 512
HASH_MODEL = "hashed-512"

STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have how i if in into is it its "
    "me my of on or our so that the their them they this to was we were what when where which who why will "
    "with you your".split()
)


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def content_words(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


def _hash_embed(text: str) -> np.ndarray:
    vec = np.zeros(HASH_DIM, dtype=np.float32)
    words = content_words(text)
    feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
    for w in words:
        padded = f"#{w}#"
        feats += [padded[i:i + 3] for i in range(max(1, len(padded) - 2))]
    for f in feats:
        h = hashlib.blake2b(f.encode(), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "little") % HASH_DIM
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = np.linalg.norm(vec)
    return vec / norm if norm else vec


async def embed(texts: list[str]) -> tuple[list[np.ndarray], str]:
    provider_id = settings.get("embeddings.provider_id")
    model = settings.get("embeddings.model")
    if provider_id and model:
        from .providers import get_client

        client = get_client(provider_id)
        if hasattr(client, "embed"):
            try:
                rows = await client.embed(model, texts)
                out = []
                for r in rows:
                    v = np.asarray(r, dtype=np.float32)
                    n = np.linalg.norm(v)
                    out.append(v / n if n else v)
                return out, f"{provider_id}:{model}"
            except Exception:  # noqa: BLE001 - fall back rather than lose the write
                pass
    return [_hash_embed(t) for t in texts], HASH_MODEL


def to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
