"""Graph schema migrations for threelane-memory.

Each migration is **idempotent** and **resumable**, gated by a
`(:SchemaMigration {name})` marker node.

``entity_speaker_v1`` converts globally name-keyed ``Entity`` nodes into
per-speaker ``(name, speaker)`` entities so that roles, states, actions and
relations isolate per tenant (closing the cross-tenant entity-layer leaks).

``location_speaker_v1`` does the same for ``Location`` nodes.  Global
name-keyed locations meant "first writer wins across namespaces": one user's
"City Palace" (Udaipur) silently supplied coordinates, region and place card
for every other user's "City Palace" (Jaipur), and generic names ("Home",
"Office") collided across every user on the account.

They run in two places:
  • ``reeve migrate`` CLI — the primary operator path (supports ``--dry-run``);
    run it against production *before* deploying the new code.
  • server startup — a safety net that applies the migration if the marker is
    missing (see mcp_server.run_server).

Design notes
------------
The re-pointing is **edge-centric**: for every episode-derived relationship the
owning speaker is read from the connected Episode and the edge is re-pointed to a
MERGE'd ``(name, speaker)`` copy of the entity.  This uniformly handles single-
and multi-speaker entities and is safe to re-run (already-correct edges are
skipped).  Roles have no episode provenance, so they are copied only for
single-speaker (attributable) originals and dropped for multi-speaker ones
(they regenerate on future ingestion).  Cross-tenant state supersession is
repaired.  Split originals are removed; true orphans (no episodes) are left.

Locations follow the same edge-centric split, with one refinement: enrichment
(coordinates, place card, vibe embedding) is copied onto the per-speaker copy
only for **single-speaker** originals — it was disambiguated by that very
speaker's footprint, so it is theirs.  **Contested** originals (episodes from
more than one speaker) are the actual collision victims: the first-writer
enrichment is right for at most one of them, so their copies are stamped bare
(``geo_status`` NULL) and re-enrich per speaker — lazily on next mention, or
eagerly via ``reeve geo-backfill``.
"""

from __future__ import annotations

import logging

from threelane_memory.database import run_query

logger = logging.getLogger(__name__)

ENTITY_SPEAKER_MIGRATION = "entity_speaker_v1"
LOCATION_SPEAKER_MIGRATION = "location_speaker_v1"
_BATCH = 500


# ── Marker helpers ─────────────────────────────────────────────────────────────


def _ensure_marker_constraint() -> None:
    """Single-property uniqueness on the marker (all Neo4j editions)."""
    try:
        run_query(
            "CREATE CONSTRAINT schema_migration_name IF NOT EXISTS "
            "FOR (m:SchemaMigration) REQUIRE m.name IS UNIQUE"
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("SchemaMigration constraint creation failed: %s", exc)


def is_applied(name: str) -> bool:
    rows = run_query(
        "MATCH (m:SchemaMigration {name:$name}) RETURN m.status AS status",
        {"name": name},
    )
    return bool(rows and rows[0].get("status") == "completed")


def _mark(name: str, status: str) -> None:
    run_query(
        "MERGE (m:SchemaMigration {name:$name}) "
        "SET m.status = $status, m.updated_at = datetime()",
        {"name": name, "status": status},
    )


def _count(query: str, params: dict | None = None) -> int:
    rows = run_query(query, params or {})
    if not rows:
        return 0
    value = next(iter(rows[0].values()))
    return int(value or 0)


# ── entity_speaker_v1 ───────────────────────────────────────────────────────────

# Episode-derived edges to re-point: (relationship, path from episode to entity).
# `x` is the node that owns the edge to the entity (the Episode itself for
# INVOLVES, otherwise the State/Action/Relation node).
_EDGE_REPOINTS: list[tuple[str, str, str]] = [
    ("INVOLVES", "(ep:Episode)-[rel:INVOLVES]->(e:Entity)", "ep"),
    ("OF_ENTITY", "(ep:Episode)-[:HAS_STATE]->(x)-[rel:OF_ENTITY]->(e:Entity)", "x"),
    ("BY_ENTITY", "(ep:Episode)-[:HAS_ACTION]->(x)-[rel:BY_ENTITY]->(e:Entity)", "x"),
    ("ON_ENTITY", "(ep:Episode)-[:HAS_ACTION]->(x)-[rel:ON_ENTITY]->(e:Entity)", "x"),
    ("FROM_ENTITY", "(ep:Episode)-[:HAS_RELATION]->(x)-[rel:FROM_ENTITY]->(e:Entity)", "x"),
    ("TO_ENTITY", "(ep:Episode)-[:HAS_RELATION]->(x)-[rel:TO_ENTITY]->(e:Entity)", "x"),
]

# Union of every (entity name, owning speaker) pair implied by episode edges.
_TARGET_PAIRS_UNION = " UNION ".join(
    f"MATCH {pattern} RETURN e.name AS name, ep.speaker AS sp"
    for _, pattern, _ in _EDGE_REPOINTS
)


def plan_entity_speaker_v1() -> dict:
    """Return counts of what the migration would change (dry-run)."""
    return {
        "migration": ENTITY_SPEAKER_MIGRATION,
        "already_applied": is_applied(ENTITY_SPEAKER_MIGRATION),
        "global_entities": _count(
            "MATCH (e:Entity) WHERE e.speaker IS NULL RETURN count(e)"
        ),
        "per_speaker_entities_target": _count(
            f"CALL {{ {_TARGET_PAIRS_UNION} }} RETURN count(*)"
        ),
        "roles_dropped_multi_speaker": _count(
            "MATCH (orig:Entity)-[:HAS_ROLE]->(:Role) WHERE orig.speaker IS NULL "
            "MATCH (ep:Episode)-[:INVOLVES]->(orig) "
            "WITH orig, count(DISTINCT ep.speaker) AS speakers WHERE speakers > 1 "
            "RETURN count(DISTINCT orig)"
        ),
        "cross_speaker_supersedes": _count(
            "MATCH (newS:State)-[:SUPERSEDES]->(oldS:State) "
            "MATCH (epNew:Episode)-[:HAS_STATE]->(newS) "
            "MATCH (epOld:Episode)-[:HAS_STATE]->(oldS) "
            "WHERE epNew.speaker <> epOld.speaker "
            "RETURN count(*)"
        ),
    }


def _repoint_edges() -> None:
    """Re-point every episode-derived entity edge to a per-speaker copy."""
    for rel_type, pattern, source in _EDGE_REPOINTS:
        # `ep`, `rel`, `e` are always bound; the intermediate `x` only when the
        # edge originates at a State/Action/Relation rather than the Episode.
        with_vars = "ep, rel, e" if source == "ep" else "ep, rel, e, x"
        run_query(
            f"MATCH {pattern} "
            "WHERE e.speaker IS NULL OR e.speaker <> ep.speaker "
            "CALL { "
            f"  WITH {with_vars} "
            "  MERGE (copy:Entity {name: e.name, speaker: ep.speaker}) "
            "    ON CREATE SET copy.embedding = e.embedding "
            f"  MERGE ({source})-[:{rel_type}]->(copy) "
            "  DELETE rel "
            f"}} IN TRANSACTIONS OF {_BATCH} ROWS"
        )


def _recreate_aliases() -> None:
    """Re-create ALIAS_OF between per-speaker copies (chains preserved)."""
    run_query(
        "MATCH (aliasOrig:Entity)-[:ALIAS_OF]->(canonOrig:Entity) "
        "WHERE aliasOrig.speaker IS NULL OR canonOrig.speaker IS NULL "
        "CALL { "
        "  WITH aliasOrig, canonOrig "
        "  MATCH (aliasCopy:Entity {name: aliasOrig.name}) WHERE aliasCopy.speaker IS NOT NULL "
        "  MATCH (canonCopy:Entity {name: canonOrig.name, speaker: aliasCopy.speaker}) "
        "  MERGE (aliasCopy)-[:ALIAS_OF]->(canonCopy) "
        f"}} IN TRANSACTIONS OF {_BATCH} ROWS"
    )


def _recreate_single_speaker_roles() -> None:
    """Copy HAS_ROLE onto the per-speaker copy, only for single-speaker originals.

    Multi-speaker originals are unattributable and their roles are dropped (they
    regenerate on future ingestion when re-extracted for the right speaker)."""
    run_query(
        "MATCH (orig:Entity)-[:HAS_ROLE]->(r:Role) WHERE orig.speaker IS NULL "
        "CALL { "
        "  WITH orig, r "
        "  MATCH (copy:Entity {name: orig.name}) WHERE copy.speaker IS NOT NULL "
        "  WITH r, collect(DISTINCT copy) AS copies WHERE size(copies) = 1 "
        "  UNWIND copies AS copy "
        "  MERGE (copy)-[:HAS_ROLE]->(r) "
        f"}} IN TRANSACTIONS OF {_BATCH} ROWS"
    )


def _repair_supersession() -> None:
    """Drop cross-tenant SUPERSEDES edges, then recompute `active` per
    (per-speaker entity, attribute): the latest state by created_at wins."""
    run_query(
        "MATCH (newS:State)-[sup:SUPERSEDES]->(oldS:State) "
        "CALL { "
        "  WITH newS, sup, oldS "
        "  MATCH (epNew:Episode)-[:HAS_STATE]->(newS) "
        "  MATCH (epOld:Episode)-[:HAS_STATE]->(oldS) "
        "  WITH sup, epNew, epOld WHERE epNew.speaker <> epOld.speaker "
        "  DELETE sup "
        f"}} IN TRANSACTIONS OF {_BATCH} ROWS"
    )
    run_query(
        "MATCH (e:Entity) WHERE e.speaker IS NOT NULL "
        "CALL { "
        "  WITH e "
        "  MATCH (e)<-[:OF_ENTITY]-(s:State) "
        "  WITH s.attribute AS attr, s ORDER BY s.created_at DESC "
        "  WITH attr, collect(s) AS states "
        "  WITH head(states) AS latest, tail(states) AS rest "
        "  SET latest.active = true "
        "  FOREACH (old IN rest | SET old.active = false) "
        f"}} IN TRANSACTIONS OF {_BATCH} ROWS"
    )


def _delete_split_originals() -> None:
    """Remove global (speaker-null) entities that now have a stamped twin.

    True orphans (no same-name stamped twin) are left untouched."""
    run_query(
        "MATCH (e:Entity) WHERE e.speaker IS NULL "
        "CALL { "
        "  WITH e "
        "  MATCH (twin:Entity {name: e.name}) WHERE twin.speaker IS NOT NULL "
        "  WITH e, count(twin) AS twins WHERE twins > 0 "
        "  DETACH DELETE e "
        f"}} IN TRANSACTIONS OF {_BATCH} ROWS"
    )


def _duplicate_entities() -> int:
    return _count(
        "MATCH (e:Entity) WHERE e.speaker IS NOT NULL "
        "WITH e.speaker AS sp, e.name AS nm, count(*) AS c WHERE c > 1 "
        "RETURN count(*)"
    )


def _create_entity_constraint() -> None:
    """Create the (speaker, name) uniqueness constraint, or a composite index."""
    try:
        run_query(
            "CREATE CONSTRAINT entity_name_speaker_unique IF NOT EXISTS "
            "FOR (e:Entity) REQUIRE (e.speaker, e.name) IS UNIQUE"
        )
        return
    except Exception as exc:
        logger.warning(
            "Entity (speaker,name) uniqueness constraint unavailable (%s); "
            "falling back to a composite index",
            exc,
        )
    try:
        run_query(
            "CREATE INDEX entity_name_speaker IF NOT EXISTS "
            "FOR (e:Entity) ON (e.speaker, e.name)"
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Entity composite index creation failed: %s", exc)


def apply_entity_speaker_v1() -> dict:
    """Apply the migration. Idempotent and resumable.

    Raises on a hard failure so the marker is not set and the run is retried
    (each phase is safe to re-run). Returns a summary dict.
    """
    if is_applied(ENTITY_SPEAKER_MIGRATION):
        return {"migration": ENTITY_SPEAKER_MIGRATION, "status": "already_applied"}

    logger.info("Applying migration %s …", ENTITY_SPEAKER_MIGRATION)
    _mark(ENTITY_SPEAKER_MIGRATION, "in_progress")

    _repoint_edges()
    _recreate_aliases()
    _recreate_single_speaker_roles()
    _repair_supersession()
    _delete_split_originals()

    dupes = _duplicate_entities()
    if dupes:
        logger.warning(
            "%s: found %s duplicate (speaker,name) entities; skipping uniqueness "
            "constraint. Resolve duplicates and re-run.",
            ENTITY_SPEAKER_MIGRATION,
            dupes,
        )
    else:
        _create_entity_constraint()

    _mark(ENTITY_SPEAKER_MIGRATION, "completed")
    logger.info("Migration %s complete.", ENTITY_SPEAKER_MIGRATION)
    return {"migration": ENTITY_SPEAKER_MIGRATION, "status": "completed", "duplicates": dupes}


# ── location_speaker_v1 ────────────────────────────────────────────────────────

# Enrichment written by geo._get_or_enrich; copied verbatim onto the stamped
# copy of a single-speaker original (contested originals stay bare so each
# speaker re-enriches with their own footprint).
_LOCATION_ENRICHMENT_PROPS = (
    "latitude", "longitude", "location_point", "display_name", "category",
    "country", "region", "city", "place_card", "vibe_embedding",
    "vibe_embedding_model", "geo_status", "geo_checked_at",
)

_COPY_LOCATION_PROPS = ", ".join(
    f"copy.{prop} = l.{prop}" for prop in _LOCATION_ENRICHMENT_PROPS
)


def plan_location_speaker_v1() -> dict:
    """Return counts of what the migration would change (dry-run)."""
    return {
        "migration": LOCATION_SPEAKER_MIGRATION,
        "already_applied": is_applied(LOCATION_SPEAKER_MIGRATION),
        "global_locations": _count(
            "MATCH (l:Location) WHERE l.speaker IS NULL RETURN count(l)"
        ),
        "per_speaker_locations_target": _count(
            "MATCH (ep:Episode)-[:AT_LOCATION]->(l:Location) "
            "WHERE l.speaker IS NULL "
            "RETURN count(DISTINCT [l.name, ep.speaker])"
        ),
        "contested_locations": _count(
            "MATCH (ep:Episode)-[:AT_LOCATION]->(l:Location) "
            "WHERE l.speaker IS NULL "
            "WITH l, count(DISTINCT ep.speaker) AS speakers WHERE speakers > 1 "
            "RETURN count(DISTINCT l)"
        ),
        "orphan_locations": _count(
            "MATCH (l:Location) WHERE l.speaker IS NULL "
            "AND NOT (l)<-[:AT_LOCATION]-(:Episode) RETURN count(l)"
        ),
    }


def _mark_contested_locations() -> None:
    """Flag globals referenced by more than one speaker's episodes.

    Their first-writer enrichment is right for at most one tenant, so the split
    stamps their copies bare instead of copying it. The flag lives on the
    original only and dies with it in ``_delete_split_location_originals``.
    """
    run_query(
        "MATCH (ep:Episode)-[:AT_LOCATION]->(l:Location) WHERE l.speaker IS NULL "
        "WITH l, count(DISTINCT ep.speaker) AS speakers WHERE speakers > 1 "
        "SET l.contested_v1 = true"
    )


def _split_locations() -> None:
    """Re-point every AT_LOCATION edge to a per-speaker copy of the location.

    Two passes with disjoint predicates: non-contested originals donate their
    enrichment to the copy (it was disambiguated by that speaker's footprint);
    contested ones donate nothing, leaving ``geo_status`` NULL so each copy
    re-enriches for its own speaker.  Safe to re-run: already-correct edges
    fail the WHERE and are skipped.
    """
    for contested_clause, on_create in (
        ("l.contested_v1 IS NULL", f" ON CREATE SET {_COPY_LOCATION_PROPS}"),
        ("l.contested_v1 = true", ""),
    ):
        run_query(
            "MATCH (ep:Episode)-[rel:AT_LOCATION]->(l:Location) "
            "WHERE (l.speaker IS NULL OR l.speaker <> ep.speaker) "
            f"  AND {contested_clause} "
            "CALL { "
            "  WITH ep, rel, l "
            "  MERGE (copy:Location {name: l.name, speaker: ep.speaker})"
            f"{on_create} "
            "  MERGE (ep)-[:AT_LOCATION]->(copy) "
            "  DELETE rel "
            f"}} IN TRANSACTIONS OF {_BATCH} ROWS"
        )


def _delete_split_location_originals() -> None:
    """Remove global (speaker-null) locations that now have a stamped twin.

    True orphans (no same-name stamped twin) are left untouched."""
    run_query(
        "MATCH (l:Location) WHERE l.speaker IS NULL "
        "CALL { "
        "  WITH l "
        "  MATCH (twin:Location {name: l.name}) WHERE twin.speaker IS NOT NULL "
        "  WITH l, count(twin) AS twins WHERE twins > 0 "
        "  DETACH DELETE l "
        f"}} IN TRANSACTIONS OF {_BATCH} ROWS"
    )


def _duplicate_locations() -> int:
    return _count(
        "MATCH (l:Location) WHERE l.speaker IS NOT NULL "
        "WITH l.speaker AS sp, l.name AS nm, count(*) AS c WHERE c > 1 "
        "RETURN count(*)"
    )


def _create_location_constraint() -> None:
    """Create the (speaker, name) uniqueness constraint, or a composite index."""
    try:
        run_query(
            "CREATE CONSTRAINT location_name_speaker_unique IF NOT EXISTS "
            "FOR (l:Location) REQUIRE (l.speaker, l.name) IS UNIQUE"
        )
        return
    except Exception as exc:
        logger.warning(
            "Location (speaker,name) uniqueness constraint unavailable (%s); "
            "falling back to a composite index",
            exc,
        )
    try:
        run_query(
            "CREATE INDEX location_name_speaker IF NOT EXISTS "
            "FOR (l:Location) ON (l.speaker, l.name)"
        )
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Location composite index creation failed: %s", exc)


def apply_location_speaker_v1() -> dict:
    """Apply the migration. Idempotent and resumable.

    Raises on a hard failure so the marker is not set and the run is retried
    (each phase is safe to re-run). Returns a summary dict.
    """
    if is_applied(LOCATION_SPEAKER_MIGRATION):
        return {"migration": LOCATION_SPEAKER_MIGRATION, "status": "already_applied"}

    logger.info("Applying migration %s …", LOCATION_SPEAKER_MIGRATION)
    _mark(LOCATION_SPEAKER_MIGRATION, "in_progress")

    _mark_contested_locations()
    _split_locations()
    _delete_split_location_originals()

    dupes = _duplicate_locations()
    if dupes:
        logger.warning(
            "%s: found %s duplicate (speaker,name) locations; skipping uniqueness "
            "constraint. Resolve duplicates and re-run.",
            LOCATION_SPEAKER_MIGRATION,
            dupes,
        )
    else:
        _create_location_constraint()

    _mark(LOCATION_SPEAKER_MIGRATION, "completed")
    logger.info("Migration %s complete.", LOCATION_SPEAKER_MIGRATION)
    return {"migration": LOCATION_SPEAKER_MIGRATION, "status": "completed", "duplicates": dupes}


EPISODE_INDEX_MIGRATION = "episode_index_v1"


def apply_episode_index_v1() -> dict:
    """Index :Episode on the two properties every query already filters by.

    Entity and Location were indexed by their own migrations; Episode never was,
    so it carried no index or constraint at all. That is expensive in a way that
    is invisible until the graph grows, because Episode is both the most numerous
    node type and the one every retrieval lane filters:

      * ``MATCH (ep:Episode {id:eid})`` — subgraph expansion does this seven
        times per retrieval, once per child query, over the same id list.
      * ``MATCH (ep:Episode {speaker:$speaker})`` — the recency, temporal,
        metadata, image-gate, backup and clear paths all start here.

    Without an index each of those is a full label scan of **every tenant's**
    episodes, so one account's query latency grows with total product usage
    rather than with its own data. These indexes make both lookups seeks.

    Pure schema, no data rewrite: unlike its two predecessors this migration
    moves no nodes and re-points no edges, so it is safe to re-run and safe to
    apply while serving traffic. Index builds are online in Neo4j 5; on a large
    graph the index is populated in the background and simply is not used until
    it comes online.
    """
    if is_applied(EPISODE_INDEX_MIGRATION):
        return {"migration": EPISODE_INDEX_MIGRATION, "status": "already_applied"}

    logger.info("Applying migration %s …", EPISODE_INDEX_MIGRATION)
    _mark(EPISODE_INDEX_MIGRATION, "in_progress")

    created: list[str] = []

    # Episode.id is the hot lookup: expansion resolves a list of ids seven times
    # per retrieval. Prefer a uniqueness constraint — it enforces the invariant
    # the code already assumes AND provides the index — but never fail the
    # migration over it: an existing duplicate id would make the constraint
    # unsatisfiable, and a plain index still delivers the whole speed benefit.
    try:
        run_query(
            "CREATE CONSTRAINT episode_id_unique IF NOT EXISTS "
            "FOR (ep:Episode) REQUIRE ep.id IS UNIQUE"
        )
        created.append("episode_id_unique")
    except Exception as exc:
        logger.warning(
            "Episode id uniqueness constraint unavailable (%s); "
            "falling back to a plain index",
            exc,
        )
        try:
            run_query("CREATE INDEX episode_id IF NOT EXISTS FOR (ep:Episode) ON (ep.id)")
            created.append("episode_id")
        except Exception as inner:  # pragma: no cover - best effort
            logger.warning("Episode id index creation failed: %s", inner)

    # Speaker alone covers the existence probes and the delete/backup paths.
    try:
        run_query("CREATE INDEX episode_speaker IF NOT EXISTS FOR (ep:Episode) ON (ep.speaker)")
        created.append("episode_speaker")
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Episode speaker index creation failed: %s", exc)

    # Composite for the lanes that filter by speaker and then order or window on
    # time — the recency safety net and the temporal lane both do exactly this.
    try:
        run_query(
            "CREATE INDEX episode_speaker_timestamp IF NOT EXISTS "
            "FOR (ep:Episode) ON (ep.speaker, ep.timestamp)"
        )
        created.append("episode_speaker_timestamp")
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("Episode (speaker,timestamp) index creation failed: %s", exc)

    _mark(EPISODE_INDEX_MIGRATION, "completed")
    logger.info("Migration %s complete: %s", EPISODE_INDEX_MIGRATION, created)
    return {"migration": EPISODE_INDEX_MIGRATION, "status": "completed", "created": created}


def run_pending_migrations() -> None:
    """Startup entry point: apply any migration not yet marked completed."""
    _ensure_marker_constraint()
    if not is_applied(ENTITY_SPEAKER_MIGRATION):
        apply_entity_speaker_v1()
    if not is_applied(LOCATION_SPEAKER_MIGRATION):
        apply_location_speaker_v1()
    if not is_applied(EPISODE_INDEX_MIGRATION):
        apply_episode_index_v1()
