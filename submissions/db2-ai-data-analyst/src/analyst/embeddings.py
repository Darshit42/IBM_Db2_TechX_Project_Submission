"""Text embeddings via Gemini (LangChain), with a deterministic offline fallback.

The fallback (hashed character n-grams) keeps the app, tests and the vector search
path working without an API key; quality is lower but similar questions still match.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from functools import lru_cache

from .config import Settings, settings


class Embedder:
    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self.dim = cfg.embed_dim
        # One client per key; same model, so vectors stay in the same space across keys.
        self._clients = []
        if cfg.api_keys:
            from langchain_google_genai import GoogleGenerativeAIEmbeddings

            self._clients = [
                GoogleGenerativeAIEmbeddings(model=cfg.embed_model, google_api_key=k) for k in cfg.api_keys
            ]

    @property
    def provider(self) -> str:
        return f"gemini:{self.cfg.embed_model}" if self._clients else "local-hash"

    def embed(self, text: str) -> list[float]:
        return self._embed_cached(text.strip())

    @lru_cache(maxsize=512)
    def _embed_cached(self, text: str) -> list[float]:
        if not self._clients:
            return hash_embed(text, self.dim)
        last: Exception | None = None
        for client in self._clients:  # fail over to the next key on quota / transient errors
            try:
                vec = client.embed_query(text, output_dimensionality=self.dim)
                return _normalize(list(vec)[: self.dim])
            except Exception as e:  # noqa: BLE001 - provider raises several error types
                last = e
        raise RuntimeError(f"All embedding keys failed: {last}")

    def embed_many(self, texts: list[str], batch: int = 90, progress=None) -> list[list[float]]:
        """Batch embedding for bulk ingestion.

        The free tier counts every text against a per-minute, per-key quota, so batches are
        spread round-robin over the configured keys and a 429 waits for the window to reset.
        """
        if not self._clients:
            return [hash_embed(t, self.dim) for t in texts]
        out: list[list[float]] = []
        turn = 0
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            last: Exception | None = None
            for attempt in range(len(self._clients) * 6):
                client = self._clients[turn % len(self._clients)]
                turn += 1
                try:
                    vecs = client.embed_documents(chunk, output_dimensionality=self.dim)
                    out.extend(_normalize(list(v)[: self.dim]) for v in vecs)
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    quota = "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e)
                    if not quota:
                        continue
                    if (attempt + 1) % len(self._clients) == 0:  # every key is rate limited
                        time.sleep(20)
            else:
                raise RuntimeError(f"All embedding keys failed: {last}")
            if progress:
                progress(len(out), len(texts))
        return out

    def __hash__(self) -> int:  # lru_cache on a method needs a hashable self
        return id(self)


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def hash_embed(text: str, dim: int) -> list[float]:
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    words = text.split()
    feats = words + [f"{a}_{b}" for a, b in zip(words, words[1:])]
    for w in words:
        padded = f"#{w}#"
        feats += [padded[i:i + 3] for i in range(len(padded) - 2)]
    v = [0.0] * dim
    for f in feats:
        h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
        v[h % dim] += 1.0 if (h >> 32) & 1 else -1.0
    return _normalize(v)


def to_vector_literal(v: list[float]) -> str:
    """Db2 VECTOR() accepts a JSON-style array string."""
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
