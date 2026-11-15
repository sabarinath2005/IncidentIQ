"""Real, bounded embedding calls. No local feature-hash fallback is provided."""
from __future__ import annotations

import math
import time

import httpx

from app.core.config import settings


class EmbeddingUnavailable(RuntimeError):
    pass


def _chunks(text: str) -> list[str]:
    words = text.split()
    size = settings.embedding_chunk_words
    overlap = min(settings.embedding_chunk_overlap_words, size - 1)
    result, offset = [], 0
    while offset < len(words):
        value = " ".join(words[offset:offset + size]).strip()
        if value:
            result.append(value)
        if offset + size >= len(words):
            break
        offset += size - overlap
    return result


def chunk_document(text: str) -> list[str]:
    return _chunks(text)


def embed_texts(texts: list[str], transport: httpx.BaseTransport | None = None) -> tuple[list[list[float]], str]:
    if settings.embedding_provider.strip().lower() != "openai":
        raise EmbeddingUnavailable("A real embedding provider is not configured")
    if not settings.openai_api_key:
        raise EmbeddingUnavailable("OpenAI embeddings are selected but OPENAI_API_KEY is not configured")
    if not texts or len(texts) > 64 or any(not value.strip() for value in texts):
        raise EmbeddingUnavailable("Embedding input is empty or exceeds the request limit")
    timeout = httpx.Timeout(max(2.0, min(float(settings.llm_timeout_seconds), 90.0)))
    retries = max(0, min(int(settings.llm_max_retries), 3))
    started = time.monotonic()
    with httpx.Client(timeout=timeout, transport=transport) as client:
        for attempt in range(retries + 1):
            try:
                response = client.post("https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                    json={"model": settings.openai_embedding_model, "input": texts,
                          "dimensions": settings.embedding_dimensions})
            except httpx.HTTPError as exc:
                if attempt < retries:
                    time.sleep(min(0.25 * (2 ** attempt), 1.0))
                    continue
                raise EmbeddingUnavailable("Embedding provider is unavailable") from exc
            if response.status_code == 429 or response.status_code in {500, 502, 503, 504}:
                if attempt < retries:
                    try:
                        delay = float(response.headers.get("retry-after", ""))
                    except ValueError:
                        delay = 0.25 * (2 ** attempt)
                    time.sleep(min(2.0, max(0.0, delay)))
                    continue
            if response.status_code == 401:
                raise EmbeddingUnavailable("Embedding provider rejected its configured credentials")
            if response.status_code == 429:
                raise EmbeddingUnavailable("Embedding provider rate limit persisted after bounded retries")
            if response.status_code >= 500:
                raise EmbeddingUnavailable("Embedding provider is temporarily unavailable")
            if response.is_error:
                raise EmbeddingUnavailable(f"Embedding provider rejected the request (HTTP {response.status_code})")
            try:
                payload = response.json()
                rows = sorted(payload["data"], key=lambda item: item["index"])
                vectors = [[float(value) for value in row["embedding"]] for row in rows]
                if len(vectors) != len(texts) or any(len(v) != settings.embedding_dimensions for v in vectors):
                    raise ValueError("Unexpected embedding dimensions")
                if any(not math.isfinite(value) for vector in vectors for value in vector):
                    raise ValueError("Non-finite embedding")
                return vectors, payload.get("model", settings.openai_embedding_model)
            except (KeyError, TypeError, ValueError) as exc:
                raise EmbeddingUnavailable("Embedding provider returned invalid vectors") from exc
    raise EmbeddingUnavailable(f"Embedding request exceeded its retry budget after {time.monotonic() - started:.2f}s")
