"""threelane-memory — Lifetime persistent memory for LLMs on Neo4j."""

from __future__ import annotations

import logging
import re
from typing import Any

from threelane_memory.embeddings import cosine_similarity, embed

__version__ = "0.1.8"


def _lazy_getattr(name: str) -> Any:
    """Lazy-load major entry points to keep the top-level namespace clean."""
    _lazy = {
        "operator_extract": ("threelane_memory.operator", "operator_extract"),
        "reconcile": ("threelane_memory.reconciler", "reconcile"),
        "retrieve": ("threelane_memory.retriever", "retrieve"),
        "invoke_llm": ("threelane_memory.llm_interface", "invoke_llm"),
        "aquery": ("threelane_memory", "aquery"),
        "consolidate": ("threelane_memory.reconciler", "consolidate"),
        "clear_speaker": ("threelane_memory.reconciler", "clear_speaker"),
        "reindex_embeddings": ("threelane_memory.reconciler", "reindex_embeddings"),
        "save_backup": ("threelane_memory.backup", "save_backup"),
        "close": ("threelane_memory.database", "close"),
        "deduplicate_entities": ("threelane_memory.entity_dedup", "deduplicate_entities"),
        "load_eval_cases": ("threelane_memory.evaluation", "load_eval_cases"),
        "evaluate_cases": ("threelane_memory.evaluation", "evaluate_cases"),
    }
    if name in _lazy:
        module_path, attr = _lazy[name]
        import importlib

        mod = importlib.import_module(module_path)
        val = getattr(mod, attr)
        globals()[name] = val  # cache for next access
        return val
    raise AttributeError(f"module 'threelane_memory' has no attribute {name!r}")


# Compatibility for lazy attributes
def __getattr__(name: str) -> Any:
    return _lazy_getattr(name)


# ── Convenience helpers ──────────────────────────────────────────────────────


def _store_sync(
    text: str,
    *,
    speaker: str = "default",
    image_embedding: list[float] | None = None,
    image_key: str | None = None,
) -> tuple[str, int, int]:
    """Run the durable memory write path synchronously.

    Returns (episode_id, tokens_in, tokens_out) where the tokens are the *total
    real* spend of this write — the extraction LLM plus every embedding call made
    during reconciliation — captured via the request-scoped usage accumulator.
    Works for the sync path and the async background worker alike (the worker
    calls this and forwards the totals to update_log_tokens).

    *image_embedding* (a multimodal image vector) is attached to the episode so
    photo memories are searchable by image similarity. *image_key* points at the
    retained original in the image store, so the vision model can be shown the
    actual photo for questions the description never anticipated.
    """
    from threelane_memory.operator import operator_extract
    from threelane_memory.reconciler import reconcile
    from threelane_memory.usage import get_usage, start_usage

    start_usage()
    semantics, _t_in, _t_out = operator_extract(text)
    episode_id = reconcile(
        semantics,
        speaker=speaker,
        raw_text=text,
        image_embedding=image_embedding,
        image_key=image_key,
    )
    tokens_in, tokens_out = get_usage()
    return episode_id, tokens_in, tokens_out


def store(
    text: str,
    *,
    speaker: str = "default",
    image_embedding: list[float] | None = None,
    image_key: str | None = None,
) -> tuple[str, int, int]:
    """Extract semantics from *text* and persist to the knowledge graph.

    Returns (id, tokens_in, tokens_out).
    If async, tokens are 0 because they happen in the background.
    """
    from threelane_memory.config import ASYNC_WRITE_ENABLED

    if not ASYNC_WRITE_ENABLED:
        return _store_sync(
            text, speaker=speaker, image_embedding=image_embedding, image_key=image_key
        )

    from threelane_memory.background_worker import enqueue, is_running, start_worker
    from threelane_memory.write_buffer import get_write_buffer

    if not is_running():
        start_worker()

    pending_id = get_write_buffer().add_pending(
        text, speaker, image_embedding=image_embedding, image_key=image_key
    )
    enqueue(pending_id)
    return pending_id, 0, 0


def _memory_fact_lines(context: str) -> list[str]:
    lines = []
    for raw_line in context.splitlines():
        line = raw_line.strip()
        if not line or line.endswith(":"):
            continue
        if line.startswith(("Conflict rule:", "Entities:", "Action:", "Relation:", "State:")):
            continue
        line = re.sub(r"^\[[^\]]+\]\s*", "", line).strip()
        line = re.sub(r"\s+\(summary:.*$", "", line).strip()
        if ":" in line:
            _, candidate = line.split(":", 1)
            candidate = candidate.strip()
            if re.match(r"^(I|We|My|Our)\b", candidate, re.IGNORECASE):
                line = candidate
        if line:
            lines.append(line)
    return lines


def _direct_context_answer(question: str, context: str) -> str | None:
    """Extract a direct answer when context has the exact first-person fact."""
    q = question.casefold()
    patterns: list[str] = []
    if re.search(r"\bwhat\b.*\b(do i use|am i using)\b", q):
        patterns.extend(
            [
                r"\bI use [^.!?\n]+",
                r"\bI am using [^.!?\n]+",
            ]
        )
    if re.search(r"\bwhat\b.*\bdo i prefer\b", q) or "what do i prefer" in q:
        patterns.append(r"\bI prefer [^.!?\n]+")
    if re.search(r"\bwhat\b.*\b(do we use|are we using)\b", q):
        patterns.extend(
            [
                r"\bWe use [^.!?\n]+",
                r"\bWe are using [^.!?\n]+",
            ]
        )
    if re.search(r"\bwhat\b.*\bdo we prefer\b", q):
        patterns.append(r"\bWe prefer [^.!?\n]+")

    for line in _memory_fact_lines(context):
        for pattern in patterns:
            match = re.search(pattern, line, re.IGNORECASE)
            if not match:
                continue
            answer = match.group(0).strip()
            if answer and answer[-1] not in ".!?":
                answer += "."
            return answer
    return None


def _answer_from_photos(
    question: str,
    ctx: str,
    image_episode_ids: list[str],
    speaker: str,
    attached_b64: str | None,
    attached_media_type: str,
) -> str | None:
    """Answer with the actual photos in view, or None to fall back to text.

    Returns None far more often than not — retention is opt-in, objects expire,
    and most questions never match a photo. Every one of those is a normal path
    back to the ordinary text answer, so this must never raise.
    """
    if not attached_b64 and not image_episode_ids:
        return None
    try:
        from threelane_memory import vision
        from threelane_memory.config import IMAGE_ANSWER_MAX_IMAGES
        from threelane_memory.retriever import fetch_episode_images

        if not vision.is_configured():
            return None

        pairs: list[tuple[str, str]] = []
        if attached_b64:
            pairs.append((attached_b64, attached_media_type))
        if image_episode_ids:
            pairs.extend(
                fetch_episode_images(image_episode_ids, speaker, IMAGE_ANSWER_MAX_IMAGES)
            )
        if not pairs:
            return None

        converse: list[tuple[str, str]] = []
        for b64, media_type in pairs[:IMAGE_ANSWER_MAX_IMAGES]:
            try:
                converse.append((b64, vision._converse_format(media_type)))
            except ValueError:
                continue  # unsupported format: skip this one, keep the rest
        if not converse:
            return None
        return vision.answer_with_images(question, ctx, converse) or None
    except Exception:
        logging.getLogger(__name__).debug("Visual answer unavailable", exc_info=True)
        return None


async def aquery(
    question: str,
    *,
    speaker: str = "default",
    return_tracked: bool = False,
    enhanced: dict[str, Any] | None = None,
    image_base64: str | None = None,
    image_media_type: str = "image/jpeg",
) -> str | dict[str, Any]:
    """Retrieve relevant memories and answer *question* via LLM (async).

    *image_base64* attaches a photo to the question. When the question matches
    the speaker's own photos — or a photo is attached — and the originals were
    retained, the answer is produced by the VISION model with those photos in
    view, so it can address what the picture shows rather than only what the
    caption happened to record.
    """
    from threelane_memory.llm_interface import invoke_llm_tracked
    from threelane_memory.query_enhancer import enhance_query
    from threelane_memory.retriever import retrieve, retrieve_multi

    # 1. Intelligence Layer: Enhance query and detect intent
    if enhanced is None:
        enhanced = enhance_query(question)
    
    intent = enhanced["intent"]

    # 2. Handle Storage intent
    if intent == "storage":
        from threelane_memory.operator import operator_extract
        from threelane_memory.reconciler import reconcile
        
        semantics, t_in, t_out = operator_extract(question)
        episode_id = reconcile(semantics, speaker=speaker, raw_text=question)
        res = {
            "text": f"I've recorded that memory (episode {episode_id}).",
            "tokens_in": t_in,
            "tokens_out": t_out
        }
        return res if return_tracked else res["text"]

    # 3. Retrieve context based on intent
    lanes: dict[str, list[str]] = {}
    if intent in ("synthesis", "recommendation") or len(enhanced["sub_queries"]) > 1:
        ctx = await retrieve_multi(enhanced["sub_queries"], speaker=speaker)
    else:
        # Fallback to standard retrieval for simple lookups
        retrieval_query = enhanced["sub_queries"][0] if enhanced["sub_queries"] else question
        ctx = retrieve(
            retrieval_query, speaker=speaker, image_base64=image_base64, lanes_out=lanes
        )

    if not ctx.strip():
        res = {"text": "I don't remember.", "tokens_in": 0, "tokens_out": 0}
        return res if return_tracked else res["text"]

    # 3b. Visual answer — only when the image lane actually matched (its
    #     calibrated break test IS the intent signal, so no second heuristic) or
    #     the user attached a photo. Falls through to the text answer whenever
    #     the originals were not retained or have aged out.
    visual_answer = _answer_from_photos(
        question, ctx, lanes.get("image", []), speaker, image_base64, image_media_type
    )
    if visual_answer:
        res = {"text": visual_answer, "tokens_in": 0, "tokens_out": 0}
        return res if return_tracked else res["text"]

    # 4. Direct context answer (only for fact_lookup)
    if intent == "fact_lookup":
        direct_answer = _direct_context_answer(question, ctx)
        if direct_answer:
            res = {"text": direct_answer, "tokens_in": 0, "tokens_out": 0}
            return res if return_tracked else res["text"]

    # 5. Final answer synthesis/lookup
    if intent in ("synthesis", "recommendation"):
        goal_line = f"Goal context: {enhanced['goal_context']}\n" if enhanced["goal_context"] else ""
        prompt = (
            "You are a personal memory assistant with access to the user's stored memories. "
            "Your job is to reason over these memories — connecting people, events, feelings, "
            "facts, patterns, and experiences — to give a thoughtful, personalised answer.\n\n"
            "Do NOT just list facts. Synthesize them into a coherent response that actually "
            "addresses what the user is asking, drawing on whatever is relevant in memory.\n\n"
            "If the memory context is sparse, use what is available and acknowledge any gaps honestly.\n\n"
            f"{goal_line}"
            f"Memory Context:\n{ctx}\n\n"
            f"Question: {question}\n"
        )
        tracked = invoke_llm_tracked(prompt)
        return tracked if return_tracked else tracked["text"]

    # Standard factual lookup prompt.
    # The memory context below has ALREADY been retrieved as relevant to the
    # question, so the answer LLM must trust it and answer even when the phrasing
    # differs (a memory "in Goa" answers "near Goa"; a place inside a region
    # answers a distance question). An earlier, stricter "answer ONLY if the
    # exact answer is present, else say I don't remember" wording made the model
    # over-refuse on any wording mismatch — while keeping the anti-hallucination
    # guard (rule 5) and the refusal backstop (rule 4).
    prompt = (
        "You are a personal memory assistant. Answer the user's question using the "
        "memory context below, which has already been retrieved as relevant to the "
        "question.\n\n"
        "RULES:\n"
        "1. Answer from the memory context. Treat the memories as relevant even when "
        "their wording differs from the question — e.g. a memory that the user was "
        "'in Goa' answers 'what did I do near Goa?', and a place within a region "
        "answers a distance question about that region.\n"
        "2. Treat short-term memory as authoritative over Reeve long-term memory on "
        "conflicts, and prefer the most recently dated item — it is the latest "
        "information.\n"
        "3. Answer in one sentence or less unless the question requires more detail; "
        "do not add suggestions or conversational filler.\n"
        "4. Only if the context contains nothing related to the question at all, "
        "respond with exactly: 'I don't remember.'\n"
        "5. Do not invent facts that the memory context does not support.\n\n"
        f"Memory Context:\n{ctx}\n\n"
        f"Question: {question}"
    )
    tracked = invoke_llm_tracked(prompt)
    return tracked if return_tracked else tracked["text"]


def query(
    question: str, *, speaker: str = "default", return_tracked: bool = False
) -> str | dict[str, Any]:
    """Retrieve relevant memories and answer *question* via LLM (sync)."""
    import asyncio
    try:
        # Try to use the existing event loop if one is running
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # This is a bit of a hack, but for sync callers in an async env,
            # we might need to use a separate thread or just warn.
            # In threelane-memory, most sync callers are CLI or tests.
            import warnings
            warnings.warn(
                "Sync query() called from a running event loop. This may hang. "
                "Use aquery() instead.", 
                RuntimeWarning
            )
            # Fallback to old sync logic to avoid hanging the loop entirely?
            # Or just try to run it. asyncio.run() will fail here.
    except RuntimeError:
        pass

    return asyncio.run(aquery(question, speaker=speaker, return_tracked=return_tracked))


__all__ = [
    "__version__",
    # High-level API
    "store",
    "query",
    "aquery",
    "close",
    # Core pipeline
    "operator_extract",
    "reconcile",
    "retrieve",
    "invoke_llm",
    # Utilities
    "embed",
    "cosine_similarity",
    "consolidate",
    "clear_speaker",
    "reindex_embeddings",
    "save_backup",
    "deduplicate_entities",
    "load_eval_cases",
    "evaluate_cases",
]
