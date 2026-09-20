"""reeve — Personal long-term memory SDK.

This package acts as a client for a remote reeve/MCP server.
"""

from __future__ import annotations

from typing import Any

try:  # Single-source the version from installed package metadata
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("reeve")
except (ImportError, PackageNotFoundError):  # running from a source checkout
    __version__ = "0.0.0.dev0"

from reeve.agent import ReeveAgent
from reeve.middleware import (
    AgentMemory,
    ChatMessage,
    DurableMemoryExtractor,
    MemoryBackend,
    MemoryCandidate,
    MemoryInjection,
    MemoryRelationship,
    MemoryRelevancePolicy,
    MemoryWriteResult,
    ReeveMemory,
    ReeveMemoryBackend,
    ReeveMiddleware,
    ReeveMiddlewareConfig,
    RetrievalDecision,
)

# Import core client logic
from reeve.tools import (
    ReeveClient,
    backup_memory,
    consolidate_memory,
    deduplicate_memory_entities,
    memory_config,
    query_memory,
    retrieve_memory_context,
    search_image_memories,
    store_memory,
)


# Core API functions are directly available for convenience
def store(text: str, speaker: str = "default", **image_kwargs: Any) -> dict[str, Any]:
    return store_memory(text, speaker=speaker, **image_kwargs)


def query(question: str, speaker: str = "default", **image_kwargs: Any) -> str:
    return query_memory(question, speaker=speaker, **image_kwargs)


__all__ = [
    "__version__",
    "ReeveClient",
    "ReeveAgent",
    "ReeveMemory",
    "AgentMemory",
    "ReeveMiddleware",
    "ReeveMiddlewareConfig",
    "ChatMessage",
    "RetrievalDecision",
    "MemoryInjection",
    "MemoryBackend",
    "ReeveMemoryBackend",
    "MemoryRelevancePolicy",
    "MemoryCandidate",
    "MemoryRelationship",
    "MemoryWriteResult",
    "DurableMemoryExtractor",
    "store",
    "query",
    "store_memory",
    "query_memory",
    "retrieve_memory_context",
    "search_image_memories",
    "memory_config",
    "backup_memory",
    "deduplicate_memory_entities",
    "consolidate_memory",
]
