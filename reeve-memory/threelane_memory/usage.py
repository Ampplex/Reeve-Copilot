"""Request-scoped token accounting.

Every generative LLM call (via ``invoke_llm_tracked``) and every embedding call
feeds a per-request accumulator so a tool can report the *real, total* tokens it
spent — including internal processing the caller never sees directly, such as
query enhancement, entity/consolidation LLM passes, and query/document
embeddings.

Implementation note: the accumulator is a *mutable dict* held in a ContextVar.
``asyncio.to_thread`` (used by the multi-query retriever) copies the context but
not the dict, so worker threads mutate the *same* dict by reference and their
usage aggregates back into the parent — no cross-thread loss.
"""

from __future__ import annotations

import contextvars

_usage: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "reeve_usage", default=None
)


def start_usage() -> None:
    """Open a fresh usage scope for the current context/thread."""
    _usage.set({"tokens_in": 0, "tokens_out": 0})


def add_usage(tokens_in: int = 0, tokens_out: int = 0) -> None:
    """Add real provider-reported tokens to the active scope (no-op if none)."""
    bucket = _usage.get()
    if bucket is not None:
        bucket["tokens_in"] += int(tokens_in or 0)
        bucket["tokens_out"] += int(tokens_out or 0)


def get_usage() -> tuple[int, int]:
    """Return (tokens_in, tokens_out) accumulated in the active scope."""
    bucket = _usage.get()
    if bucket is None:
        return 0, 0
    return bucket["tokens_in"], bucket["tokens_out"]
