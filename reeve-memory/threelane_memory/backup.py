"""Backup & export utility – full graph dump to timestamped JSON files.

Supports:
  • Full graph export (all nodes + relationships)
  • Per-speaker export
  • Incremental export (since last backup timestamp)

Usage:
    python -m threelane_memory.backup                     # full export
    python -m threelane_memory.backup --speaker ankesh    # single user
    python -m threelane_memory.backup --since 2025-01-01  # incremental
"""

from __future__ import annotations


import json
import os
import re
from datetime import datetime, timezone

from threelane_memory.config import BACKUP_DIR
from threelane_memory.database import close, run_query

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.=-]+")


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _episode_clauses(alias: str, speaker: str | None, since: str | None) -> tuple[list[str], dict]:
    clauses = []
    params: dict = {}
    if speaker:
        clauses.append(f"{alias}.speaker = $speaker")
        params["speaker"] = speaker
    if since:
        clauses.append(f"{alias}.timestamp >= datetime($since)")
        params["since"] = since
    return clauses, params


def _where_clause(clauses: list[str]) -> str:
    return "WHERE " + " AND ".join(clauses) if clauses else ""


def _safe_filename_component(value: str) -> str:
    safe = _SAFE_FILENAME_RE.sub("_", value).strip("._")
    return (safe or "speaker")[:80]


def export_episodes(speaker: str | None = None, since: str | None = None) -> list[dict]:
    """Export Episode nodes with optional speaker/date filters."""
    clauses, params = _episode_clauses("ep", speaker, since)
    where = _where_clause(clauses)

    return run_query(
        f"MATCH (ep:Episode) {where} "
        "RETURN ep.id AS id, ep.summary AS summary, ep.raw_text AS raw_text, "
        "       ep.emotion AS emotion, ep.importance AS importance, "
        "       toString(ep.timestamp) AS timestamp, ep.speaker AS speaker, "
        "       ep.source_speaker AS source_speaker, "
        "       toString(ep.event_time) AS event_time, "
        "       ep.embedding_model AS embedding_model, "
        "       ep.consolidated AS consolidated, "
        "       ep.consolidated_into AS consolidated_into, "
        "       ep.source_count AS source_count "
        "ORDER BY ep.timestamp ASC",
        params,
    )


def export_entities(speaker: str | None = None, since: str | None = None) -> list[dict]:
    """Export Entity nodes, scoped to matching episodes when filters are provided."""
    if speaker or since:
        clauses, params = _episode_clauses("ep", speaker, since)
        where = _where_clause(clauses)
        return run_query(
            f"MATCH (ep:Episode)-[:INVOLVES]->(e:Entity) {where} "
            "RETURN DISTINCT e.name AS name, e.speaker AS speaker ORDER BY e.name",
            params,
        )
    # Entities are keyed by (name, speaker); include speaker so two tenants'
    # same-named entities stay distinguishable in a global backup.
    return run_query("MATCH (e:Entity) RETURN e.name AS name, e.speaker AS speaker")


def export_states(
    active_only: bool = False,
    speaker: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Export State nodes with their entity links."""
    clauses, params = _episode_clauses("ep", speaker, since)
    if active_only:
        clauses.append("coalesce(s.active, true) <> false")
    where = _where_clause(clauses)
    if speaker or since:
        return run_query(
            "MATCH (ep:Episode)-[:HAS_STATE]->(s:State)-[:OF_ENTITY]->(e:Entity) "
            f"{where} "
            "OPTIONAL MATCH (s)-[:SUPERSEDES]->(old:State) "
            "RETURN s.attribute AS attribute, s.value AS value, "
            "       s.active AS active, e.name AS entity, e.speaker AS speaker, "
            "       toString(s.created_at) AS created_at, "
            "       toString(s.superseded_at) AS superseded_at, "
            "       old.value AS superseded_value",
            params,
        )
    active_filter = "AND coalesce(s.active, true) <> false" if active_only else ""
    return run_query(
        "MATCH (s:State)-[:OF_ENTITY]->(e:Entity) "
        f"WHERE true {active_filter} "
        "OPTIONAL MATCH (s)-[:SUPERSEDES]->(old:State) "
        "RETURN s.attribute AS attribute, s.value AS value, "
        "       s.active AS active, e.name AS entity, e.speaker AS speaker, "
        "       toString(s.created_at) AS created_at, "
        "       toString(s.superseded_at) AS superseded_at, "
        "       old.value AS superseded_value"
    )


def export_roles(speaker: str | None = None, since: str | None = None) -> list[dict]:
    """Export Entity to Role relationships."""
    if speaker or since:
        clauses, params = _episode_clauses("ep", speaker, since)
        where = _where_clause(clauses)
        return run_query(
            "MATCH (ep:Episode)-[:INVOLVES]->(e:Entity)-[:HAS_ROLE]->(r:Role) "
            f"{where} "
            "RETURN DISTINCT e.name AS entity, e.speaker AS speaker, r.name AS role",
            params,
        )
    return run_query(
        "MATCH (e:Entity)-[:HAS_ROLE]->(r:Role) "
        "RETURN e.name AS entity, e.speaker AS speaker, r.name AS role"
    )


def export_actions(speaker: str | None = None, since: str | None = None) -> list[dict]:
    """Export Action nodes with actor/object/episode links."""
    clauses, params = _episode_clauses("ep", speaker, since)
    where = _where_clause(clauses)
    return run_query(
        "MATCH (ep:Episode)-[:HAS_ACTION]->(a:Action)-[:BY_ENTITY]->(actor:Entity) "
        "OPTIONAL MATCH (a)-[:ON_ENTITY]->(obj:Entity) "
        f"{where} "
        "RETURN ep.id AS episode_id, actor.name AS actor, actor.speaker AS speaker, "
        "       a.verb AS verb, obj.name AS object",
        params,
    )


def export_locations(speaker: str | None = None, since: str | None = None) -> list[dict]:
    """Export Episode to Location relationships."""
    clauses, params = _episode_clauses("ep", speaker, since)
    where = _where_clause(clauses)
    return run_query(
        "MATCH (ep:Episode)-[:AT_LOCATION]->(loc:Location) "
        f"{where} "
        "RETURN ep.id AS episode_id, loc.name AS location",
        params,
    )


def export_involves(speaker: str | None = None, since: str | None = None) -> list[dict]:
    """Export Episode to Entity INVOLVES relationships."""
    clauses, params = _episode_clauses("ep", speaker, since)
    where = _where_clause(clauses)
    return run_query(
        "MATCH (ep:Episode)-[:INVOLVES]->(e:Entity) "
        f"{where} "
        "RETURN ep.id AS episode_id, e.name AS entity, e.speaker AS speaker",
        params,
    )


def full_export(speaker: str | None = None, since: str | None = None) -> dict:
    """Run a complete graph export and return as a single dict."""
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "speaker_filter": speaker,
        "since_filter": since,
        "episodes": export_episodes(speaker, since),
        "entities": export_entities(speaker, since),
        "states": export_states(speaker=speaker, since=since),
        "roles": export_roles(speaker, since),
        "actions": export_actions(speaker, since),
        "locations": export_locations(speaker, since),
        "involves": export_involves(speaker, since),
    }


def save_backup(speaker: str | None = None, since: str | None = None) -> str:
    """Export the graph and save to a timestamped JSON file.

    Returns the path to the backup file.
    """
    _ensure_dir(BACKUP_DIR)
    data = full_export(speaker, since)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{_safe_filename_component(speaker)}" if speaker else ""
    filename = f"backup{suffix}_{ts}.json"
    filepath = os.path.join(BACKUP_DIR, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    # Write a summary
    summary = {
        "episodes": len(data["episodes"]),
        "entities": len(data["entities"]),
        "states": len(data["states"]),
        "roles": len(data["roles"]),
        "actions": len(data["actions"]),
        "locations": len(data["locations"]),
    }
    print(f"  📦 Backup saved to {filepath}")
    for k, v in summary.items():
        print(f"     {k}: {v}")

    return filepath


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Backup memory graph to JSON")
    parser.add_argument("--speaker", type=str, default=None, help="Filter by speaker")
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Export only episodes since this ISO date (e.g. 2025-01-01)",
    )
    args = parser.parse_args()

    try:
        path = save_backup(speaker=args.speaker, since=args.since)
        print(f"\n  ✅ Done: {path}")
    finally:
        close()
