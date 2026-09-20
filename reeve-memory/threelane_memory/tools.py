"""Standard library functions that mirror the MCP tools for threelane-memory.

These functions provide a higher-level dictionary-based interface suitable for
automated agents and tools, wrapping the core memory operations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from threelane_memory import store
from threelane_memory.auth import current_user_id
from threelane_memory.backup import save_backup
from threelane_memory.config import get_provider_summary
from threelane_memory.database import check_index_dimension, get_index_dimension, run_query
from threelane_memory.entity_dedup import deduplicate_entities
from threelane_memory.reconciler import clear_speaker, consolidate
from threelane_memory.retriever import retrieve

logger = logging.getLogger(__name__)


class MonthlyQuotaExceededError(RuntimeError):
    """Raised when a plan's monthly query quota is exhausted."""


class MonthlyTokenQuotaExceededError(RuntimeError):
    """Raised when a plan's monthly token allowance is exhausted (hard-blocked plans)."""


def _enforce_monthly_token_quota() -> None:
    """Gate token-consuming ops on the plan's monthly token allowance.

    Tokens are metered *after* an op runs, so this checks the already-accumulated
    total: once at/over the cap, a hard-blocked plan (free) is rejected before the
    next op; top-up plans (pro/enterprise) are allowed to overflow and the excess
    keeps accumulating for metered billing. Local/CLI (no uid) and admins exempt.
    """
    uid = current_user_id.get()
    if not uid:
        return
    from threelane_memory.config import (
        ADMIN_UIDS,
        DEFAULT_PLAN,
        PLAN_MONTHLY_TOKEN_QUOTAS,
        TOKEN_TOPUP_PLANS,
    )

    if uid in ADMIN_UIDS:
        return
    from threelane_memory.database import get_monthly_tokens, get_user_plan

    plan = get_user_plan(uid) or DEFAULT_PLAN

    # Pay-as-you-go has no quota to exhaust, so the ceiling is the user's own
    # spend cap instead. Checked before the quota branch because an uncapped
    # PAYG account would otherwise sail past on the top-up exemption below.
    from threelane_memory.payg import enforce_spend_cap

    enforce_spend_cap(uid)

    quota = PLAN_MONTHLY_TOKEN_QUOTAS.get(plan, PLAN_MONTHLY_TOKEN_QUOTAS[DEFAULT_PLAN])
    if get_monthly_tokens(uid) < quota:
        return
    if plan in TOKEN_TOPUP_PLANS:
        return  # allow overflow; excess is billed via metered top-up
    raise MonthlyTokenQuotaExceededError(
        f"Monthly token limit reached: {get_monthly_tokens(uid):,}/{quota:,} tokens used "
        f"on the '{plan}' plan. The allowance resets on the 1st of the month. "
        "Upgrade at https://www.reeve.co.in/pricing"
    )


def _enforce_monthly_query_quota() -> None:
    """Reject query-serving calls once the caller's monthly plan quota is spent.

    Applies only to authenticated hosted callers; local/CLI use (no uid) and
    administrators are exempt. query_memory and retrieve_memory_context count
    equally — both are a "query served".
    """
    uid = current_user_id.get()
    if not uid:
        return
    from threelane_memory.config import ADMIN_UIDS, DEFAULT_PLAN, PLAN_MONTHLY_QUERY_QUOTAS

    if uid in ADMIN_UIDS:
        return
    from threelane_memory.database import get_user_plan, reserve_monthly_query

    plan = get_user_plan(uid) or DEFAULT_PLAN
    quota = PLAN_MONTHLY_QUERY_QUOTAS.get(plan, PLAN_MONTHLY_QUERY_QUOTAS[DEFAULT_PLAN])
    # Atomic reserve-then-check: exactly `quota` calls succeed per month; the
    # (quota+1)th gets used > quota and is rejected. No check-then-act window.
    used = reserve_monthly_query(uid)
    if used > quota:
        raise MonthlyQuotaExceededError(
            f"Monthly query quota exceeded: {min(used, quota + 1)}/{quota} queries "
            f"used on the '{plan}' plan. The quota resets on the 1st of the month. "
            "Upgrade at https://www.reeve.co.in/pricing"
        )


def _compose_speaker(uid: str, speaker: str | None) -> str:
    """Compose an account uid with a client-supplied namespace into a partition key.

    The ``uid`` prefix is the hard tenant boundary: whatever a client passes as
    ``speaker`` only ever partitions *within* its own account, so one account can
    never reach another account's data. The default namespace maps to the bare
    ``uid`` so that data written before namespace isolation existed stays reachable.
    """
    if not speaker or speaker == "default":
        return uid
    return f"{uid}:{speaker}"


def _get_effective_speaker(speaker: str) -> str:
    """Resolve the effective partition key from the auth context.

    On the hosted service an authenticated account ``uid`` is present in the
    request context; we compose it with the client's ``speaker`` so that
    namespaces stay isolated within an account (and accounts stay isolated from
    each other). With no auth context (self-hosted / local CLI) the namespace
    passes through untouched.
    """
    user_id = current_user_id.get()
    return _compose_speaker(user_id, speaker) if user_id else speaker


def store_memory(
    text: str,
    speaker: str = "default",
    image_base64: str | None = None,
    image_media_type: str = "image/jpeg",
    image_url: str | None = None,
) -> dict[str, Any]:
    """Store a memory entry into the 3-lane long-term memory graph.

    When an image is attached (``image_base64`` for byte-capable clients/SDK, or
    ``image_url`` for MCP clients — the server fetches it), two things happen:
    a multimodal model turns it into a text memory record (description +
    EXIF-GPS place, see vision.py) that flows through the normal pipeline, AND a
    multimodal *image* embedding is stored so the memory is searchable by image
    similarity.

    The image bytes are persisted ONLY when the operator has enabled the image
    store (``IMAGE_STORE_ENABLED``, off by default). When enabled they are held
    encrypted, expire on a retention clock, and are deleted by every erasure
    path — see image_store.py. When disabled, nothing but the description and
    the embedding survives the call.
    """
    _enforce_monthly_token_quota()
    vision_in = vision_out = 0
    image_embedding: list[float] | None = None
    image_key: str | None = None

    if image_url and not image_base64:
        from threelane_memory.multimodal import fetch_image_as_base64

        image_base64, image_media_type = fetch_image_as_base64(image_url)

    if image_base64:
        from threelane_memory import multimodal
        from threelane_memory.usage import get_usage, start_usage
        from threelane_memory.vision import image_to_memory_text

        # Separate scope: store() resets the accumulator, so vision/embedding
        # spend here is captured and added to the logged totals below.
        start_usage()
        image_text = image_to_memory_text(image_base64, image_media_type)
        if multimodal.is_configured():
            try:
                image_embedding = multimodal.embed_image_b64(image_base64)
            except Exception:
                image_embedding = None  # image search is best-effort
        vision_in, vision_out = get_usage()
        text = f"{text.strip()}\n\n{image_text}" if text and text.strip() else image_text
    effective_speaker = _get_effective_speaker(speaker)

    # Retain the original only if the operator has switched it on. Keyed by the
    # EFFECTIVE speaker so the object sits under the same tenant that erasure
    # will later sweep. Best-effort: a store outage costs the ability to re-read
    # this photo, never the memory itself.
    if image_base64:
        from threelane_memory import image_store

        image_key = image_store.put_image(
            effective_speaker, image_base64, image_media_type
        )

    result_id, t_in, t_out = store(
        text,
        speaker=effective_speaker,
        image_embedding=image_embedding,
        image_key=image_key,
    )
    t_in += vision_in
    t_out += vision_out
    try:
        from threelane_memory.database import log_request
        uid = current_user_id.get()
        if uid:
            # For sync, t_in/t_out are the real total (extraction LLM + embeddings,
            # summed in _store_sync). For async they are 0 here and the worker
            # forwards the real total to update_log_tokens via internal_id.
            log_request(uid, "store_memory", t_in, t_out, internal_id=result_id)
    except Exception:
        pass
    if result_id.startswith("tmp_"):
        return {
            "pending_id": result_id,
            "speaker": effective_speaker,
            "stored": False,
            "persisting": True,
        }
    return {"episode_id": result_id, "speaker": effective_speaker, "stored": True}


async def query_memory(
    question: str,
    speaker: str = "default",
    image_url: str | None = None,
    image_base64: str | None = None,
    image_media_type: str = "image/jpeg",
) -> str:
    """Query long-term memory and get a natural-language answer.

    Attach a photo (``image_url`` or ``image_base64``) to ask about it directly
    — "what did I do here?". The photo steers retrieval toward memories that
    LOOK like it, and, when the originals were retained, the answer is written
    by the vision model with the photos in view.
    """
    from threelane_memory import aquery
    from threelane_memory.usage import get_usage, start_usage

    _enforce_monthly_query_quota()
    _enforce_monthly_token_quota()
    effective_speaker = _get_effective_speaker(speaker)

    if image_url and not image_base64:
        from threelane_memory.multimodal import fetch_image_as_base64

        image_base64, image_media_type = fetch_image_as_base64(image_url)

    start_usage()
    tracked = await aquery(
        question,
        speaker=effective_speaker,
        return_tracked=True,
        image_base64=image_base64,
        image_media_type=image_media_type,
    )

    try:
        from threelane_memory.database import log_request
        uid = current_user_id.get()
        if uid:
            # Real total spend for the whole query: query enhancement + answer
            # LLM + every embedding, captured by the accumulator (aquery's own
            # returned tokens count only the final answer, so use the scope).
            tokens_in, tokens_out = get_usage()
            log_request(uid, "query_memory", tokens_in, tokens_out)
    except Exception:
        pass

    return tracked["text"]


# Two subjects in one sentence embed as one vector, and one vector retrieves one
# subject. "Tell me about my shoes and the movie poster" came back with only the
# poster; asked separately, both answered. The decomposition that fixes this
# already existed in `query_enhancer`, wired into `query_memory` only, so every
# caller running its own LLM over `retrieve_memory_context` (which is the whole
# point of that tool) silently had none of it.


def _retrieve_decomposed(question: str, speaker: str, image_base64: str | None) -> str:
    """Retrieve once, or once per subject when the question carries several.

    The enhancer decides what a subject is, because deciding that is a language
    question and it is the component that already answers it. There is no
    keyword test in front of this: any such rule is a worse classifier wearing
    a cheaper coat, and it fails on exactly the phrasings nobody thought to add
    to it. `enhance_query` short-circuits queries of six words or fewer without
    calling a model, so short lookups still cost nothing.
    """
    try:
        from threelane_memory.query_enhancer import enhance_query

        subs = [q.strip() for q in enhance_query(question).get("sub_queries", []) if q.strip()]
    except Exception:  # noqa: BLE001
        # Enhancement is an optimisation, never a dependency. A throttled or
        # malformed enhancer degrades to the previous single-query behaviour
        # rather than failing a retrieval that would otherwise have worked.
        logger.warning("query enhancement failed; retrieving unsplit", exc_info=True)
        subs = []

    if len(subs) < 2:
        return retrieve(question, speaker=speaker, image_base64=image_base64)

    # Safe: the MCP layer calls this tool through `asyncio.to_thread`, so this
    # runs on a worker thread with no event loop of its own to collide with.
    from threelane_memory.retriever import retrieve_multi

    return asyncio.run(retrieve_multi(subs, speaker=speaker, image_base64=image_base64))


def retrieve_memory_context(
    question: str,
    speaker: str = "default",
    image_url: str | None = None,
    image_base64: str | None = None,
) -> str:
    """Retrieve ranked raw memory context (pre-answer) for inspection.

    Accepts an attached photo like ``query_memory``, so callers running their
    own LLM get the same visually-matched context without Reeve narrating.
    """
    from threelane_memory.usage import get_usage, start_usage

    _enforce_monthly_query_quota()
    _enforce_monthly_token_quota()
    effective_speaker = _get_effective_speaker(speaker)

    if image_url and not image_base64:
        from threelane_memory.multimodal import fetch_image_as_base64

        image_base64, _ = fetch_image_as_base64(image_url)

    start_usage()
    context = _retrieve_decomposed(question, effective_speaker, image_base64)
    try:
        from threelane_memory.database import log_request
        uid = current_user_id.get()
        if uid:
            # No generative LLM runs here, but the query embedding does — so
            # tokens_in reflects the real embedding spend (not 0/0 anymore).
            tokens_in, tokens_out = get_usage()
            log_request(uid, "retrieve_memory_context", tokens_in, tokens_out)
    except Exception:
        pass
    return context


def _vision_configured() -> bool:
    try:
        from threelane_memory.vision import is_configured

        return is_configured()
    except Exception:
        return False


def _image_store_configured() -> bool:
    """True when original photos are being retained (opt-in, off by default)."""
    from threelane_memory import image_store

    return image_store.is_configured()


def _multimodal_configured() -> bool:
    try:
        from threelane_memory.multimodal import is_configured

        return is_configured()
    except Exception:
        return False


def search_image_memories(
    query: str = "",
    speaker: str = "default",
    image_url: str | None = None,
    image_base64: str | None = None,
) -> str:
    """Find photo memories by visual similarity (text→image or image→image).

    Matches on the multimodal *image* vector, so it finds memories by how the
    photo looks — "beach photos", or "photos like this one" via image_url /
    image_base64 — not just by their text description.
    """
    from threelane_memory.retriever import retrieve_image_memories
    from threelane_memory.usage import get_usage, start_usage

    _enforce_monthly_query_quota()
    _enforce_monthly_token_quota()
    effective_speaker = _get_effective_speaker(speaker)

    if image_url and not image_base64:
        from threelane_memory.multimodal import fetch_image_as_base64

        image_base64, _ = fetch_image_as_base64(image_url)

    start_usage()
    context = retrieve_image_memories(
        effective_speaker, query_text=query or None, image_base64=image_base64
    )
    try:
        from threelane_memory.database import log_request

        uid = current_user_id.get()
        if uid:
            tokens_in, tokens_out = get_usage()
            log_request(uid, "search_image_memories", tokens_in, tokens_out)
    except Exception:
        pass
    return context or "No matching photo memories found."


def memory_config() -> dict[str, Any]:
    """Show provider and vector-index health details."""
    from threelane_memory.config import (
        ASYNC_WRITE_ENABLED,
        GEO_ENRICHMENT_ENABLED,
        IMAGE_STORE_RETENTION_DAYS,
    )
    from threelane_memory.background_worker import is_running
    from threelane_memory.write_buffer import get_write_buffer

    info = get_provider_summary()
    status = {
        "provider": info["provider"],
        "chat_model": info["chat_model"],
        "embedding_dim": info["embedding_dim"],
        "neo4j_connected": False,
        "async_write_enabled": ASYNC_WRITE_ENABLED,
        "async_worker_running": is_running(),
        "async_pending_count": len(get_write_buffer().drain_pending()),
        "geo_enrichment_enabled": GEO_ENRICHMENT_ENABLED,
        "vision_enabled": _vision_configured(),
        "multimodal_image_search_enabled": _multimodal_configured(),
        # Whether ORIGINAL photos are being retained. Reported because it is the
        # one setting that changes what personal data the service holds, and
        # without it there is no way to confirm the state from outside the box —
        # turning retention on would be a blind change.
        "image_retention_enabled": _image_store_configured(),
        # 0 means "no expiry", but reporting a bare 0 next to enabled=true reads
        # like "expires in 0 days" — the opposite. Spell it out.
        "image_retention_days": (
            (IMAGE_STORE_RETENTION_DAYS if IMAGE_STORE_RETENTION_DAYS > 0 else "indefinite")
            if _image_store_configured()
            else 0
        ),
    }
    try:
        run_query("RETURN 1 AS ok")
        status["neo4j_connected"] = True
    except Exception:
        pass
    return status


def backup_memory(speaker: str | None = None, since: str | None = None) -> dict[str, str]:
    """Export memories to JSON backup. Optionally filter by speaker and date."""
    # A backup returns the whole (namespace's) graph — the heaviest possible
    # read. Count it as a query served so it can't be used to exfiltrate data
    # around the query quota.
    _enforce_monthly_query_quota()
    _enforce_monthly_token_quota()
    user_id = current_user_id.get()
    effective_speaker = _compose_speaker(user_id, speaker) if user_id else speaker

    path = save_backup(speaker=effective_speaker, since=since)
    try:
        from threelane_memory.database import log_request

        if user_id:
            log_request(user_id, "backup_memory", 0, 0)
    except Exception:
        pass
    return {"path": path}


def deduplicate_memory_entities(
    dry_run: bool = False, speaker: str = "default"
) -> dict[str, int | bool]:
    """Deduplicate entities in the memory graph.

    Entities are keyed by (name, speaker), so dedup only ever merges within one
    tenant. A normal authenticated user may deduplicate only their own graph
    (scope is forced to their uid). Administrators and unauthenticated local/CLI
    callers may run a global all-speakers maintenance pass, or target a specific
    speaker.
    """
    from threelane_memory.config import ADMIN_UIDS
    from threelane_memory.entity_dedup import deduplicate_all_speakers

    uid = current_user_id.get()
    if uid is not None and uid not in ADMIN_UIDS:
        # Authenticated non-admin: force scope to the caller's own account. An
        # explicit namespace cleans that namespace's graph; the uid prefix is
        # always enforced so the caller can never reach another account.
        result = deduplicate_entities(dry_run=dry_run, speaker=_compose_speaker(uid, speaker))
    elif speaker and speaker != "default":
        # Admin or local caller targeting one tenant explicitly.
        result = deduplicate_entities(dry_run=dry_run, speaker=speaker)
    else:
        # Admin or local caller: global maintenance across all tenants.
        result = deduplicate_all_speakers(dry_run=dry_run)

    return {
        "dry_run": dry_run,
        "duplicates_found": int(result.get("duplicates_found", 0)),
        "merged": int(result.get("merged", 0)),
    }


def consolidate_memory(speaker: str = "default") -> dict[str, Any]:
    """Consolidate old low-importance episodes for a speaker."""
    effective_speaker = _get_effective_speaker(speaker)
    return consolidate(effective_speaker)


def clear_memory(speaker: str = "default", dry_run: bool = False) -> dict[str, Any]:
    """Permanently delete all memory for a namespace (hard reset).

    Destructive and irreversible. Scoped through the same ``uid:namespace``
    composition as every other tool, so an authenticated caller can only ever
    clear their own account's data (or one of its namespaces) — never another
    account's. Pass ``dry_run=True`` to preview the counts without deleting.
    """
    effective_speaker = _get_effective_speaker(speaker)
    return clear_speaker(effective_speaker, dry_run=dry_run)
