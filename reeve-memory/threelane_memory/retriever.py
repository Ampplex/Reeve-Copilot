"""Smart retriever – Neo4j vector index + graph traversal for targeted recall.

Designed for 70-year lifespans:
  • Recency is a light tiebreaker (5%), not a dominant signal.
  • High-importance episodes ignore recency entirely (importance floor).
  • Candidate pool scales dynamically with graph size.
  • Temporal queries (date-range) are supported as a first-class path.
"""

import asyncio
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from threelane_memory.config import (
    IMPORTANCE_FLOOR,
    RECENCY_HALF_LIFE_DAYS,
    SIMILARITY_THRESHOLD,
    VECTOR_CANDIDATES_GLOBAL_MAX,
    VECTOR_CANDIDATES_MAX,
    VECTOR_CANDIDATES_MIN,
    VECTOR_CANDIDATES_RATIO,
    WEIGHT_IMPORTANCE,
    WEIGHT_RECENCY,
    WEIGHT_SIMILARITY,
)

# ── Tunables ──────────────────────────────────────────────────────────────────
TOP_K = 12  # max episodes to retrieve
MIN_SCORE = 0.18  # drop episodes below this combined score
RECENT_WINDOW_MINUTES = 5  # always include episodes this fresh
# (covers vector index lag)
RECENT_SAFETY_MAX = 2  # keep recent recall from crowding out relevance
EVENT_SIBLING_LIMIT = 4  # pull nearby scene lines from the same event
MAX_EXPANDED_EPISODES = 24  # context cap after event-sibling expansion

# "What was the last picture I uploaded?" had no path to an answer, and the
# failure was structural rather than a tuning miss. Every other lane matches on
# what a memory SAYS: the vector lane on meaning, fulltext on words, temporal on
# dates. A photo's stored text is a description of the scene, so it contains
# neither "picture" nor "uploaded", and no amount of similarity reaches it. The
# one photo-aware lane is the ambient image lane, which needs
# IMAGE_LANE_MIN_SAMPLE photos before it will admit anything, so a person with
# one or two photos had nothing at all. The honest-sounding "I don't remember"
# was wrong: the system knew perfectly well, it just had no way to be asked.
#
# This lane answers a question about the STORE rather than about meaning —
# "which of my memories are photographs, newest first" — which is a lookup, not
# a guess, so it needs no similarity threshold. It fires only when the question
# actually mentions a picture, and it is capped, so ordinary questions are
# untouched.
PHOTO_RECENCY_MAX = 3
_PHOTO_WORDS = re.compile(
    r"\b(photo|photos|photograph|photographs|picture|pictures|pic|pics"
    r"|image|images|snap|snaps|screenshot|screenshots)\b",
    re.IGNORECASE,
)


# "the big picture" is a figure of speech, and it is common enough in the kind
# of note this app holds ("keep the big picture in mind") to be worth excluding
# by name. No attempt is made at general idiom detection — the cost of a false
# fire is a few extra photos in the context, not a wrong answer.
_PHOTO_IDIOMS = re.compile(r"\bbig\s+pictures?\b|\bpicture[- ]perfect\b", re.IGNORECASE)


def _asks_about_photos(question: str) -> bool:
    if not question:
        return False
    return bool(_PHOTO_WORDS.search(_PHOTO_IDIOMS.sub(" ", question)))


logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _recency_weight(ts_iso: str | None) -> float:
    """Exponential decay based on age.  Returns 0‑1.

    With RECENCY_HALF_LIFE_DAYS=365 a 10-year-old memory still scores ~0.001
    (instead of ~0 with 30-day half-life).
    """
    if not ts_iso:
        return 0.5
    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
    except Exception:
        return 0.5
    return math.exp(-0.693 * age_days / RECENCY_HALF_LIFE_DAYS)


def _combined_score(sim: float, importance: float, recency: float) -> float:
    """Weighted combination with importance-floor bypass.

    Episodes with importance >= IMPORTANCE_FLOOR get recency=1.0 so that
    major life events are never penalised for being old.
    """
    if importance >= IMPORTANCE_FLOOR:
        recency = 1.0  # never penalise landmark memories
    return WEIGHT_SIMILARITY * sim + WEIGHT_IMPORTANCE * importance + WEIGHT_RECENCY * recency


# One shared worker pool for the whole process rather than a fresh
# ThreadPoolExecutor per retrieval. Measured: creating a pool per call cost more
# than the serialisation it removed — a retrieval against a low-latency database
# went from 33 ms to 55 ms median, because spawning seven threads twice per query
# dwarfs a 1 ms round trip. Reusing threads keeps the win where round trips are
# slow (a hosted database over the network) without paying setup on every call.
#
# Sized for several concurrent retrievals, each fanning out to at most seven
# lanes and seven expansion queries. Threads are idle almost all the time — they
# are waiting on the database, not computing — so this is cheap.
_POOL_MAX_WORKERS = 32
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _worker_pool() -> ThreadPoolExecutor:
    """Lazily create the shared pool; never torn down for the process lifetime."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(
                    max_workers=_POOL_MAX_WORKERS, thread_name_prefix="reeve-retrieve"
                )
    return _pool


# These counts only size the ANN candidate pool — they are a tuning input, not
# an answer — so a slightly stale value costs nothing, while recomputing them on
# every query costs a graph scan. Cached briefly per speaker.
_COUNTS_TTL_SECONDS = 60.0
_counts_cache: dict[str, tuple[float, int, int]] = {}
_counts_lock = threading.Lock()


def _episode_counts(speaker: str, _now=time.monotonic) -> tuple[int, int]:
    """Return (tenant_total, global_total) episode counts.

    Split into two statements on purpose. The single-pass version read a
    property inside an aggregate::

        MATCH (ep:Episode)
        RETURN sum(CASE WHEN ep.speaker = $speaker THEN 1 ELSE 0 END), count(ep)

    which forces every Episode in the database to be loaded and inspected — so
    one tenant's query latency grew with every *other* tenant's data. Separated,
    the global count is answered from the label count store and the tenant count
    is an index seek on ``episode_speaker`` (see migrations.episode_index_v1).
    """
    from threelane_memory.database import run_query

    now = _now()
    with _counts_lock:
        hit = _counts_cache.get(speaker)
        if hit and now - hit[0] < _COUNTS_TTL_SECONDS:
            return hit[1], hit[2]

    # count(ep) with no predicate: Neo4j answers this from stored label counts
    # without touching nodes.
    total_rows = run_query("MATCH (ep:Episode) RETURN count(ep) AS total")
    global_total = int((total_rows[0]["total"] if total_rows else 0) or 0)

    tenant_rows = run_query(
        "MATCH (ep:Episode {speaker:$speaker}) RETURN count(ep) AS tenant",
        {"speaker": speaker},
    )
    tenant_total = int((tenant_rows[0]["tenant"] if tenant_rows else 0) or 0)

    with _counts_lock:
        # Bound the cache so a long-lived process serving many speakers cannot
        # grow it without limit; these entries are cheap to rebuild.
        if len(_counts_cache) > 512:
            _counts_cache.clear()
        _counts_cache[speaker] = (now, tenant_total, global_total)

    return tenant_total, global_total


def _dynamic_candidates(global_total: int) -> int:
    """Base ANN candidate count sized from the GLOBAL episode count.

    The `episode_embedding` index spans every speaker and results are
    post-filtered by speaker, so sizing from a single tenant's count starved
    small tenants of hits.  Sizing from the global count keeps a tenant's own
    nearest neighbours in the base pool; a one-shot widening retry in
    find_relevant_episodes covers the remaining tail.
    """
    candidates = int(global_total * VECTOR_CANDIDATES_RATIO)
    return max(VECTOR_CANDIDATES_MIN, min(candidates, VECTOR_CANDIDATES_MAX))


def _vector_episode_rows(q_vec: list[float], speaker: str, candidates: int) -> list[dict]:
    """Run the global ANN query and post-filter the raw rows by speaker."""
    from threelane_memory.database import run_query

    return run_query(
        "CALL db.index.vector.queryNodes('episode_embedding', $candidates, $vec) "
        "YIELD node, score "
        "WHERE node.speaker = $speaker "
        "RETURN node.id AS id, score AS sim, "
        "       node.importance AS imp, toString(node.timestamp) AS ts",
        {"candidates": candidates, "vec": q_vec, "speaker": speaker},
    )


# ── Temporal query helpers ────────────────────────────────────────────────────

_YEAR_RE = re.compile(r"\b(18|19|20)\d{2}\b")
_MONTH_NAMES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_RELATIVE_RE = re.compile(r"(?:last|past)\s+(\d+)\s+(day|week|month|year)s?", re.IGNORECASE)
_AGE_RE = re.compile(
    r"(?:\bage\s+|aged\s+|at\s+age\s+|when\s+\w+\s+was\s+)(\d{1,3})",
    re.IGNORECASE,
)
_FULLTEXT_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "what",
    "who",
    "where",
    "when",
    "why",
    "how",
    "did",
    "does",
    "was",
    "were",
    "had",
    "has",
    "have",
    "from",
    "that",
    "this",
    "his",
    "her",
    "him",
    "she",
    "they",
    "them",
    "arthur",
    "jennings",
}


def _extract_time_range(text: str) -> tuple[str, str] | None:
    """Try to pull a (start_dt, end_dt) from the question. Returns None if no temporal cue."""
    lower = text.lower()
    now = datetime.now(timezone.utc)

    # "last N days/weeks/months/years"
    m = _RELATIVE_RE.search(lower)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days_map = {"day": 1, "week": 7, "month": 30, "year": 365}
        delta_days = n * days_map.get(unit, 1)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start - __import__("datetime").timedelta(days=delta_days)
        return start.isoformat(), now.isoformat()

    # Explicit year mentions like "in 2030"
    year_match = _YEAR_RE.search(text)
    if year_match:
        year = int(year_match.group())
        # Check for month
        month_start, month_end = 1, 12
        for name, num in _MONTH_NAMES.items():
            if name in lower:
                month_start = month_end = num
                break
        from datetime import timedelta

        start = datetime(year, month_start, 1, tzinfo=timezone.utc)
        if month_end == 12:
            end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        else:
            end = datetime(year, month_end + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
        return start.isoformat(), end.isoformat()

    return None


def _extract_metadata_filters(text: str) -> dict:
    """Infer coarse event metadata constraints from natural-language questions."""
    lower = text.lower()
    filters: dict = {}

    age_match = _AGE_RE.search(lower)
    if age_match:
        filters["age"] = int(age_match.group(1))

    if any(term in lower for term in ("birth", "born", "newborn")):
        filters["max_age"] = 0
        filters.setdefault("keywords", []).extend(["birth", "born", "new arrival"])
    elif any(term in lower for term in ("baby", "infant", "cradle")):
        filters["max_age"] = 1

    if any(term in lower for term in ("childhood", "early life", "early-life")):
        filters["phase_contains"] = "childhood"

    return filters


def _metadata_episode_ids(speaker: str, filters: dict, limit: int = TOP_K) -> list[str]:
    """Fetch episodes matching inferred structured metadata such as age/phase."""
    if not filters:
        return []

    from threelane_memory.database import run_query

    clauses = ["ep.speaker = $speaker", "ep.consolidated_into IS NULL"]
    params = {"speaker": speaker, "limit": limit}

    if "age" in filters:
        params["age"] = filters["age"]
        params["age_marker"] = f"(age {filters['age']})"
        clauses.append(
            "($age IN coalesce(ep.character_ages, []) "
            "OR toLower(coalesce(ep.raw_text, '')) CONTAINS $age_marker)"
        )

    if "max_age" in filters:
        params["max_age"] = filters["max_age"]
        markers = [f"(age {age})" for age in range(filters["max_age"] + 1)]
        params["age_markers"] = markers
        clauses.append(
            "(any(age IN coalesce(ep.character_ages, []) WHERE age <= $max_age) "
            "OR any(marker IN $age_markers WHERE "
            "toLower(coalesce(ep.raw_text, '')) CONTAINS marker))"
        )

    if filters.get("phase_contains"):
        params["phase_contains"] = filters["phase_contains"]
        clauses.append(
            "(toLower(coalesce(ep.event_phase, '')) CONTAINS $phase_contains "
            "OR toLower(coalesce(ep.raw_text, '')) CONTAINS $phase_contains)"
        )

    if filters.get("keywords"):
        params["keywords"] = [str(k).lower() for k in filters["keywords"]]
        clauses.append(
            "any(keyword IN $keywords WHERE "
            "toLower(coalesce(ep.event_title, '')) CONTAINS keyword "
            "OR toLower(coalesce(ep.raw_text, '')) CONTAINS keyword "
            "OR toLower(coalesce(ep.summary, '')) CONTAINS keyword)"
        )

    query = (
        "MATCH (ep:Episode) "
        f"WHERE {' AND '.join(clauses)} "
        "RETURN ep.id AS id "
        "ORDER BY ep.importance DESC, coalesce(ep.event_time, ep.timestamp) ASC "
        "LIMIT $limit"
    )
    rows = run_query(query, params)
    return [r["id"] for r in rows]


def _temporal_episode_ids(
    speaker: str, start_iso: str, end_iso: str, limit: int = TOP_K
) -> list[str]:
    """Fetch episode IDs within a date range, ordered by importance DESC."""
    from threelane_memory.database import run_query

    rows = run_query(
        "MATCH (ep:Episode {speaker:$speaker}) "
        "WHERE coalesce(ep.event_time, ep.timestamp) >= datetime($start) "
        "  AND coalesce(ep.event_time, ep.timestamp) <= datetime($end) "
        "RETURN ep.id AS id "
        "ORDER BY ep.importance DESC, coalesce(ep.event_time, ep.timestamp) DESC "
        "LIMIT $limit",
        {"speaker": speaker, "start": start_iso, "end": end_iso, "limit": limit},
    )
    return [r["id"] for r in rows]


def _recent_episode_ids(speaker: str, minutes: int = 5) -> list[str]:
    """Fetch episodes created in the last *minutes* minutes.

    This is a safety net: Neo4j vector indexes update asynchronously so a
    brand-new episode might not appear in ANN search yet.  By always
    including very recent episodes we guarantee the user's latest input is
    available for retrieval immediately.
    """
    from threelane_memory.database import run_query

    rows = run_query(
        "MATCH (ep:Episode {speaker:$speaker}) "
        "WHERE ep.timestamp >= datetime() - duration({minutes: $mins}) "
        "RETURN ep.id AS id "
        "ORDER BY ep.timestamp DESC",
        {"speaker": speaker, "mins": minutes},
    )
    return [r["id"] for r in rows]


def _recent_photo_episode_ids(speaker: str, limit: int = PHOTO_RECENCY_MAX) -> list[str]:
    """This speaker's photo memories, newest first.

    ``image_key`` is set only when the original photo was retained, which makes
    it the exact marker for "this memory is a picture". Ordering is by time
    because that is what the question asks — "the LAST picture" — and no
    similarity is involved: nothing here is being guessed at.
    """
    from threelane_memory.database import run_query

    try:
        rows = run_query(
            "MATCH (ep:Episode {speaker:$speaker}) "
            "WHERE ep.image_key IS NOT NULL AND ep.consolidated_into IS NULL "
            "RETURN ep.id AS id ORDER BY ep.timestamp DESC LIMIT $limit",
            {"speaker": speaker, "limit": limit},
        )
    except Exception as exc:
        logger.debug("Photo recency lookup failed: %s", exc)
        return []
    return [r["id"] for r in rows]


def _fulltext_query(text: str) -> str:
    """Build a Lucene-friendly keyword query from a user question."""
    tokens = re.findall(r"[A-Za-z0-9]+", text.lower())
    keywords = [t for t in tokens if len(t) > 2 and t not in _FULLTEXT_STOPWORDS]
    # Lucene treats whitespace as OR-ish query syntax in Neo4j fulltext search;
    # keep this simple and robust rather than passing punctuation-heavy questions.
    return " ".join(keywords[:12])


def _fulltext_episode_scores(question: str, speaker: str, top_k: int) -> list[tuple[str, float]]:
    """Return full-text hits with a normalized score boost, if the index exists."""
    from threelane_memory.database import ensure_episode_fulltext_index, run_query

    query = _fulltext_query(question)
    if not query:
        return []

    if not ensure_episode_fulltext_index(quiet=True):
        return []

    try:
        ft_rows = run_query(
            "CALL db.index.fulltext.queryNodes('episode_raw_text', $query) "
            "YIELD node, score "
            "WHERE node.speaker = $speaker AND node.consolidated_into IS NULL "
            "RETURN node.id AS id, score "
            "ORDER BY score DESC "
            "LIMIT $limit",
            {"query": query, "speaker": speaker, "limit": top_k},
        )
    except Exception as exc:
        logger.debug("Full-text retrieval failed: %s", exc)
        return []

    if not ft_rows:
        return []

    max_score = max(float(row.get("score") or 0.0) for row in ft_rows) or 1.0
    return [
        (row["id"], 0.72 + 0.28 * (float(row.get("score") or 0.0) / max_score)) for row in ft_rows
    ]


def _merge_ranked_candidates(
    groups: list[tuple[str, list[tuple[str, float]]]],
    limit: int,
) -> list[str]:
    """Merge candidate groups by score while keeping each lane's rank as a tiebreaker."""
    best: dict[str, tuple[float, int, str]] = {}
    for lane_index, (lane_name, candidates) in enumerate(groups):
        for rank, (eid, score) in enumerate(candidates):
            # Earlier lane wins ties only; score remains the main ordering signal.
            adjusted = score - (rank * 0.0001) - (lane_index * 0.00001)
            if eid not in best or adjusted > best[eid][0]:
                best[eid] = (adjusted, rank, lane_name)

    ordered = sorted(best.items(), key=lambda item: item[1][0], reverse=True)
    return [eid for eid, _ in ordered[:limit]]


# ── Core retrieval (Neo4j vector index) ──────────────────────────────────────


def _rank_in_range_by_residual(in_range_ids: list[str], residual: str, top_k: int) -> list[str]:
    """Rank the distance-filtered episodes by the query's non-spatial residual.

    Distance is already enforced (in_range_ids), so this only re-orders that set
    by semantic similarity to what the user actually asked ("restaurants I
    liked") — using the episodes' stored embeddings. Falls back to the given
    order (nearest-first) if embeddings/scoring are unavailable.
    """
    from threelane_memory.database import run_query
    from threelane_memory.embeddings import cosine_similarity, embed

    try:
        q_vec = embed(residual)
        rows = run_query(
            "UNWIND $ids AS eid MATCH (ep:Episode {id:eid}) "
            "RETURN ep.id AS id, ep.embedding AS emb",
            {"ids": in_range_ids},
        )
        scored = [
            (cosine_similarity(q_vec, r["emb"]), r["id"]) for r in rows if r.get("emb")
        ]
        if not scored:
            return in_range_ids[:top_k]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [rid for _, rid in scored][:top_k]
    except Exception as exc:
        logger.debug("Residual ranking failed: %s", exc)
        return in_range_ids[:top_k]


def find_relevant_episodes(
    question: str,
    speaker: str,
    top_k: int = TOP_K,
    image_base64: str | None = None,
    lanes_out: dict[str, list[str]] | None = None,
) -> list[str]:
    """Return up to *top_k* episode IDs most relevant to *question*.

    Pipeline:
      0. Fetch a tiny recent-episode safety net to beat index lag.
      1. Check for temporal cues → date-range query if found.
      2. Infer lightweight metadata filters such as "age 1" or "birth".
      3. Geo lane: explicit spatial cues ("near X", "places like X").
      4. Dynamic ANN search → re-rank with importance + recency.
      5. Full-text search for exact names/objects/sensory details.
      6. Image lane: match the question — or an attached photo — against how the
         stored photos LOOK, not how they were described.
      7. Merge all lanes by score, with recent memories as a capped fallback.

    *image_base64* attaches a photo to the question ("what did I do here?"); it
    drives the image lane instead of the question text.

    *lanes_out*, when given, is filled with {lane_name: [episode_id]} so callers
    can tell WHICH lane matched. The answer path uses this to decide whether to
    show the vision model the actual photos — reusing the image lane's own
    verdict rather than re-running the embedding or inventing a second heuristic.
    """
    from threelane_memory.embeddings import embed

    # ── Geo distance filter: stays sequential because it can short-circuit ──
    # A distance-bounded query ("(which restaurants did I like) within 50 km of
    # Pune") splits into a HARD distance filter and a semantic residual: only
    # in-range episodes are eligible, and they are ranked by the rest of the
    # question — so out-of-range places can never be re-added, and in dense
    # areas the relevant in-range places win over merely-nearest ones. It
    # returns outright, so running the other lanes alongside it would be work
    # thrown away. Vibe queries ("places like X") fall through to the merge and
    # so join the parallel wave below.
    run_geo_vibe = False
    try:
        from threelane_memory.geo import (
            distance_filter_ids,
            geo_lane_candidates,
            is_distance_query,
            strip_spatial_clause,
        )

        if is_distance_query(question):
            in_range = distance_filter_ids(question, speaker)
            if in_range:
                residual = strip_spatial_clause(question)
                if residual.strip():
                    return _rank_in_range_by_residual(in_range, residual, top_k)
                return in_range[:top_k]  # pure distance query → nearest-first
            # distance query but no in-range hit → fall through to normal lanes
        else:
            run_geo_vibe = True
    except Exception:
        run_geo_vibe = False

    # Counts first, and deliberately not in the wave: they decide whether the
    # vector lane runs at all, and skipping it for an empty tenant is what keeps
    # a brand-new user's first query from paying for a pointless embedding call.
    # Cached per speaker, so this is usually free.
    tenant_total, global_total = _episode_counts(speaker)

    # Cheap regex parsing — no I/O, so it stays on this thread.
    time_range = _extract_time_range(question)
    metadata_filters = _extract_metadata_filters(question)

    # ── The remaining lanes are independent, so they run concurrently ──────
    # Each one used to wait for the one before it even though none consumes
    # another's output. The wall time is now the slowest lane (usually the
    # vector lane, which pays for an embedding call) rather than their sum.
    #
    # Exception semantics are preserved exactly: geo and image swallow their own
    # failures because retrieval must survive them, while the others propagate —
    # Future.result() re-raises in this thread, so a failing lane still aborts
    # the retrieval the way it always did.

    def _lane_recent() -> list[str]:
        return _recent_episode_ids(speaker, minutes=RECENT_WINDOW_MINUTES)[:RECENT_SAFETY_MAX]

    def _lane_temporal() -> list[str]:
        if not time_range:
            return []
        return _temporal_episode_ids(speaker, *time_range, limit=top_k)

    def _lane_metadata() -> list[str]:
        return _metadata_episode_ids(speaker, metadata_filters, limit=top_k)

    def _lane_fulltext() -> list[tuple[str, float]]:
        return _fulltext_episode_scores(question, speaker, top_k)

    def _lane_geo() -> list[tuple[str, float]]:
        if not run_geo_vibe:
            return []
        try:
            return geo_lane_candidates(question, speaker, limit=top_k)
        except Exception:
            return []

    def _lane_vector() -> list[tuple[str, float]]:
        if tenant_total <= 0:  # skip the vector lane entirely for empty tenants
            return []
        q_vec = embed(question)
        candidates = _dynamic_candidates(global_total)
        rows = _vector_episode_rows(q_vec, speaker, candidates)

        # Widen once if the global pool surfaced too few of THIS tenant's own
        # episodes. Measured on raw speaker-filtered rows (before the
        # similarity/score cuts) so genuinely low-similarity queries don't
        # trigger a pointless second ANN pass.
        target = min(top_k, tenant_total)
        if len(rows) < target and candidates < VECTOR_CANDIDATES_GLOBAL_MAX:
            rows = _vector_episode_rows(q_vec, speaker, VECTOR_CANDIDATES_GLOBAL_MAX)

        scored = []
        for r in rows:
            sim = r["sim"]
            if sim < SIMILARITY_THRESHOLD * 0.5:
                continue
            recency = _recency_weight(r.get("ts"))
            importance = r.get("imp", 0.5)
            score = _combined_score(sim, importance, recency)
            if score >= MIN_SCORE:
                scored.append((r["id"], score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def _lane_photo() -> list[str]:
        # Only when the question mentions a picture at all. Without that guard
        # every question would drag the newest photos into context.
        if not _asks_about_photos(question):
            return []
        return _recent_photo_episode_ids(speaker)

    def _lane_image() -> list[tuple[str, float]]:
        # A vision caption only records what the captioner thought worth saying,
        # so "the red dish" misses a curry described as "creamy with coconut
        # flakes". The image vector has no such gap. Skipped entirely for
        # speakers with no photos, so text-only tenants never pay the extra
        # multimodal embedding call.
        #
        # The gate applies to an ATTACHED photo too. Bypassing it there looked
        # reasonable — the user pointed at a picture, so intent is explicit —
        # but it admits every photo the speaker owns in score order, and the
        # vision answer then reads whichever happened to rank first. Measured: a
        # curry probe put a soup photo above the actual dinner and the answer
        # came back about pizza. Explicit intent settles WHETHER to search, not
        # whether the result is any good; only search_image_memories skips it.
        try:
            if image_base64 or _speaker_has_image_memories(speaker):
                return image_lane_scores(
                    speaker,
                    query_text=None if image_base64 else question,
                    image_base64=image_base64,
                    top_k=top_k,
                )
        except Exception:
            return []  # never let the image lane break retrieval
        return []

    _lanes = {
        "recent": _lane_recent,
        "temporal": _lane_temporal,
        "metadata": _lane_metadata,
        "fulltext": _lane_fulltext,
        "geo": _lane_geo,
        "vector": _lane_vector,
        "image": _lane_image,
        "photo": _lane_photo,
    }
    _pool_ref = _worker_pool()
    _futures = {name: _pool_ref.submit(fn) for name, fn in _lanes.items()}
    # Resolved in a fixed order so that when several lanes fail, the exception
    # that surfaces is deterministic rather than a race.
    _out = {name: _futures[name].result() for name in _lanes}

    ids_recent = _out["recent"]
    ids_from_time = _out["temporal"]
    ids_from_metadata = _out["metadata"]
    fulltext_scored = _out["fulltext"]
    geo_scored = _out["geo"]
    vector_scored = _out["vector"]
    image_scored = _out["image"]
    ids_photo = _out["photo"]

    if lanes_out is not None:
        lanes_out["image"] = [eid for eid, _ in image_scored]

    return _merge_ranked_candidates(
        [
            ("geo", geo_scored),
            ("temporal", [(eid, 1.15) for eid in ids_from_time]),
            ("metadata", [(eid, 1.1) for eid in ids_from_metadata]),
            ("fulltext", fulltext_scored),
            ("vector", vector_scored),
            ("image", image_scored),
            # Above the ambient image lane and below an exact textual hit: when
            # someone says "picture", their pictures belong in the context, but
            # a question that also names a specific memory should still find it.
            ("photo", [(eid, 1.05 - 0.05 * i) for i, eid in enumerate(ids_photo)]),
            ("recent", [(eid, 0.25) for eid in ids_recent]),
        ],
        limit=top_k,
    )


# ── Subgraph expansion ───────────────────────────────────────────────────────


def _episode_ids_with_event_siblings(episode_ids: list[str]) -> list[str]:
    """Add same-event sibling episodes while preserving primary relevance order."""
    if not episode_ids:
        return []

    from threelane_memory.database import run_query

    try:
        rows = run_query(
            "UNWIND $ids AS eid "
            "MATCH (hit:Episode {id:eid}) "
            "OPTIONAL MATCH (sib:Episode) "
            "WHERE hit.event_id IS NOT NULL "
            "  AND sib.speaker = hit.speaker "
            "  AND sib.event_id = hit.event_id "
            "  AND sib.consolidated_into IS NULL "
            "WITH eid, sib "
            "ORDER BY coalesce(sib.event_time, sib.timestamp) ASC, sib.timestamp ASC "
            "RETURN eid AS hit_id, collect(sib.id) AS sibling_ids",
            {"ids": episode_ids},
        )
    except Exception:
        rows = []

    siblings_by_hit = {
        row["hit_id"]: [sid for sid in row.get("sibling_ids", []) if sid] for row in rows
    }

    seen = set()
    expanded: list[str] = []
    for eid in episode_ids:
        if eid not in seen:
            seen.add(eid)
            expanded.append(eid)
        for sibling_id in siblings_by_hit.get(eid, [])[:EVENT_SIBLING_LIMIT]:
            if sibling_id not in seen:
                seen.add(sibling_id)
                expanded.append(sibling_id)
            if len(expanded) >= MAX_EXPANDED_EPISODES:
                return expanded
    return expanded


def _render_location(loc: dict) -> str:
    """Render an enriched location: 'Taj Mahal — Agra, Uttar Pradesh, India (card)'."""
    name = str(loc["location"])
    where = ", ".join(
        str(bit) for bit in (loc.get("city"), loc.get("region"), loc.get("country")) if bit
    )
    head = f"{name} — {where}" if where else name
    card = loc.get("place_card")
    return f"{head} ({card})" if card else head


def expand_episodes(episode_ids: list[str], speaker: str) -> str:
    """Given episode IDs, traverse their subgraphs and return formatted context.

    All entity-touching sub-queries are rooted at the caller's own episodes and
    scoped to *speaker*, so no other tenant's entity/role data can enter the
    context on a name collision.
    """
    if not episode_ids:
        return ""

    from threelane_memory.database import run_query

    episode_ids = _episode_ids_with_event_siblings(episode_ids)

    # The seven statements below are independent — every one of them needs only
    # `episode_ids` — but they used to run one after another, so a retrieval paid
    # seven sequential network round trips to the database for data that could
    # have been fetched at once.
    #
    # They are dispatched concurrently instead. Note what this deliberately does
    # NOT do: merging them into a single Cypher statement with CALL subqueries
    # also works and saves more hops, but `collect()` inside a subquery walks
    # relationships in a different order than the unbound MATCH does, so the
    # rendered "Entities:" line came out permuted. That text is what the answer
    # model reads, and the old order — though itself only an artefact of the
    # planner — is what every existing prompt was tuned against. Running the
    # original queries unchanged keeps the output byte-identical while still
    # collapsing seven serial waits into one.
    #
    # Thread-safety: run_query opens its own session per call, and the driver's
    # connection pool is designed for concurrent use, so each task is isolated.
    _params = {"ids": episode_ids}
    _speaker_params = {"ids": episode_ids, "speaker": speaker}

    _queries: dict[str, tuple[str, dict]] = {
        # Episodes (exclude originals that were consolidated into another episode)
        "episodes": (
            "UNWIND range(0, size($ids) - 1) AS ord "
            "WITH $ids[ord] AS eid, ord "
            "MATCH (ep:Episode {id:eid}) "
            "WHERE ep.consolidated_into IS NULL "
            "RETURN ord, ep.id AS id, ep.summary AS summary, "
            "       ep.raw_text AS raw_text, ep.emotion AS emotion, "
            "       ep.importance AS importance, toString(ep.timestamp) AS ts, "
            "       ep.event_id AS event_id, ep.event_title AS event_title, "
            "       ep.event_year AS event_year, ep.event_phase AS event_phase "
            "ORDER BY ord",
            _params,
        ),
        # Entities involved (follow ALIAS_OF to show canonical names)
        "entities": (
            "UNWIND $ids AS eid "
            "MATCH (ep:Episode {id:eid})-[:INVOLVES]->(e:Entity) "
            "OPTIONAL MATCH (e)-[:ALIAS_OF]->(canon:Entity) "
            "RETURN DISTINCT ep.id AS ep_id, coalesce(canon.name, e.name) AS entity",
            _params,
        ),
        "actions": (
            "UNWIND $ids AS eid "
            "MATCH (ep:Episode {id:eid})-[:HAS_ACTION]->(a:Action)-[:BY_ENTITY]->(actor:Entity) "
            "OPTIONAL MATCH (a)-[:ON_ENTITY]->(obj:Entity) "
            "RETURN ep.id AS ep_id, actor.name AS actor, a.verb AS verb, "
            "       coalesce(obj.name, a.object_name) AS object",
            _params,
        ),
        "relations": (
            "UNWIND $ids AS eid "
            "MATCH (ep:Episode {id:eid})-[:HAS_RELATION]->(rel:Relation)"
            "-[:FROM_ENTITY]->(subject:Entity) "
            "MATCH (rel)-[:TO_ENTITY]->(object:Entity) "
            "OPTIONAL MATCH (subject)-[:ALIAS_OF]->(subject_canon:Entity) "
            "OPTIONAL MATCH (object)-[:ALIAS_OF]->(object_canon:Entity) "
            "RETURN ep.id AS ep_id, coalesce(subject_canon.name, subject.name) AS subject, "
            "       rel.type AS relation, coalesce(object_canon.name, object.name) AS object",
            _params,
        ),
        # States: coalesce handles old State nodes with no 'active' property yet,
        # and ALIAS_OF resolves canonical entity names.
        "states": (
            "UNWIND $ids AS eid "
            "MATCH (ep:Episode {id:eid})-[:HAS_STATE]->(s:State)-[:OF_ENTITY]->(e:Entity) "
            "OPTIONAL MATCH (e)-[:ALIAS_OF]->(canon:Entity) "
            "RETURN ep.id AS ep_id, coalesce(canon.name, e.name) AS entity, "
            "       s.attribute AS attr, s.value AS val, coalesce(s.active, true) AS active",
            _params,
        ),
        # Roles (for involved entities + their aliases), re-rooted through THIS
        # tenant's episodes and scoped to $speaker. Rooting at the episode instead
        # of a global name lookup is what closes the cross-tenant role leak.
        "roles": (
            "UNWIND $ids AS eid "
            "MATCH (ep:Episode {id:eid, speaker:$speaker})-[:INVOLVES]->"
            "(e:Entity {speaker:$speaker}) "
            "OPTIONAL MATCH (e)-[:ALIAS_OF]->(canon:Entity {speaker:$speaker}) "
            "WITH coalesce(canon, e) AS ent "
            "OPTIONAL MATCH (alias:Entity {speaker:$speaker})-[:ALIAS_OF]->(ent) "
            "WITH ent, collect(DISTINCT alias) AS aliases "
            "UNWIND ([ent] + aliases) AS member "
            "MATCH (member)-[:HAS_ROLE]->(r:Role) "
            "RETURN DISTINCT ent.name AS entity, r.name AS role",
            _speaker_params,
        ),
        # Locations (place_card + geographic hierarchy carry the enriched place)
        "locations": (
            "UNWIND $ids AS eid "
            "MATCH (ep:Episode {id:eid})-[:AT_LOCATION]->(loc:Location) "
            "RETURN ep.id AS ep_id, loc.name AS location, loc.place_card AS place_card, "
            "       loc.city AS city, loc.region AS region, loc.country AS country",
            _params,
        ),
    }

    _pool_ref = _worker_pool()
    futures = {
        name: _pool_ref.submit(run_query, cypher, params)
        for name, (cypher, params) in _queries.items()
    }
    results = {name: future.result() for name, future in futures.items()}

    episodes = results["episodes"]
    entities = results["entities"]
    actions = results["actions"]
    relations = results["relations"]
    states = results["states"]
    roles = results["roles"]
    locations = results["locations"]

    # (Locations come from the merged query above, alongside the other
    # per-episode child lists.)

    # ── Format ────────────────────────────────────────────────────────────
    lines = []
    for ep in episodes:
        # Show raw text if available (contains the actual user words)
        display = ep.get("raw_text") or ep["summary"]
        metadata = []
        if ep.get("event_id") is not None:
            metadata.append(f"event_id={ep['event_id']}")
        if ep.get("event_title"):
            metadata.append(f"title={ep['event_title']}")
        if ep.get("event_year"):
            metadata.append(f"year={ep['event_year']}")
        if ep.get("event_phase"):
            metadata.append(f"phase={ep['event_phase']}")
        metadata_text = f" [{' | '.join(metadata)}]" if metadata else ""
        lines.append(
            f"[{ep.get('ts', '?')}]{metadata_text} {display}  "
            f"(summary: {ep['summary']}, emotion={ep['emotion']}, importance={ep['importance']})"
        )

        ep_entities = [e["entity"] for e in entities if e["ep_id"] == ep["id"]]
        if ep_entities:
            lines.append(f"  Entities: {', '.join(ep_entities)}")

        ep_actions = [a for a in actions if a["ep_id"] == ep["id"]]
        for a in ep_actions:
            obj = f" → {a['object']}" if a.get("object") else ""
            lines.append(f"  Action: {a['actor']} {a['verb']}{obj}")

        ep_relations = [r for r in relations if r["ep_id"] == ep["id"]]
        for r in ep_relations:
            lines.append(f"  Relation: {r['subject']} {r['relation']} {r['object']}")

        ep_states = [s for s in states if s["ep_id"] == ep["id"]]
        for s in ep_states:
            status = "" if s.get("active", True) else " (superseded)"
            lines.append(f"  State: {s['entity']}.{s['attr']} = {s['val']}{status}")

        ep_locs = [loc for loc in locations if loc["ep_id"] == ep["id"]]
        if ep_locs:
            rendered = [_render_location(loc) for loc in ep_locs]
            lines.append(f"  Location: {', '.join(rendered)}")

        lines.append("")

    if roles:
        lines.append("Roles:")
        for r in roles:
            lines.append(f"  {r['entity']} → {r['role']}")

    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────


SHORT_TERM_MEMORY_HEADER = (
    "Short-term memory context (newest; overrides Reeve long-term memory on conflicts):"
)
LONG_TERM_MEMORY_HEADER = (
    "Long-term memory context (Reeve; use when not contradicted by short-term memory):"
)
MERGED_CONTEXT_CONFLICT_RULE = (
    "Conflict rule: if short-term memory and Reeve long-term memory disagree, "
    "prefer the short-term memory fact because it is the latest information."
)


def _pending_context(speaker: str) -> str:
    try:
        from threelane_memory.config import ASYNC_WRITE_ENABLED, TEMP_BUFFER_MAX_AGE_SECONDS

        if not ASYNC_WRITE_ENABLED:
            return ""

        from threelane_memory.write_buffer import get_write_buffer

        pending = get_write_buffer().get_pending_for_speaker(
            speaker,
            max_age_seconds=TEMP_BUFFER_MAX_AGE_SECONDS,
        )
    except Exception:
        return ""

    pending = sorted(pending, key=lambda entry: entry.created_at, reverse=True)
    return "\n".join(f"[PENDING - not yet indexed] {entry.raw_text}" for entry in pending)


def retrieve(
    question: str,
    speaker: str,
    top_k: int = TOP_K,
    image_base64: str | None = None,
    lanes_out: dict[str, list[str]] | None = None,
) -> str:
    """End-to-end: embed question -> find top-k episodes -> expand subgraphs."""
    pending_context = _pending_context(speaker)
    episode_ids = find_relevant_episodes(
        question, speaker, top_k, image_base64=image_base64, lanes_out=lanes_out
    )
    expanded = expand_episodes(episode_ids, speaker)

    if pending_context and expanded:
        return (
            f"{MERGED_CONTEXT_CONFLICT_RULE}\n\n"
            f"{SHORT_TERM_MEMORY_HEADER}\n{pending_context}\n\n"
            f"{LONG_TERM_MEMORY_HEADER}\n{expanded}"
        )
    if pending_context:
        return f"{SHORT_TERM_MEMORY_HEADER}\n{pending_context}"
    if expanded:
        return f"{LONG_TERM_MEMORY_HEADER}\n{expanded}"
    return ""


# A speaker who owns photos keeps owning them, so a positive answer is cached for
# the life of the process; only clear_memory can falsify it, and that path clears
# the cache explicitly. A negative expires quickly, because it stops being true
# the moment someone's first photo finishes indexing.
_IMAGE_PROBE_NEGATIVE_TTL_SECONDS = 30.0
_image_probe_cache: dict[str, tuple[float, bool]] = {}
_image_probe_lock = threading.Lock()


def forget_image_probe(speaker: str | None = None) -> None:
    """Drop the cached image-existence answer (all speakers when None).

    Called by the delete path: clear_memory is the one operation that can turn a
    cached True back into a False.
    """
    with _image_probe_lock:
        if speaker is None:
            _image_probe_cache.clear()
        else:
            _image_probe_cache.pop(speaker, None)


def _speaker_has_image_memories(speaker: str, _now=time.monotonic) -> bool:
    """Cheap existence probe: does this speaker have any photo memories at all?

    Gates the image lane during ordinary chat. Most queries come from tenants
    with no photos, and this one indexed lookup is far cheaper than the
    multimodal embedding call it avoids — but it ran on *every* query, so the
    answer is cached (asymmetrically, see above).
    """
    from threelane_memory.database import run_query

    now = _now()
    with _image_probe_lock:
        hit = _image_probe_cache.get(speaker)
        if hit is not None:
            cached_at, value = hit
            if value or now - cached_at < _IMAGE_PROBE_NEGATIVE_TTL_SECONDS:
                return value

    try:
        rows = run_query(
            "MATCH (ep:Episode {speaker:$speaker}) "
            "WHERE ep.image_embedding IS NOT NULL AND ep.consolidated_into IS NULL "
            "RETURN ep.id AS id LIMIT 1",
            {"speaker": speaker},
        )
    except Exception:
        # Deliberately not cached: a transient failure is not evidence of absence.
        return False

    found = bool(rows)
    with _image_probe_lock:
        if len(_image_probe_cache) > 512:
            _image_probe_cache.clear()
        _image_probe_cache[speaker] = (now, found)
    return found


def image_lane_scores(
    speaker: str,
    *,
    query_text: str | None = None,
    image_base64: str | None = None,
    top_k: int = TOP_K,
    require_margin: bool = True,
) -> list[tuple[str, float]]:
    """Return (episode_id, similarity) pairs from the image vector index.

    The query is embedded into the shared multimodal space — from *query_text*
    ("beach photos") or *image_base64* ("photos like this one") — and matched
    against the ``image_embedding`` index, post-filtered to *speaker*.

    Qualification is by a natural BREAK in the ranking, not by an absolute floor
    — see ``IMAGE_LANE_MIN_GAP`` in config.py for the measurements that forced
    this. A real match stands clear of the next-best photo; a question with
    nothing to match leaves an almost flat ranking. Admitting the group above
    the break also handles genuine clusters (two beach photos both qualify).

    *require_margin=False* skips the gate for explicit photo search, where the
    user has asked for pictures and the best available match IS the answer.
    """
    from threelane_memory.config import (
        IMAGE_LANE_MAX_GROUP,
        IMAGE_LANE_MIN_GAP,
        IMAGE_LANE_MIN_SAMPLE,
    )
    from threelane_memory.database import ensure_image_embedding_index, run_query
    from threelane_memory.multimodal import embed_image_b64, embed_text, is_configured

    if not is_configured():
        return []
    if image_base64:
        q_vec = embed_image_b64(image_base64)
    elif query_text and query_text.strip():
        q_vec = embed_text(query_text)
    else:
        return []

    if not ensure_image_embedding_index():
        return []
    # Pull a wider sample than we return: the margin is computed against the
    # speaker's own score distribution, so the sample IS the discriminator.
    sample_size = max(top_k * 2, IMAGE_LANE_MIN_SAMPLE * 2)
    try:
        rows = run_query(
            "CALL db.index.vector.queryNodes('image_embedding', $k, $vec) "
            "YIELD node, score "
            "WHERE node.speaker = $speaker AND node.consolidated_into IS NULL "
            "RETURN node.id AS id, score AS score ORDER BY score DESC LIMIT $limit",
            {
                "k": max(top_k * 4, 20),
                "vec": q_vec,
                "speaker": speaker,
                "limit": sample_size,
            },
        )
    except Exception as exc:
        logger.debug("Image memory search failed: %s", exc)
        return []

    scored = [(r["id"], float(r["score"])) for r in rows if r.get("score") is not None]
    if not scored:
        return []
    if not require_margin:
        return scored[:top_k]
    if len(scored) < IMAGE_LANE_MIN_SAMPLE:
        return []  # too few photos to tell a match from the nearest thing

    # Walk down the ranking for the first clear break. Taking the FIRST one
    # keeps the admitted group tight: a later, larger drop usually just marks
    # where this speaker's photos stop resembling the query at all.
    for cut in range(1, min(IMAGE_LANE_MAX_GROUP, len(scored) - 1) + 1):
        if scored[cut - 1][1] - scored[cut][1] >= IMAGE_LANE_MIN_GAP:
            return scored[:cut][:top_k]
    return []


def fetch_episode_images(
    episode_ids: list[str], speaker: str, limit: int
) -> list[tuple[str, str]]:
    """Re-read the retained originals for *episode_ids* as (base64, media_type).

    Returns fewer than requested — or nothing — when photos were never retained
    or have aged out of the retention window. That is the normal, expected case:
    the description and the embedding outlive the bytes by design, so the caller
    must be able to fall back to a text-only answer.
    """
    from threelane_memory import image_store

    if not episode_ids or not image_store.is_configured():
        return []
    from threelane_memory.database import run_query

    try:
        rows = run_query(
            "UNWIND $ids AS eid MATCH (ep:Episode {id:eid, speaker:$speaker}) "
            "WHERE ep.image_key IS NOT NULL RETURN ep.id AS id, ep.image_key AS key",
            {"ids": episode_ids, "speaker": speaker},
        )
    except Exception as exc:
        logger.debug("Could not look up image keys: %s", exc)
        return []

    order = {eid: i for i, eid in enumerate(episode_ids)}
    rows.sort(key=lambda r: order.get(r["id"], len(order)))

    images: list[tuple[str, str]] = []
    for row in rows[:limit]:
        fetched = image_store.get_image(row["key"], speaker)
        if fetched:
            images.append(fetched)
    return images


def find_image_memories(
    speaker: str,
    *,
    query_text: str | None = None,
    image_base64: str | None = None,
    top_k: int = TOP_K,
) -> list[str]:
    """Episode IDs whose image vector is nearest the query (explicit search).

    Kept as the direct-search entry point (``search_image_memories``), where the
    user has *asked* for photos and the similarity floor should not apply — a
    weak best match is still the answer to "show me photos like this".
    """
    from threelane_memory.database import ensure_image_embedding_index, run_query
    from threelane_memory.multimodal import embed_image_b64, embed_text, is_configured

    if not is_configured():
        return []
    if image_base64:
        q_vec = embed_image_b64(image_base64)
    elif query_text and query_text.strip():
        q_vec = embed_text(query_text)
    else:
        return []

    if not ensure_image_embedding_index():
        return []
    try:
        rows = run_query(
            "CALL db.index.vector.queryNodes('image_embedding', $k, $vec) "
            "YIELD node, score "
            "WHERE node.speaker = $speaker AND node.consolidated_into IS NULL "
            "RETURN node.id AS id ORDER BY score DESC LIMIT $limit",
            {"k": max(top_k * 4, 20), "vec": q_vec, "speaker": speaker, "limit": top_k},
        )
    except Exception as exc:
        logger.debug("Image memory search failed: %s", exc)
        return []
    return [r["id"] for r in rows]


def retrieve_image_memories(
    speaker: str,
    *,
    query_text: str | None = None,
    image_base64: str | None = None,
    top_k: int = TOP_K,
) -> str:
    """End-to-end image search → formatted context for the matched episodes."""
    episode_ids = find_image_memories(
        speaker, query_text=query_text, image_base64=image_base64, top_k=top_k
    )
    expanded = expand_episodes(episode_ids, speaker)
    if expanded:
        return f"{LONG_TERM_MEMORY_HEADER}\n{expanded}"
    return ""


async def retrieve_multi(
    questions: list[str],
    speaker: str,
    top_k: int = 5,
    image_base64: str | None = None,
) -> str:
    """Run multiple retrievals in parallel and merge context.

    An attached photo is passed to the FIRST sub-query only. The image lane
    embeds the picture itself, so the answer it returns does not depend on which
    sub-query it rides along with, and running it once per sub-query would pay
    the vision cost two or three times over for the same result.

    Without this parameter the multi-query path was text-only, which is worse
    than it sounds: a caller that attached a photo and asked about two things
    would silently lose the image lane by virtue of having asked about two
    things, and be told its own photograph was never mentioned.
    """
    # Run all retrievals in parallel
    tasks = [
        asyncio.to_thread(
            find_relevant_episodes, q, speaker, top_k, image_base64 if i == 0 else None
        )
        for i, q in enumerate(questions)
    ]
    results = await asyncio.gather(*tasks)

    # Flatten and deduplicate episode IDs while preserving some order
    seen = set()
    merged_ids = []
    for episode_ids in results:
        for eid in episode_ids:
            if eid not in seen:
                seen.add(eid)
                merged_ids.append(eid)

    # Expand the merged set (expand_episodes handles sibling expansion and formatting)
    expanded = expand_episodes(merged_ids, speaker)
    pending_context = _pending_context(speaker)

    if pending_context and expanded:
        return (
            f"{MERGED_CONTEXT_CONFLICT_RULE}\n\n"
            f"{SHORT_TERM_MEMORY_HEADER}\n{pending_context}\n\n"
            f"{LONG_TERM_MEMORY_HEADER}\n{expanded}"
        )
    if pending_context:
        return f"{SHORT_TERM_MEMORY_HEADER}\n{pending_context}"
    if expanded:
        return f"{LONG_TERM_MEMORY_HEADER}\n{expanded}"
    return ""
