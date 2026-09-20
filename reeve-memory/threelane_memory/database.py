"""Neo4j driver and query helper."""

from __future__ import annotations

import logging
import hashlib
import hmac
import secrets
import sys
from typing import Any

from neo4j import GraphDatabase

from threelane_memory.config import (
    API_KEY_HASH_SECRET,
    EMBEDDING_DIM,
    EMBEDDING_PROVIDER,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USER,
)

# Suppress Neo4j property-not-exist warnings for new properties on old nodes
logging.getLogger("neo4j").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

driver = GraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USER, NEO4J_PASSWORD),
    notifications_min_severity="OFF",
)


def run_query(query: str, params: dict | None = None) -> list[dict[str, Any]]:
    """Execute a Cypher query and return the result rows as dicts."""
    with driver.session() as session:
        result = session.run(query, params or {})
        return result.data()


def close():
    """Shut down the Neo4j driver cleanly."""
    driver.close()


def ensure_entity_vector_index() -> bool:
    """Ensure the Entity vector index exists for fast similarity merging."""
    try:
        rows = run_query("SHOW INDEXES")
        exists = any(r.get("name") == "entity_embedding" for r in rows)
        if not exists:
            run_query(
                f"""
                CREATE VECTOR INDEX entity_embedding IF NOT EXISTS
                FOR (e:Entity) ON (e.embedding)
                OPTIONS {{indexConfig: {{
                  `vector.dimensions`: {EMBEDDING_DIM},
                  `vector.similarity_function`: 'cosine'
                }}}}
                """
            )
        return True
    except Exception as exc:
        logger.warning("Entity vector index creation failed: %s", exc)
        return False


_image_embedding_index_checked = False


def ensure_image_embedding_index() -> bool:
    """Ensure the vector index for multimodal image embeddings exists.

    Separate from ``episode_embedding`` (text) because image vectors live in a
    different space and must not be searched together with text vectors.
    """
    global _image_embedding_index_checked
    if _image_embedding_index_checked:
        return True
    try:
        from threelane_memory.config import MULTIMODAL_EMBED_DIM

        run_query(
            f"""
            CREATE VECTOR INDEX image_embedding IF NOT EXISTS
            FOR (ep:Episode) ON (ep.image_embedding)
            OPTIONS {{indexConfig: {{
              `vector.dimensions`: {MULTIMODAL_EMBED_DIM},
              `vector.similarity_function`: 'cosine'
            }}}}
            """
        )
        _image_embedding_index_checked = True
        return True
    except Exception as exc:
        logger.warning("Image embedding index creation failed: %s", exc)
        return False


_episode_fulltext_index_checked = False


def ensure_episode_fulltext_index(quiet: bool = False) -> bool:
    """Ensure the Episode full-text index used by hybrid retrieval exists."""
    global _episode_fulltext_index_checked

    if _episode_fulltext_index_checked:
        return True

    try:
        rows = run_query("SHOW INDEXES YIELD name WHERE name = 'episode_raw_text' RETURN name")
        if not rows:
            run_query(
                "CREATE FULLTEXT INDEX episode_raw_text IF NOT EXISTS "
                "FOR (ep:Episode) ON EACH [ep.raw_text, ep.summary, ep.searchable_text]"
            )
        _episode_fulltext_index_checked = True
        return True
    except Exception as exc:
        if not quiet:
            logger.warning("Episode full-text index is unavailable: %s", exc)
        return False


_location_point_index_checked = False


def ensure_location_point_index() -> bool:
    """Ensure the point index used for spatial Location queries exists."""
    global _location_point_index_checked

    if _location_point_index_checked:
        return True

    try:
        run_query(
            "CREATE POINT INDEX location_point_index IF NOT EXISTS "
            "FOR (loc:Location) ON (loc.location_point)"
        )
        _location_point_index_checked = True
        return True
    except Exception as exc:
        logger.warning("Location point index creation failed: %s", exc)
        return False


# ── User Management ──────────────────────────────────────────────────────────


def _hash_api_key(api_key: str) -> str:
    """Return the HMAC digest stored for permanent API keys."""
    return hmac.new(
        API_KEY_HASH_SECRET.encode("utf-8"),
        api_key.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _generate_api_key() -> str:
    return f"sk-{secrets.token_urlsafe(32)}"


def upsert_user(uid: str, email: str | None = None, plan: str | None = None) -> None:
    """Create or update a user node without issuing or returning an API key."""
    query = (
        "MERGE (u:User {uid: $uid}) "
        "SET u.email = coalesce($email, u.email), "
        "    u.plan = coalesce($plan, u.plan, 'free'), "
        "    u.updated_at = timestamp()"
    )
    run_query(query, {"uid": uid, "email": email, "plan": plan})


def issue_user_api_key(uid: str, email: str | None = None, plan: str | None = None) -> str:
    """Create or rotate a user's permanent API key and store only its HMAC digest."""
    api_key = _generate_api_key()
    api_key_hash = _hash_api_key(api_key)
    query = (
        "MERGE (u:User {uid: $uid}) "
        "SET u.email = coalesce($email, u.email), "
        "    u.plan = coalesce($plan, u.plan, 'free'), "
        "    u.api_key_hash = $api_key_hash, "
        "    u.api_key_created_at = timestamp(), "
        "    u.updated_at = timestamp() "
        "REMOVE u.api_key "
        "RETURN u.uid AS uid"
    )
    run_query(query, {"uid": uid, "email": email, "plan": plan, "api_key_hash": api_key_hash})
    return api_key


def revoke_user_api_key(uid: str) -> None:
    """Remove a user's permanent API key material."""
    run_query(
        """
        MATCH (u:User {uid: $uid})
        REMOVE u.api_key_hash, u.api_key
        SET u.api_key_revoked_at = timestamp(),
            u.updated_at = timestamp()
        """,
        {"uid": uid},
    )


def migrate_plaintext_api_keys() -> int:
    """Hash and remove legacy plaintext API keys. Returns migrated user count."""
    rows = run_query(
        """
        MATCH (u:User)
        WHERE u.api_key IS NOT NULL
        RETURN u.uid AS uid, u.api_key AS api_key
        """
    )
    migrated = 0
    for row in rows:
        uid = row.get("uid")
        api_key = row.get("api_key")
        if not uid or not isinstance(api_key, str):
            continue
        run_query(
            """
            MATCH (u:User {uid: $uid})
            SET u.api_key_hash = coalesce(u.api_key_hash, $api_key_hash),
                u.api_key_migrated_at = timestamp()
            REMOVE u.api_key
            """,
            {"uid": uid, "api_key_hash": _hash_api_key(api_key)},
        )
        migrated += 1
    return migrated


def user_has_api_key(uid: str) -> bool:
    """Return whether a user has either a hashed or legacy plaintext API key."""
    rows = run_query(
        """
        MATCH (u:User {uid: $uid})
        RETURN u.api_key_hash IS NOT NULL OR u.api_key IS NOT NULL AS has_api_key
        """,
        {"uid": uid},
    )
    return bool(rows and rows[0].get("has_api_key"))


def get_user_by_api_key(api_key: str) -> dict[str, Any] | None:
    """Find a user by permanent API key hash."""
    api_key_hash = _hash_api_key(api_key)
    rows = run_query(
        """
        MATCH (u:User {api_key_hash: $api_key_hash})
        RETURN u.uid AS uid, coalesce(u.plan, 'free') AS plan
        """,
        {"api_key_hash": api_key_hash},
    )
    return rows[0] if rows else None


def get_user_plan(uid: str) -> str:
    """Return the user's effective plan, defaulting to 'free'.

    Lazy subscription expiry: a paid plan whose ``plan_expires_at`` (set ~1 cycle
    ahead on each confirmed charge) has passed without a renewal charge is
    downgraded to 'free' here and persisted — and its usage cycle is reverted to
    calendar-month. This enforces "next cycle without payment -> free" even if
    Razorpay's halt/cancel webhook is delayed or missed. Plans with no
    ``plan_expires_at`` (never charged / grandfathered) are left untouched.
    """
    rows = run_query(
        "MATCH (u:User {uid: $uid}) RETURN u.plan AS plan, "
        "(u.plan_expires_at IS NOT NULL AND datetime() > u.plan_expires_at) AS expired",
        {"uid": uid},
    )
    if not rows or not rows[0].get("plan"):
        return "free"
    plan = str(rows[0]["plan"])
    if plan != "free" and rows[0].get("expired"):
        run_query(
            "MATCH (u:User {uid: $uid}) SET u.plan = 'free' "
            "REMOVE u.usage_period, u.plan_expires_at",
            {"uid": uid},
        )
        return "free"
    return plan


def advance_usage_period(uid: str, *, days_valid: int = 31) -> None:
    """Start a fresh billing cycle for *uid* on a confirmed charge.

    Advances ``usage_period`` (the key the monthly quota counters use, so both the
    query and token counters reset to 0 for the new cycle) and extends
    ``plan_expires_at`` to ~1 cycle ahead. Anchored to the charge date, so the
    cycle follows the subscription, not the calendar.
    """
    run_query(
        "MATCH (u:User {uid: $uid}) "
        "SET u.usage_period = toString(datetime()), "
        "    u.plan_expires_at = datetime() + duration({days: $days})",
        {"uid": uid, "days": days_valid},
    )


def clear_usage_period(uid: str) -> None:
    """Revert *uid* to the calendar-month usage cycle (used on downgrade to free)."""
    run_query(
        "MATCH (u:User {uid: $uid}) REMOVE u.usage_period, u.plan_expires_at",
        {"uid": uid},
    )


_monthly_usage_constraint_ready = False


def _current_month_key() -> str:
    """UTC 'YYYY-MM' bucket; a new string each calendar month resets usage."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m")


def _ensure_monthly_usage_constraint() -> None:
    global _monthly_usage_constraint_ready
    if _monthly_usage_constraint_ready:
        return
    try:
        # Uniqueness makes MERGE on (uid, month) safe under concurrency: without
        # it, two racing MERGEs could each create a duplicate counter node.
        run_query(
            "CREATE CONSTRAINT monthly_usage_uid_month IF NOT EXISTS "
            "FOR (mu:MonthlyUsage) REQUIRE (mu.uid, mu.month) IS UNIQUE"
        )
    except Exception:  # pragma: no cover - best effort
        pass
    _monthly_usage_constraint_ready = True


def reserve_monthly_query(uid: str) -> int:
    """Atomically count one query-serving call and return the running month total.

    The MERGE + SET increment runs as a single write against a node kept unique
    by (uid, month), so concurrent callers serialize on that node's lock — this
    closes the check-then-act race a read-then-compare counter would leave open.
    A rejected/failed call still consumes a slot (the increment precedes the
    quota check); that is the conservative, standard cost of atomic reservation.
    """
    _ensure_monthly_usage_constraint()
    rows = run_query(
        """
        MATCH (u:User {uid: $uid})
        WITH coalesce(u.usage_period, $month) AS period
        MERGE (mu:MonthlyUsage {uid: $uid, month: period})
        ON CREATE SET mu.count = 0
        SET mu.count = mu.count + 1
        RETURN mu.count AS count
        """,
        {"uid": uid, "month": _current_month_key()},
    )
    return int(rows[0]["count"]) if rows else 1


def count_monthly_queries(uid: str) -> int:
    """Read this cycle's query-serving count for *uid* (display only; the quota
    gate uses ``reserve_monthly_query`` for atomic enforcement)."""
    rows = run_query(
        """
        MATCH (u:User {uid: $uid})
        WITH coalesce(u.usage_period, $month) AS period
        OPTIONAL MATCH (mu:MonthlyUsage {uid: $uid, month: period})
        RETURN coalesce(mu.count, 0) AS c
        """,
        {"uid": uid, "month": _current_month_key()},
    )
    return int(rows[0]["c"]) if rows and rows[0].get("c") is not None else 0


def add_monthly_tokens(uid: str, tokens: int) -> None:
    """Atomically add *tokens* to this UTC month's running total for *uid*.

    Tokens are known only after an operation runs, so this is called at log time
    (not reserved up front). Shares the MonthlyUsage node with the query counter.
    """
    if not uid or tokens <= 0:
        return
    _ensure_monthly_usage_constraint()
    run_query(
        """
        MATCH (u:User {uid: $uid})
        WITH coalesce(u.usage_period, $month) AS period
        MERGE (mu:MonthlyUsage {uid: $uid, month: period})
        ON CREATE SET mu.count = 0
        SET mu.tokens = coalesce(mu.tokens, 0) + $tokens
        """,
        {"uid": uid, "month": _current_month_key(), "tokens": int(tokens)},
    )


def get_monthly_tokens(uid: str) -> int:
    """This cycle's total token spend for *uid* (in+out, all operations)."""
    rows = run_query(
        """
        MATCH (u:User {uid: $uid})
        WITH coalesce(u.usage_period, $month) AS period
        OPTIONAL MATCH (mu:MonthlyUsage {uid: $uid, month: period})
        RETURN coalesce(mu.tokens, 0) AS t
        """,
        {"uid": uid, "month": _current_month_key()},
    )
    return int(rows[0]["t"]) if rows and rows[0].get("t") is not None else 0


# ── Vector index dimension check ─────────────────────────────────────────────


def get_index_dimension() -> int | None:
    """Return the dimension of the ``episode_embedding`` vector index, or None."""
    try:
        rows = run_query(
            "SHOW INDEXES YIELD name, type, options "
            "WHERE name = 'episode_embedding' AND type = 'VECTOR' "
            "RETURN options"
        )
        if rows:
            opts = rows[0].get("options", {})
            cfg = opts.get("indexConfig", {})
            dim = cfg.get("vector.dimensions")
            return int(dim) if dim is not None else None
    except Exception:
        return None
    return None


def check_index_dimension(quiet: bool = False) -> bool:
    """Compare Neo4j index dimension with configured EMBEDDING_DIM.

    Returns True if they match (or if the index doesn't exist yet).
    Prints a warning to stderr on mismatch unless *quiet* is True.
    """
    index_dim = get_index_dimension()
    if index_dim is None:
        # Index doesn't exist yet — will be created on first store
        return True
    if index_dim != EMBEDDING_DIM:
        if not quiet:
            print(
                f"\n⚠  DIMENSION MISMATCH: Neo4j vector index is {index_dim}-dim "
                f"but your {EMBEDDING_PROVIDER} embedding model expects {EMBEDDING_DIM}-dim.\n"
                f"   Queries will fail or return bad results until you fix this.\n"
                f"\n"
                f"   To fix, run these steps:\n"
                f"   1. Drop the old index in Neo4j Browser:\n"
                f"        DROP INDEX episode_embedding;\n"
                f"   2. Create a new index with the correct dimension:\n"
                f"        CREATE VECTOR INDEX episode_embedding IF NOT EXISTS\n"
                f"        FOR (ep:Episode) ON (ep.embedding)\n"
                f"        OPTIONS {{indexConfig: {{\n"
                f"          `vector.dimensions`: {EMBEDDING_DIM},\n"
                f"          `vector.similarity_function`: 'cosine'\n"
                f"        }}}};\n"
                f"   3. Re-embed all episodes:\n"
                f"        threelane-memory reindex --old-model <previous-model>\n",
                file=sys.stderr,
            )
        return False
    return True


# ── OAuth 2.1 Management ─────────────────────────────────────────────────────


def create_oauth_client(client_name: str, redirect_uris: list[str]) -> str:
    """Register a new OAuth client and return its client_id."""
    client_id = secrets.token_urlsafe(24)
    query = (
        "CREATE (c:OAuthClient {client_id: $client_id, client_name: $client_name, "
        "redirect_uris: $redirect_uris, created_at: timestamp()}) "
        "RETURN c.client_id AS client_id"
    )
    rows = run_query(
        query,
        {"client_id": client_id, "client_name": client_name, "redirect_uris": redirect_uris},
    )
    if rows:
        stored_id = rows[0].get("client_id")
        if stored_id is not None:
            return str(stored_id)
    return client_id


def get_oauth_client(client_id: str) -> dict[str, Any] | None:
    """Retrieve an OAuth client by its client_id."""
    query = "MATCH (c:OAuthClient {client_id: $client_id}) RETURN properties(c) AS props"
    rows = run_query(query, {"client_id": client_id})
    if not rows:
        return None
    props = rows[0].get("props")
    if isinstance(props, dict):
        return props
    return None


def create_auth_code(
    code: str, client_id: str, user_id: str, redirect_uri: str, code_challenge: str
) -> None:
    """Store an authorization code with its associated metadata and PKCE challenge."""
    query = (
        "CREATE (a:AuthCode {code: $code, client_id: $client_id, user_id: $user_id, "
        "redirect_uri: $redirect_uri, code_challenge: $code_challenge, "
        "created_at: timestamp()})"
    )
    run_query(
        query,
        {
            "code": code,
            "client_id": client_id,
            "user_id": user_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
        },
    )


def consume_auth_code(code: str) -> dict[str, Any] | None:
    """Retrieve and delete an authorization code if it's not expired (10 min)."""
    import time

    # Atomically get and delete
    query = "MATCH (a:AuthCode {code: $code}) WITH a, properties(a) AS props DELETE a RETURN props"
    rows = run_query(query, {"code": code})
    if rows:
        props = rows[0].get("props")
        if not isinstance(props, dict):
            return None
        # Expire after 10 minutes (600,000 ms)
        if time.time() * 1000 - props["created_at"] > 600_000:
            return None
        return props
    return None


# ── Dashboard API queries ─────────────────────────────────────────────────────


def log_request(uid: str, operation: str, tokens_in: int, tokens_out: int, internal_id: str | None = None) -> None:
    """Write a RequestLog node for this user's MCP tool call.

    ``tokens_in`` already includes any embedding-model input tokens (embeddings
    are input-only), so total_tokens reflects overall spend — the same tokens
    the plan's monthly token allowance and top-up bill on.
    """
    run_query(
        """
        MATCH (u:User {uid: $uid})
        CREATE (rl:RequestLog {
            uid: $uid,
            operation: $operation,
            tokens_in: $tokens_in,
            tokens_out: $tokens_out,
            internal_id: $internal_id,
            timestamp: datetime(),
            total_tokens: $tokens_in + $tokens_out
        })
        CREATE (u)-[:HAS_LOG]->(rl)
        """,
        {
            "uid": uid,
            "operation": operation,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "internal_id": internal_id,
        },
    )
    # Meter tokens toward the monthly token quota. Async stores log 0/0 here and
    # the real total is added later in update_log_tokens (so no double-count).
    add_monthly_tokens(uid, (tokens_in or 0) + (tokens_out or 0))


def update_log_tokens(internal_id: str, tokens_in: int, tokens_out: int) -> None:
    """Update an existing RequestLog with actual token counts after async processing.

    ``tokens_in`` is expected to already include the embedding input tokens.
    """
    rows = run_query(
        """
        MATCH (rl:RequestLog {internal_id: $internal_id})
        SET rl.tokens_in = $tokens_in,
            rl.tokens_out = $tokens_out,
            rl.total_tokens = $tokens_in + $tokens_out,
            rl.async_completed_at = datetime()
        RETURN rl.uid AS uid
        """,
        {
            "internal_id": internal_id,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        },
    )
    # The initial async log added 0 tokens; add the real total now.
    if rows and rows[0].get("uid"):
        add_monthly_tokens(rows[0]["uid"], (tokens_in or 0) + (tokens_out or 0))


def get_dashboard_stats(uid: str) -> dict:
    """Return episode count, entity count, and token totals for a user."""
    rows = run_query(
        """
        CALL {
            MATCH (ep:Episode {speaker: $uid})
            WHERE ep.consolidated_into IS NULL
            RETURN count(ep) AS episode_count
        }
        CALL {
            MATCH (ep:Episode {speaker: $uid})-[:INVOLVES]->(e:Entity)
            WHERE ep.consolidated_into IS NULL
            RETURN count(DISTINCT e) AS entity_count
        }
        CALL {
            MATCH (rl:RequestLog {uid: $uid})
            RETURN coalesce(sum(rl.tokens_in), 0)  AS tokens_in_total,
                   coalesce(sum(rl.tokens_out), 0) AS tokens_out_total,
                   count(rl)                        AS total_requests
        }
        RETURN episode_count, entity_count, tokens_in_total, tokens_out_total, total_requests
        """,
        {"uid": uid},
    )
    r = rows[0] if rows else {}
    # Current-cycle usage vs the plan caps (what the dashboard should show as
    # "this month", resetting per the user's billing cycle — not lifetime).
    from threelane_memory.config import (
        DEFAULT_PLAN,
        PLAN_MONTHLY_QUERY_QUOTAS,
        PLAN_MONTHLY_TOKEN_QUOTAS,
    )

    plan = get_user_plan(uid)
    return {
        "episode_count": r.get("episode_count", 0),
        "entity_count": r.get("entity_count", 0),
        "tokens_in_total": r.get("tokens_in_total", 0),
        "tokens_out_total": r.get("tokens_out_total", 0),
        "total_requests": r.get("total_requests", 0),
        "plan": plan,
        "monthly_tokens": get_monthly_tokens(uid),
        "token_quota": PLAN_MONTHLY_TOKEN_QUOTAS.get(plan, PLAN_MONTHLY_TOKEN_QUOTAS[DEFAULT_PLAN]),
        "monthly_queries": count_monthly_queries(uid),
        "query_quota": PLAN_MONTHLY_QUERY_QUOTAS.get(plan, PLAN_MONTHLY_QUERY_QUOTAS[DEFAULT_PLAN]),
    }


def get_dashboard_logs(uid: str, limit: int = 50) -> list[dict]:
    """Return recent request logs for a user."""
    rows = run_query(
        """
        MATCH (rl:RequestLog {uid: $uid})
        RETURN rl.operation    AS operation,
               rl.tokens_in   AS tokens_in,
               rl.tokens_out  AS tokens_out,
               coalesce(rl.total_tokens, rl.tokens_in + rl.tokens_out, 0) AS total_tokens,
               toString(rl.timestamp) AS timestamp
        ORDER BY rl.timestamp DESC
        LIMIT $limit
        """,
        {"uid": uid, "limit": limit},
    )
    return [
        {
            "operation": r.get("operation", ""),
            "tokens_in": r.get("tokens_in", 0) or 0,
            "tokens_out": r.get("tokens_out", 0) or 0,
            "total_tokens": r.get("total_tokens", 0) or 0,
            "timestamp": r.get("timestamp", ""),
        }
        for r in rows
    ]


def get_dashboard_graph(uid: str) -> dict:
    """Return nodes and edges for memory graph visualization."""
    episode_rows = run_query(
        """
        MATCH (ep:Episode {speaker: $uid})
        WHERE ep.consolidated_into IS NULL
        RETURN ep.id        AS id,
               ep.summary   AS summary,
               ep.importance AS importance,
               toString(ep.timestamp) AS timestamp,
               ep.emotion   AS emotion
        ORDER BY ep.timestamp DESC
        LIMIT 100
        """,
        {"uid": uid},
    )

    entity_rows = run_query(
        """
        MATCH (ep:Episode {speaker: $uid})-[:INVOLVES]->(e:Entity)
        WHERE ep.consolidated_into IS NULL
          AND NOT (e)-[:ALIAS_OF]->()
        RETURN DISTINCT e.name AS name
        LIMIT 100
        """,
        {"uid": uid},
    )

    edge_rows = run_query(
        """
        MATCH (ep:Episode {speaker: $uid})-[:INVOLVES]->(e:Entity)
        WHERE ep.consolidated_into IS NULL
          AND NOT (e)-[:ALIAS_OF]->()
        RETURN ep.id AS episode_id, e.name AS entity_name
        LIMIT 300
        """,
        {"uid": uid},
    )

    nodes = []
    for r in episode_rows:
        nodes.append({
            "id": r["id"],
            "type": "Episode",
            "label": r.get("summary") or "",
            "importance": r.get("importance", 0.5),
            "timestamp": r.get("timestamp", ""),
            "emotion": r.get("emotion", ""),
        })
    for r in entity_rows:
        nodes.append({
            "id": f"entity_{r['name']}",
            "type": "Entity",
            "label": r["name"],
            "importance": 0.5,
            "timestamp": "",
            "emotion": "",
        })

    edges = []
    for r in edge_rows:
        edges.append({
            "source": r["episode_id"],
            "target": f"entity_{r['entity_name']}",
            "type": "INVOLVES",
        })

    return {"nodes": nodes, "edges": edges}


def get_dashboard_usage(uid: str, days: int = 30) -> dict:
    """Return daily token usage breakdown for the last N days."""
    rows = run_query(
        """
        MATCH (rl:RequestLog {uid: $uid})
        WHERE rl.timestamp >= datetime() - duration({days: $days})
        RETURN toString(date(rl.timestamp)) AS date,
               sum(rl.tokens_in)            AS tokens_in,
               sum(rl.tokens_out)           AS tokens_out,
               count(rl)                    AS requests
        ORDER BY date DESC
        """,
        {"uid": uid, "days": days},
    )
    daily = []
    for r in rows:
        daily.append({
            "date": r.get("date", ""),
            "tokens_in": r.get("tokens_in", 0) or 0,
            "tokens_out": r.get("tokens_out", 0) or 0,
            "requests": r.get("requests", 0) or 0,
        })
    return {"daily": daily}


# ── Billing ────────────────────────────────────────────────────────────────

def update_user_plan(
    uid: str,
    plan: str,
    subscription_id: str = "",
    *,
    subscription_status: str | None = None,
    subscription_short_url: str | None = None,
) -> None:
    """Update user plan in Neo4j after successful payment."""
    run_query(
        """
        MATCH (u:User {uid: $uid})
        SET u.plan = $plan,
            u.subscription_id = $subscription_id,
            u.subscription_status = coalesce($subscription_status, u.subscription_status),
            u.subscription_short_url =
                CASE
                    WHEN $subscription_short_url IS NULL THEN u.subscription_short_url
                    ELSE $subscription_short_url
                END,
            u.plan_updated_at = datetime()
        """,
        {
            "uid": uid,
            "plan": plan,
            "subscription_id": subscription_id,
            "subscription_status": subscription_status,
            "subscription_short_url": subscription_short_url,
        },
    )


def set_user_subscription(
    uid: str,
    subscription_id: str,
    subscription_status: str,
    subscription_short_url: str | None = None,
) -> None:
    """Store billing subscription metadata without changing the user's plan."""
    run_query(
        """
        MATCH (u:User {uid: $uid})
        SET u.subscription_id = $subscription_id,
            u.subscription_status = $subscription_status,
            u.subscription_short_url =
                CASE
                    WHEN $subscription_short_url IS NULL THEN u.subscription_short_url
                    ELSE $subscription_short_url
                END,
            u.subscription_updated_at = datetime()
        """,
        {
            "uid": uid,
            "subscription_id": subscription_id,
            "subscription_status": subscription_status,
            "subscription_short_url": subscription_short_url,
        },
    )


def get_user_subscription(uid: str) -> dict:
    """Get user's current plan and subscription info."""
    rows = run_query(
        """
        MATCH (u:User {uid: $uid})
        RETURN u.plan AS plan,
               u.subscription_id AS subscription_id,
               u.subscription_status AS subscription_status,
               u.subscription_short_url AS subscription_short_url,
               toString(u.plan_updated_at) AS plan_updated_at
        """,
        {"uid": uid},
    )
    if not rows:
        return {"plan": "free", "subscription_id": None}
    r = rows[0]
    return {
        "plan": r.get("plan", "free") or "free",
        "subscription_id": r.get("subscription_id"),
        "subscription_status": r.get("subscription_status"),
        "subscription_short_url": r.get("subscription_short_url"),
        "plan_updated_at": r.get("plan_updated_at"),
    }


def get_usage_period_key(uid: str) -> str:
    """The MonthlyUsage bucket key this account's counters actually live under.

    Paid accounts are anchored to their subscription: ``advance_usage_period``
    stamps ``usage_period`` on every confirmed charge, so their cycle follows the
    billing date rather than the calendar. Everyone else falls back to the
    calendar month.

    Billing MUST resolve the key this way rather than assuming the calendar
    month. Reading the wrong bucket would make "has this cycle been billed?"
    answer no every time, and a redelivered webhook would charge the card again.
    """
    rows = run_query(
        "MATCH (u:User {uid: $uid}) RETURN coalesce(u.usage_period, $month) AS period",
        {"uid": uid, "month": _current_month_key()},
    )
    return str(rows[0]["period"]) if rows and rows[0].get("period") else _current_month_key()


def get_user_spend_cap(uid: str) -> int | None:
    """The account's monthly pay-as-you-go spend cap in paise.

    None means "never set", which the caller turns into the default — distinct
    from 0, which is a deliberate choice to run uncapped.
    """
    rows = run_query(
        "MATCH (u:User {uid: $uid}) RETURN u.spend_cap_paise AS cap", {"uid": uid}
    )
    if not rows or rows[0].get("cap") is None:
        return None
    return int(rows[0]["cap"])


def set_user_spend_cap(uid: str, paise: int) -> None:
    """Set the account's monthly pay-as-you-go spend cap (paise)."""
    run_query(
        "MERGE (u:User {uid: $uid}) SET u.spend_cap_paise = $paise, "
        "u.spend_cap_updated_at = datetime()",
        {"uid": uid, "paise": int(paise)},
    )


def get_billed_tokens(uid: str, month: str) -> int:
    """Tokens already invoiced for *month*, so a cycle is never billed twice.

    The add-on that charges a cycle is created once and recorded here. If the
    billing job runs again — a retry, an overlapping cron, a manual re-run — it
    sees the cycle is settled and does nothing.
    """
    rows = run_query(
        "MATCH (mu:MonthlyUsage {uid: $uid, month: $month}) "
        "RETURN coalesce(mu.billed_tokens, 0) AS t",
        {"uid": uid, "month": month},
    )
    return int(rows[0]["t"]) if rows and rows[0].get("t") is not None else 0


def mark_tokens_billed(uid: str, month: str, tokens: int, addon_id: str) -> None:
    """Record that *tokens* for *month* have been added to an invoice."""
    run_query(
        """
        MERGE (mu:MonthlyUsage {uid: $uid, month: $month})
        SET mu.billed_tokens = $tokens,
            mu.billed_at = datetime(),
            mu.billing_addon_id = $addon_id
        """,
        {"uid": uid, "month": month, "tokens": int(tokens), "addon_id": addon_id},
    )
