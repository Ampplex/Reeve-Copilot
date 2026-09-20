"""Embedding wrapper — supports Ollama and OpenAI embedding providers."""

from __future__ import annotations

from typing import Any, cast

import numpy as np
from langchain_core.embeddings import Embeddings

from threelane_memory.config import (
    AWS_REGION,
    BEDROCK_API_KEY,
    BEDROCK_EMBED_MODEL,
    EMBEDDING_PROVIDER,
    OLLAMA_BASE_URL,
    OLLAMA_EMBED_MODEL,
    OPENAI_API_KEY,
    OPENAI_EMBED_MODEL,
)

# ── Build the embeddings client based on provider (lazily) ───────────────────

_embeddings: Embeddings | None = None


def _build_embeddings() -> Embeddings:
    if EMBEDDING_PROVIDER == "ollama":
        from langchain_ollama import OllamaEmbeddings

        return OllamaEmbeddings(
            model=OLLAMA_EMBED_MODEL,
            base_url=OLLAMA_BASE_URL,
        )
    if EMBEDDING_PROVIDER == "bedrock":
        from threelane_memory.bedrock_llm import BedrockEmbeddingsAPIKey

        if not BEDROCK_API_KEY:
            raise ValueError(
                "Bedrock API key not set for embeddings. "
                "Add to .env:\n  BEDROCK_API_KEY=your-bedrock-api-key"
            )
        return BedrockEmbeddingsAPIKey(
            api_key=BEDROCK_API_KEY,
            model_id=BEDROCK_EMBED_MODEL,
            region=AWS_REGION,
        )
    try:
        from langchain_openai import OpenAIEmbeddings  # type: ignore[import-not-found]
    except ImportError:
        raise ImportError(
            "OpenAI embedding dependencies not found. "
            "Install with: pip install 'threelane-memory[openai]'"
        )

    if not OPENAI_API_KEY:
        raise ValueError(
            "OpenAI API key not set for embeddings. Add to .env:\n  OPENAI_API_KEY=sk-your-api-key"
        )
    return OpenAIEmbeddings(
        model=OPENAI_EMBED_MODEL,
        api_key=cast(Any, OPENAI_API_KEY),
    )


def _get_embeddings() -> Embeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = _build_embeddings()
    return _embeddings


from functools import lru_cache

@lru_cache(maxsize=1000)
def embed(text: str) -> list[float]:
    """Return an embedding vector for *text*."""
    if not text or not text.strip():
        return []  # Return empty list instead of raising error for empty text if preferred, 
                   # but here we follow original logic mostly.
    
    # Original logic below, but we must handle the cacheable part carefully.
    # lru_cache requires the function to be deterministic and arguments hashable.
    return _embed_internal(text)

def _embed_internal(text: str) -> list[float]:
    """Internal non-cached embedding logic."""
    try:
        vec = cast(list[float], _get_embeddings().embed_query(text))
        arr = np.asarray(vec, dtype=float)
        if arr.size == 0:
            raise RuntimeError("Embedding provider returned an empty vector.")
        if not np.all(np.isfinite(arr)):
            raise RuntimeError("Embedding provider returned non-finite values.")
        if np.linalg.norm(arr) == 0:
            raise RuntimeError(
                "Embedding provider returned an all-zero vector. "
                "Check EMBEDDING_PROVIDER/OLLAMA_BASE_URL/OpenAI settings."
            )
        # Bedrock records the provider's *real* input tokens inside its wrapper.
        # Other providers don't surface a count through langchain, so add a
        # labeled estimate here (avoids double-counting the Bedrock path).
        if EMBEDDING_PROVIDER != "bedrock":
            try:
                from threelane_memory.usage import add_usage

                add_usage(tokens_in=estimate_embedding_tokens(text))
            except Exception:
                pass
        return [float(x) for x in arr.tolist()]
    except Exception as e:
        raise RuntimeError(
            f"Failed to generate embedding via provider '{EMBEDDING_PROVIDER}': {e}"
        ) from e



def estimate_embedding_tokens(text: str) -> int:
    """Approximate the input tokens an embedding call consumes, for cost accounting.

    Embedding providers (Bedrock/Ollama/OpenAI) don't surface an exact input-token
    count through the langchain ``embed_query`` interface used above, so this is a
    deliberate, provider-agnostic **estimate** using the ~4-chars-per-token rule of
    thumb. Embeddings produce no generated tokens, so only input is counted.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def cosine_similarity(vec1, vec2) -> float:
    """Cosine similarity between two vectors."""
    v1, v2 = np.asarray(vec1), np.asarray(vec2)
    n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
    if n1 == 0 or n2 == 0:
        return 0.0
    return float(np.dot(v1, v2) / (n1 * n2))
