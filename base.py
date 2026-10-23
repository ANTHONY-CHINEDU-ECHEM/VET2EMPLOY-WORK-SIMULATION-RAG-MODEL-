"""Embedding providers.

``OpenRouterEmbedder`` calls the OpenRouter embeddings endpoint (OpenAI compatible) so the
platform keeps a single provider key and billing account. ``HashingEmbedder`` is a
deterministic, dependency free embedder for offline development and tests only.
``CachedEmbedder`` wraps any provider with a persistent cache keyed by model and content hash.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import OrderedDict
from itertools import pairwise
from typing import Protocol

import httpx
import numpy as np
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from v2e_rag.domain.models import sha256_hex
from v2e_rag.observability.logging import get_logger
from v2e_rag.observability.metrics import EMBEDDING_REQUESTS
from v2e_rag.store.base import VectorStore
from v2e_rag.store.lexical import tokenize

log = get_logger(__name__)


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    model: str
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def aclose(self) -> None: ...


def _l2(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm > 0 else vector


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError | httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 409, 425, 429, 500, 502, 503, 504)
    return False


class OpenRouterEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str,
        dimensions: int,
        base_url: str = "https://openrouter.ai/api/v1",
        batch_size: int = 64,
        send_dimensions: bool = False,
        timeout: float = 60.0,
        max_retries: int = 3,
        app_url: str = "",
        app_title: str = "",
        data_collection: str = "deny",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.dimensions = dimensions
        self.batch_size = max(1, batch_size)
        self.send_dimensions = send_dimensions
        self.max_retries = max_retries
        self.data_collection = data_collection
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if app_url:
            headers["HTTP-Referer"] = app_url
        if app_title:
            headers["X-Title"] = app_title
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout
        )
        self._owns_client = client is None
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, object] = {
            "model": self.model,
            "input": texts,
            "provider": {"data_collection": self.data_collection},
        }
        if self.send_dimensions:
            payload["dimensions"] = self.dimensions
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential_jitter(initial=1, max=20),
            retry=retry_if_exception(_retryable),
            reraise=True,
        ):
            with attempt:
                response = await self._client.post("/embeddings", json=payload)
                response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise EmbeddingError(f"OpenRouter embeddings error: {body['error']}")
        data = sorted(body.get("data", []), key=lambda d: d.get("index", 0))
        if len(data) != len(texts):
            raise EmbeddingError(f"Expected {len(texts)} embeddings, received {len(data)}")
        vectors = [list(map(float, d["embedding"])) for d in data]
        for vector in vectors:
            if len(vector) != self.dimensions:
                raise EmbeddingError(
                    f"Model {self.model} returned {len(vector)} dimensions; configured {self.dimensions}. "
                    "Set V2E_EMBEDDING_DIMENSIONS to match the model."
                )
        EMBEDDING_REQUESTS.labels(model=self.model).inc()
        return [_l2(v) for v in vectors]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            out.extend(await self._embed_batch(texts[start : start + self.batch_size]))
        return out

    async def embed_query(self, text: str) -> list[float]:
        key = sha256_hex(text)
        if key in self._query_cache:
            self._query_cache.move_to_end(key)
            return self._query_cache[key]
        vector = (await self._embed_batch([text]))[0]
        self._query_cache[key] = vector
        if len(self._query_cache) > 2048:
            self._query_cache.popitem(last=False)
        return vector


class HashingEmbedder:
    """Deterministic feature hashing of stemmed unigrams and bigrams.

    Produces stable vectors with meaningful lexical similarity. It is not a semantic model
    and must never be used in production (the settings validator enforces this).
    """

    def __init__(self, dimensions: int = 384) -> None:
        self.model = f"hashing-{dimensions}"
        self.dimensions = dimensions

    async def aclose(self) -> None:
        return None

    def _vector(self, text: str) -> list[float]:
        vec = np.zeros(self.dimensions, dtype=np.float32)
        tokens = tokenize(text)
        features = tokens + [f"{a}_{b}" for a, b in pairwise(tokens)]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign
        norm = float(np.linalg.norm(vec))
        return (vec / norm).tolist() if norm > 0 else vec.tolist()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class CachedEmbedder:
    """Adds a persistent content hash cache in front of any embedder."""

    def __init__(self, inner: Embedder, store: VectorStore) -> None:
        self.inner = inner
        self.store = store
        self.model = inner.model
        self.dimensions = inner.dimensions

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        hashes = [sha256_hex(t) for t in texts]
        cached = await self.store.cached_embeddings(self.model, list(dict.fromkeys(hashes)))
        first_index: dict[str, int] = {}
        for i, h in enumerate(hashes):
            if h not in cached:
                first_index.setdefault(h, i)
        missing = list(first_index.values())
        if missing:
            fresh = await self.inner.embed_documents([texts[i] for i in missing])
            new_items = {hashes[i]: vector for i, vector in zip(missing, fresh, strict=True)}
            await self.store.cache_embeddings(self.model, new_items)
            cached.update(new_items)
        log.info("embeddings_resolved", total=len(texts), embedded=len(missing))
        return [cached[h] for h in hashes]

    async def embed_query(self, text: str) -> list[float]:
        return await self.inner.embed_query(text)


_WS = re.compile(r"\s+")


def prepare_embedding_text(text: str, max_chars: int = 24000) -> str:
    """Collapse whitespace and cap length below provider input limits."""
    return _WS.sub(" ", text).strip()[:max_chars]
