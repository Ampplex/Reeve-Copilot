"""GSW-style Reconciler – writes semantic extractions into the Neo4j workspace.

Includes:
  • **State contradiction resolution**: new states SUPERSEDE old conflicting
    ones so only the latest value is active for each entity+attribute pair.
  • **Memory consolidation**: old low-importance episodes are periodically
    merged into compact summary episodes so the graph stays manageable
    across decades of data.
  • **Embedding versioning**: stores model version on every Episode for future
    migration safety.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from threelane_memory.config import (
    CONSOLIDATION_AGE_DAYS,
    CONSOLIDATION_BATCH_SIZE,
    CONSOLIDATION_IMPORTANCE_CAP,
    EMBEDDING_MODEL_VERSION,
)
from threelane_memory.database import run_query
from threelane_memory.embeddings import cosine_similarity, embed
from threelane_memory.llm_interface import invoke_llm
from threelane_memory.schemas import (
    ActionItem,
    EntityRole,
    RelationItem,
    SemanticExtraction,
    StateItem,
)

# ── Entity-merge tunables ──────────────────────────────────────────────────────
# Entities are keyed by (name, speaker): each tenant gets its own node so roles,
# states, actions and relations attach to a per-speaker entity and never leak.
ENTITY_SIMILARITY_THRESHOLD = 0.93  # cosine floor for aliasing to an existing entity
ENTITY_SCAN_MAX = 2000  # above this many per-tenant entities, use ANN instead of a full scan
ENTITY_ANN_OVERSAMPLE = 200  # ANN candidates fetched before the speaker post-filter


def _build_searchable_text(
    semantics: SemanticExtraction,
    raw_text: str = "",
    location_context: str | None = None,
) -> str:
    """Combine all semantic fields into a single string for embedding."""
    parts = []
    if raw_text:
        parts.append(raw_text)
    parts.append(semantics["summary"])
    for ent in _valid_entities(semantics.get("entities", [])):
        parts.append(ent)
    for role in _valid_roles(semantics.get("roles", [])):
        parts.append(f"{role['entity']} is {role['role']}")
    for act in _valid_actions(semantics.get("actions", [])):
        obj = f" {act['object']}" if act.get("object") else ""
        parts.append(f"{act['actor']} {act['verb']}{obj}")
    for st in _valid_states(semantics.get("states", [])):
        parts.append(f"{st['entity']} {st['attribute']} is {st['value']}")
    if semantics.get("location"):
        parts.append(f"at {semantics['location']}")
        if location_context:
            parts.append(location_context)
    for rel in _valid_relations(semantics.get("relations", [])):
        parts.append(f"{rel['subject']} {rel['relation']} {rel['object']}")
    return ". ".join(parts)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _clean_string(value: object) -> str | None:
    """Return a stripped string value, or None when empty/invalid."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _valid_entities(entities: Sequence[object] | None) -> list[str]:
    """Keep only non-empty entity names."""
    if not isinstance(entities, list):
        return []
    cleaned: list[str] = []
    for entity in entities:
        name = _clean_string(entity)
        if name:
            cleaned.append(name)
    return cleaned


def _valid_roles(roles: Sequence[object] | None) -> list[EntityRole]:
    """Drop malformed roles so ingestion can proceed on partial extractions."""
    if not isinstance(roles, list):
        return []
    cleaned: list[EntityRole] = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        entity = _clean_string(role.get("entity"))
        role_name = _clean_string(role.get("role"))
        if entity and role_name:
            cleaned.append({"entity": entity, "role": role_name})
    return cleaned


def _valid_actions(actions: Sequence[object] | None) -> list[ActionItem]:
    """Drop malformed actions while preserving optional objects."""
    if not isinstance(actions, list):
        return []
    cleaned: list[ActionItem] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        actor = _clean_string(action.get("actor"))
        verb = _clean_string(action.get("verb"))
        if not actor or not verb:
            continue
        cleaned.append(
            {
                "actor": actor,
                "verb": verb,
                "object": _clean_string(action.get("object")),
            }
        )
    return cleaned


def _valid_states(states: Sequence[object] | None) -> list[StateItem]:
    """Drop malformed states so missing model fields do not crash reconcile."""
    if not isinstance(states, list):
        return []
    cleaned: list[StateItem] = []
    for state in states:
        if not isinstance(state, dict):
            continue
        entity = _clean_string(state.get("entity"))
        attribute = _clean_string(state.get("attribute"))
        value = _clean_string(state.get("value"))
        if entity and attribute and value:
            cleaned.append(
                {
                    "entity": entity,
                    "attribute": attribute,
                    "value": value,
                }
            )
    return cleaned


def _valid_relations(relations: Sequence[object] | None) -> list[RelationItem]:
    """Drop malformed subject-relation-object triples."""
    if not isinstance(relations, list):
        return []
    cleaned: list[RelationItem] = []
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        subject = _clean_string(relation.get("subject"))
        rel = _clean_string(relation.get("relation"))
        obj = _clean_string(relation.get("object"))
        if subject and rel and obj:
            cleaned.append({"subject": subject, "relation": rel, "object": obj})
    return cleaned


def _valid_ints(values: Sequence[object] | None) -> list[int]:
    """Keep integer values suitable for Neo4j primitive list properties."""
    if not isinstance(values, list):
        return []
    cleaned: list[int] = []
    for value in values:
        try:
            cleaned.append(int(str(value)))
        except (TypeError, ValueError):
            continue
    return cleaned


def create_episode(
    semantics: SemanticExtraction,
    speaker: str,
    raw_text: str = "",
    source_speaker: str | None = None,
    event_time: str | None = None,
    event_id: int | str | None = None,
    event_title: str | None = None,
    event_year: int | None = None,
    event_timeline: str | None = None,
    event_phase: str | None = None,
    event_location: str | None = None,
    character_ages: list[int] | None = None,
    location_context: str | None = None,
    image_embedding: list[float] | None = None,
    image_key: str | None = None,
) -> str:
    """Create an Episode node with an embedding vector and return its id.

    Stores `embedding_model` so embeddings can be re-generated when the
    model is changed in the future (70-year migration safety).

    When *image_embedding* is supplied (a memory built from a photo), it is
    stored on ``ep.image_embedding`` in the multimodal space for image search.
    *image_key* records where the original photo was retained, so it can be
    shown to the vision model later — and, just as importantly, so erasure can
    find and delete it.
    """
    episode_id = f"ep_{uuid.uuid4().hex[:10]}"
    searchable = _build_searchable_text(
        semantics, raw_text=raw_text, location_context=location_context
    )
    vector = embed(searchable)
    run_query(
        """
        CREATE (ep:Episode {
            id:              $id,
            summary:         $summary,
            raw_text:        $raw_text,
            searchable_text: $searchable_text,
            emotion:         $emotion,
            importance:      $importance,
            asserts_fact:    $asserts_fact,
            timestamp:       datetime(),
            speaker:         $speaker,
            source_speaker:  $source_speaker,
            event_time:      CASE WHEN $event_time IS NULL THEN NULL ELSE datetime($event_time) END,
            event_id:        $event_id,
            event_title:     $event_title,
            event_year:      $event_year,
            event_timeline:  $event_timeline,
            event_phase:     $event_phase,
            event_location:  $event_location,
            character_ages:  $character_ages,
            embedding:       $embedding,
            embedding_model: $embedding_model,
            image_embedding: $image_embedding,
            image_key:       $image_key
        })
        """,
        {
            "id": episode_id,
            "summary": semantics["summary"],
            "raw_text": raw_text,
            "searchable_text": searchable,
            "emotion": semantics["emotion"],
            "importance": semantics["importance"],
            # Defaulted rather than indexed off the key: extractions built
            # before this field existed, and any caller assembling semantics by
            # hand, must keep behaving as they did.
            "asserts_fact": semantics.get("asserts_fact", True),
            "speaker": speaker,
            "source_speaker": source_speaker,
            "event_time": event_time,
            "event_id": event_id,
            "event_title": event_title,
            "event_year": event_year,
            "event_timeline": event_timeline,
            "event_phase": event_phase,
            "event_location": event_location,
            "character_ages": _valid_ints(character_ages),
            "embedding": vector,
            "embedding_model": EMBEDDING_MODEL_VERSION,
            "image_embedding": image_embedding,
            "image_key": image_key,
        },
    )
    if image_embedding:
        from threelane_memory.database import ensure_image_embedding_index

        ensure_image_embedding_index()
    return episode_id


def _alias_to(alias_name: str, canonical_name: str, speaker: str) -> None:
    """Link a per-speaker alias Entity to this speaker's canonical Entity."""
    if alias_name == canonical_name:
        return
    run_query(
        "MERGE (alias:Entity {name:$alias, speaker:$speaker}) "
        "WITH alias "
        "MATCH (canon:Entity {name:$canon, speaker:$speaker}) "
        "MERGE (alias)-[:ALIAS_OF]->(canon)",
        {"alias": alias_name, "canon": canonical_name, "speaker": speaker},
    )


def _similar_entity(name_vec: list[float], speaker: str) -> str | None:
    """Return this speaker's most similar existing entity name, or None.

    Exhaustive per-tenant cosine scan (exact and leak-proof) for modest
    tenants; ANN index with a speaker post-filter as a large-tenant fallback.
    Both are scoped to *speaker* so an entity can never alias across tenants.
    """
    count_rows = run_query(
        "MATCH (e:Entity {speaker:$speaker}) WHERE e.embedding IS NOT NULL "
        "RETURN count(e) AS cnt",
        {"speaker": speaker},
    )
    total = count_rows[0]["cnt"] if count_rows else 0
    if total == 0:
        return None

    if total <= ENTITY_SCAN_MAX:
        rows = run_query(
            "MATCH (e:Entity {speaker:$speaker}) WHERE e.embedding IS NOT NULL "
            "RETURN e.name AS name, e.embedding AS embedding",
            {"speaker": speaker},
        )
        best_name: str | None = None
        best_score = ENTITY_SIMILARITY_THRESHOLD
        for row in rows:
            emb = row.get("embedding")
            if not emb:
                continue
            score = cosine_similarity(name_vec, emb)
            if score >= best_score:
                best_score = score
                best_name = str(row["name"])
        return best_name

    # Large tenant: oversample the ANN index and post-filter by speaker.
    try:
        rows = run_query(
            "CALL db.index.vector.queryNodes('entity_embedding', $k, $vector) "
            "YIELD node, score "
            "WHERE node.speaker = $speaker AND score >= $threshold "
            "RETURN node.name AS name, score ORDER BY score DESC LIMIT 1",
            {
                "k": ENTITY_ANN_OVERSAMPLE,
                "vector": name_vec,
                "speaker": speaker,
                "threshold": ENTITY_SIMILARITY_THRESHOLD,
            },
        )
    except Exception:
        return None
    return str(rows[0]["name"]) if rows else None


def merge_entity(name: str, speaker: str) -> str:
    """Ensure a per-speaker Entity node exists, resolving to this speaker's
    canonical entity if one matches.  Returns the canonical entity name to use
    for all subsequent linking (actions, states, roles, etc.).

    Entities are keyed by (name, speaker); every lookup below is scoped to
    *speaker* so one tenant's entities can never match or alias another's.

    Resolution order (first match wins), all resolved through any ALIAS_OF
    chain to the canonical head so clusters never fragment:
      1. Exact match (case-insensitive)
      2. One name is a substring of the other (e.g. "Jeff" vs "Jeffrey Epstein")
      3. Embedding cosine similarity ≥ ENTITY_SIMILARITY_THRESHOLD

    If a match is found the new name becomes an alias of the canonical entity
    via an ALIAS_OF relationship.  If no match, a new canonical Entity is created.
    """
    if not speaker:
        raise ValueError("merge_entity requires a non-empty speaker")
    name_lower = name.strip().lower()

    # ── 1. Exact match (case-insensitive), resolved to canonical head ────
    exact = run_query(
        "MATCH (e:Entity {speaker:$speaker}) WHERE toLower(e.name) = $name_lower "
        "MATCH path = (e)-[:ALIAS_OF*0..]->(head:Entity) "
        "WHERE NOT (head)-[:ALIAS_OF]->() "
        "RETURN head.name AS name ORDER BY length(path) ASC LIMIT 1",
        {"name_lower": name_lower, "speaker": speaker},
    )
    if exact:
        canonical = str(exact[0]["name"])
        _alias_to(name, canonical, speaker)
        return canonical

    # ── 2. Substring containment (longest canonical head wins) ───────────
    #    "Jeff" ⊂ "Jeffrey Epstein", or "Jeffrey Epstein" ⊃ "Jeff"
    substring_hits = run_query(
        "MATCH (e:Entity {speaker:$speaker}) "
        "WHERE toLower(e.name) CONTAINS $name_lower "
        "   OR $name_lower CONTAINS toLower(e.name) "
        "MATCH path = (e)-[:ALIAS_OF*0..]->(head:Entity) "
        "WHERE NOT (head)-[:ALIAS_OF]->() "
        "RETURN DISTINCT head.name AS name",
        {"name_lower": name_lower, "speaker": speaker},
    )
    if substring_hits:
        canonical = max((str(r["name"]) for r in substring_hits), key=len)
        _alias_to(name, canonical, speaker)
        return canonical

    # ── 3. Embedding similarity (this speaker only) ──────────────────────
    name_vec = embed(name)
    best_name = _similar_entity(name_vec, speaker)
    if best_name:
        _alias_to(name, best_name, speaker)
        return best_name

    # ── 4. No match — create new canonical entity with embedding ─────────
    #    catch-and-rematch guards the (speaker, name) uniqueness constraint
    #    against concurrent first-mentions (run_query does not retry).
    try:
        run_query(
            "MERGE (e:Entity {name:$name, speaker:$speaker}) SET e.embedding = $embedding",
            {"name": name, "speaker": speaker, "embedding": name_vec},
        )
    except Exception:
        run_query(
            "MATCH (e:Entity {name:$name, speaker:$speaker}) "
            "SET e.embedding = coalesce(e.embedding, $embedding)",
            {"name": name, "speaker": speaker, "embedding": name_vec},
        )
    return name


def link_entity_to_episode(entity: str, episode_id: str, speaker: str) -> None:
    """Connect a per-speaker Entity to an Episode via INVOLVES.

    Uses the canonical name so all episodes cluster under one Entity.
    """
    run_query(
        """
        MATCH (e:Entity {name:$entity, speaker:$speaker}),
              (ep:Episode {id:$ep})
        MERGE (ep)-[:INVOLVES]->(e)
        """,
        {"entity": entity, "ep": episode_id, "speaker": speaker},
    )


def create_action(action: ActionItem, episode_id: str, speaker: str) -> None:
    """Create an Action node linked to its actor, optional object, and episode.

    Actor and object are matched within *speaker* so an action can never link to
    another tenant's entity.  The object string is always stored on the Action;
    the ON_ENTITY edge is added only when the object is a known entity for this
    speaker.
    """
    run_query(
        """
        CREATE (a:Action {verb:$verb, object_name:$object})
        WITH a
        MATCH (actor:Entity {name:$actor, speaker:$speaker})
        MERGE (a)-[:BY_ENTITY]->(actor)
        WITH a
        OPTIONAL MATCH (obj:Entity {name:$object, speaker:$speaker})
        FOREACH (_ IN CASE WHEN obj IS NOT NULL THEN [1] ELSE [] END |
            MERGE (a)-[:ON_ENTITY]->(obj)
        )
        WITH a
        MATCH (ep:Episode {id:$ep})
        MERGE (ep)-[:HAS_ACTION]->(a)
        """,
        {
            "verb": action["verb"],
            "actor": action["actor"],
            "object": action.get("object"),
            "ep": episode_id,
            "speaker": speaker,
        },
    )


def create_role(role_item: EntityRole, speaker: str) -> None:
    """Bind a Role to this speaker's Entity.

    Role nodes are shared labels; isolation comes from the HAS_ROLE edge, which
    originates at a per-speaker Entity.
    """
    run_query(
        """
        MATCH (e:Entity {name:$entity, speaker:$speaker})
        MERGE (r:Role {name:$role})
        MERGE (e)-[:HAS_ROLE]->(r)
        """,
        {"entity": role_item["entity"], "role": role_item["role"], "speaker": speaker},
    )


def create_relation(relation_item: RelationItem, episode_id: str, speaker: str) -> None:
    """Create a subject-relation-object triple tied to an episode.

    Subject and object are matched within *speaker*.
    """
    run_query(
        """
        MATCH (subject:Entity {name:$subject, speaker:$speaker}),
              (object:Entity {name:$object, speaker:$speaker}),
              (ep:Episode {id:$ep})
        CREATE (rel:Relation {type:$relation, created_at:datetime()})
        MERGE (rel)-[:FROM_ENTITY]->(subject)
        MERGE (rel)-[:TO_ENTITY]->(object)
        MERGE (ep)-[:HAS_RELATION]->(rel)
        """,
        {
            "subject": relation_item["subject"],
            "relation": relation_item["relation"],
            "object": relation_item["object"],
            "ep": episode_id,
            "speaker": speaker,
        },
    )


def create_state(state_item: StateItem, episode_id: str, speaker: str) -> None:
    """Create a State node tied to an Entity and Episode.

    **Contradiction resolution**: if the same (entity, attribute) already
    has an active State node, the old state is marked `active:false` and a
    `SUPERSEDES` edge is created from the new state to the old one.  This
    keeps a full audit trail while ensuring only the latest value is live.
    """
    # 1. Mark any existing active state for this entity+attribute as inactive
    #    and collect its internal id so we can link SUPERSEDES. Scoped to the
    #    per-speaker entity, so one tenant can never supersede another's state.
    old_rows = run_query(
        """
        MATCH (e:Entity {name:$entity, speaker:$speaker})<-[:OF_ENTITY]-(s:State)
        WHERE s.attribute = $attribute AND s.active <> false
        SET s.active = false, s.superseded_at = datetime()
        RETURN elementId(s) AS old_id
        """,
        {"entity": state_item["entity"], "attribute": state_item["attribute"], "speaker": speaker},
    )

    # 2. Create the new (active) state
    run_query(
        """
        MATCH (e:Entity {name:$entity, speaker:$speaker}),
              (ep:Episode {id:$ep})
        CREATE (s:State {
            attribute: $attribute,
            value:     $value,
            active:    true,
            created_at: datetime()
        })
        MERGE (ep)-[:HAS_STATE]->(s)
        MERGE (s)-[:OF_ENTITY]->(e)
        """,
        {
            "entity": state_item["entity"],
            "attribute": state_item["attribute"],
            "value": state_item["value"],
            "ep": episode_id,
            "speaker": speaker,
        },
    )

    # 3. Link SUPERSEDES from new state to each old state
    if old_rows:
        old_ids = [r["old_id"] for r in old_rows]
        run_query(
            """
            MATCH (new_s:State {attribute:$attribute, active:true})
                  -[:OF_ENTITY]->(e:Entity {name:$entity, speaker:$speaker})
            UNWIND $old_ids AS oid
            MATCH (old_s) WHERE elementId(old_s) = oid
            MERGE (new_s)-[:SUPERSEDES]->(old_s)
            """,
            {
                "entity": state_item["entity"],
                "attribute": state_item["attribute"],
                "old_ids": old_ids,
                "speaker": speaker,
            },
        )


def bind_location(location: str, episode_id: str, speaker: str) -> None:
    """Attach *speaker*'s Location node to an Episode.

    Location nodes are keyed (name, speaker) — per tenant, like entities — so
    the same place name never shares a node (or its coordinates) across
    speakers."""
    run_query(
        """
        MERGE (loc:Location {name:$location, speaker:$speaker})
        WITH loc
        MATCH (ep:Episode {id:$ep})
        MERGE (ep)-[:AT_LOCATION]->(loc)
        """,
        {"location": location, "speaker": speaker, "ep": episode_id},
    )


# ── Main entry point ─────────────────────────────────────────────────────────


def reconcile(
    semantics: SemanticExtraction,
    speaker: str,
    raw_text: str = "",
    source_speaker: str | None = None,
    event_time: str | None = None,
    event_id: int | str | None = None,
    event_title: str | None = None,
    event_year: int | None = None,
    event_timeline: str | None = None,
    event_phase: str | None = None,
    event_location: str | None = None,
    character_ages: list[int] | None = None,
    image_embedding: list[float] | None = None,
    image_key: str | None = None,
) -> str:
    """Write a full SemanticExtraction into Neo4j. Returns the episode id."""

    raw_location = semantics.get("location")
    location = raw_location if isinstance(raw_location, str) and raw_location else None

    # 0. Geo enrichment (optional, best-effort): resolve the place once and
    #    fold its "place card" into this episode's searchable text so
    #    vibe-level queries ("somewhere with beaches") reach it through the
    #    existing vector and full-text lanes.
    location_context: str | None = None
    if location:
        from threelane_memory.geo import get_location_context, location_descriptor

        # Pass the speaker so an ambiguous place ("City Palace") is disambiguated
        # toward this speaker's other known places, not a global namesake.
        geo_ctx = get_location_context(location, speaker=speaker)
        if geo_ctx:
            # Full descriptor: geographic hierarchy ("in Agra, Uttar Pradesh,
            # India") + vibe card, so both where and character are searchable.
            location_context = location_descriptor(geo_ctx) or None

    # 1. Episode
    episode_id = create_episode(
        semantics,
        speaker,
        raw_text=raw_text,
        source_speaker=source_speaker,
        event_time=event_time,
        event_id=event_id,
        event_title=event_title,
        event_year=event_year,
        event_timeline=event_timeline,
        event_phase=event_phase,
        event_location=event_location,
        character_ages=character_ages,
        location_context=location_context,
        image_embedding=image_embedding,
        image_key=image_key,
    )

    # 2. Entities — resolve to canonical names within this speaker.
    #    Build a mapping {extracted_name → canonical_name} so actions/states/roles
    #    use the resolved canonical entity.
    alias_map: dict[str, str] = {}
    for entity in _valid_entities(semantics.get("entities", [])):
        canonical = merge_entity(entity, speaker)
        alias_map[entity] = canonical
        link_entity_to_episode(canonical, episode_id, speaker)

    def _resolve(raw_name: str) -> str:
        """Resolve a referenced name to this speaker's canonical entity,
        MERGE-creating it if it wasn't in the entities list.  Without this a
        strict per-speaker MATCH would silently drop the fact."""
        if raw_name in alias_map:
            return alias_map[raw_name]
        canonical = merge_entity(raw_name, speaker)
        alias_map[raw_name] = canonical
        return canonical

    # 3. Roles (use canonical names)
    for role in _valid_roles(semantics.get("roles", [])):
        resolved_role: EntityRole = {"entity": _resolve(role["entity"]), "role": role["role"]}
        create_role(resolved_role, speaker)

    # 4. Relations (use canonical names)
    for relation in _valid_relations(semantics.get("relations", [])):
        resolved_relation: RelationItem = {
            "subject": _resolve(relation["subject"]),
            "relation": relation["relation"],
            "object": _resolve(relation["object"]),
        }
        create_relation(resolved_relation, episode_id, speaker)

    # 5. Actions (use canonical names)
    for action in _valid_actions(semantics.get("actions", [])):
        action_object = action.get("object")
        # Objects stay optional: keep the raw string (create_action always stores
        # object_name) and only resolve when it is already a known entity, so we
        # don't manufacture an entity node for every free-text object.
        resolved_action_object: str | None = None
        if action_object:
            resolved_action_object = alias_map.get(action_object) or action_object
        resolved_action: ActionItem = {
            "actor": _resolve(action["actor"]),
            "verb": action["verb"],
            "object": resolved_action_object,
        }
        create_action(resolved_action, episode_id, speaker)

    # 6. States (use canonical names)
    for state in _valid_states(semantics.get("states", [])):
        resolved_state: StateItem = {
            "entity": _resolve(state["entity"]),
            "attribute": state["attribute"],
            "value": state["value"],
        }
        create_state(resolved_state, episode_id, speaker)

    # 7. Location
    if location:
        bind_location(location, episode_id, speaker)

    return episode_id


# ══════════════════════════════════════════════════════════════════════════════
#  MEMORY CONSOLIDATION – keeps the graph manageable over decades
# ══════════════════════════════════════════════════════════════════════════════


def _find_consolidation_candidates(speaker: str) -> list[dict]:
    """Return old, low-importance episodes eligible for consolidation."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CONSOLIDATION_AGE_DAYS)).isoformat()
    rows = run_query(
        "MATCH (ep:Episode {speaker:$speaker}) "
        "WHERE ep.importance <= $cap "
        "  AND ep.timestamp <= datetime($cutoff) "
        "  AND ep.consolidated IS NULL "
        "RETURN ep.id AS id, ep.summary AS summary, ep.raw_text AS raw_text, "
        "       ep.importance AS importance, toString(ep.timestamp) AS ts "
        "ORDER BY ep.timestamp ASC "
        "LIMIT $limit",
        {
            "speaker": speaker,
            "cap": CONSOLIDATION_IMPORTANCE_CAP,
            "cutoff": cutoff,
            "limit": CONSOLIDATION_BATCH_SIZE,
        },
    )
    return rows


def _summarise_batch(episodes: list[dict]) -> str:
    """Use the LLM to produce a compact summary of a batch of episodes."""
    texts = []
    for ep in episodes:
        display = ep.get("raw_text") or ep["summary"]
        texts.append(f"[{ep.get('ts', '?')}] {display}")
    prompt = (
        "Combine the following memory episodes into ONE concise summary "
        "paragraph.  Preserve all concrete facts, names, and numbers.\n\n" + "\n".join(texts)
    )
    return invoke_llm(prompt)


def consolidate(speaker: str) -> dict:
    """Run one round of memory consolidation for *speaker*.

    1. Find old low-importance episodes.
    2. Summarise them into a single consolidated episode.
    3. Mark originals as consolidated (keep for audit, but exclude from
       future retrieval by default).

    Returns {"merged": int, "consolidated_episode_id": str | None}.
    """
    candidates = _find_consolidation_candidates(speaker)
    if not candidates:
        return {"merged": 0, "consolidated_episode_id": None}

    # Build consolidated summary via LLM
    summary_text = _summarise_batch(candidates)
    episode_id = f"ep_consolidated_{uuid.uuid4().hex[:8]}"

    # Collect all involved entity names from the candidate episodes
    candidate_ids = [c["id"] for c in candidates]
    ent_rows = run_query(
        "UNWIND $ids AS eid "
        "MATCH (ep:Episode {id:eid})-[:INVOLVES]->(e:Entity) "
        "RETURN DISTINCT e.name AS entity",
        {"ids": candidate_ids},
    )
    entity_names = [r["entity"] for r in ent_rows]

    # Build searchable text and embed
    searchable = summary_text + ". " + ". ".join(entity_names)
    vector = embed(searchable)

    # Average importance of originals
    avg_importance = sum(c["importance"] for c in candidates) / len(candidates)

    # Create consolidated episode
    run_query(
        """
        CREATE (ep:Episode {
            id:              $id,
            summary:         $summary,
            raw_text:        '',
            emotion:         'neutral',
            importance:      $importance,
            timestamp:       datetime(),
            speaker:         $speaker,
            embedding:       $embedding,
            embedding_model: $embedding_model,
            consolidated:    true,
            source_count:    $source_count
        })
        """,
        {
            "id": episode_id,
            "summary": summary_text,
            "importance": round(min(avg_importance + 0.1, 1.0), 2),
            "speaker": speaker,
            "embedding": vector,
            "embedding_model": EMBEDDING_MODEL_VERSION,
            "source_count": len(candidates),
        },
    )

    # Link consolidated episode to same entities (scoped to this speaker)
    for ename in entity_names:
        run_query(
            "MATCH (e:Entity {name:$entity, speaker:$speaker}), (ep:Episode {id:$ep}) "
            "MERGE (ep)-[:INVOLVES]->(e)",
            {"entity": ename, "ep": episode_id, "speaker": speaker},
        )

    # Mark originals as consolidated
    run_query(
        "UNWIND $ids AS eid "
        "MATCH (ep:Episode {id:eid}) "
        "SET ep.consolidated = true, ep.consolidated_into = $target",
        {"ids": candidate_ids, "target": episode_id},
    )

    return {"merged": len(candidates), "consolidated_episode_id": episode_id}


# ══════════════════════════════════════════════════════════════════════════════
#  HARD RESET – permanently delete all memory for one speaker
# ══════════════════════════════════════════════════════════════════════════════


def clear_speaker(speaker: str, dry_run: bool = False) -> dict:
    """Permanently delete all memory for *speaker* (hard reset).

    Removes the speaker's Episodes, Entities and Locations together with the
    State, Action, and Relation nodes owned by those episodes. Entities and
    Locations are keyed (name, speaker), so deleting them touches only this
    tenant. Shared **Role** nodes are deliberately left intact — only this
    speaker's edges to them are removed — because roles are global by name
    (isolation lives on the edge, not the node), so deleting them would corrupt
    other speakers' graphs.

    Destructive and irreversible. The caller is responsible for scoping *speaker*
    to a tenant boundary (see ``tools.clear_memory``, which composes the
    authenticated account uid). Pass ``dry_run=True`` to return the counts that
    would be deleted without touching the graph.

    Retained photos (image_store) are swept by key prefix before the graph goes,
    so erasure covers the bytes and not merely the nodes pointing at them.

    Returns {"deleted", "dry_run", "speaker", "episodes", "entities", "states",
    "actions", "relations", "locations", "images"} plus "images_deleted" on a
    real run (which can exceed "images" when it reclaims photos whose write was
    cancelled before an episode existed).
    """
    if not speaker:
        raise ValueError("clear_speaker requires a non-empty speaker")

    # Retrieval caches a "this speaker owns photos" answer for the life of the
    # process, since photos are not normally removed. This is the one path that
    # removes them, so it is also the one path that must invalidate that answer —
    # otherwise the image lane would keep firing for an erased tenant.
    try:
        from threelane_memory.retriever import forget_image_probe

        forget_image_probe(speaker)
    except Exception:  # pragma: no cover - cache invalidation must never block a delete
        pass

    def _count(query: str) -> int:
        rows = run_query(query, {"speaker": speaker})
        return int(rows[0]["n"]) if rows else 0

    def _cancel_queued_writes() -> int:
        """Drop this speaker's not-yet-persisted writes.

        Writes are acknowledged before they reach Neo4j, so deleting only what is
        already stored would let anything still queued land seconds later and
        resurrect memories the user just erased. Cancelling first makes "delete
        my memory" mean it. Best-effort: never block the delete itself.
        """
        try:
            from threelane_memory.write_buffer import get_write_buffer

            return get_write_buffer().discard_speaker(speaker)
        except Exception as exc:  # pragma: no cover - best effort
            import logging

            logging.getLogger(__name__).warning(
                "Could not cancel queued writes for %s: %s", speaker, exc
            )
            return 0

    summary = {
        "dry_run": dry_run,
        "deleted": not dry_run,
        "speaker": speaker,
        "episodes": _count("MATCH (ep:Episode {speaker:$speaker}) RETURN count(ep) AS n"),
        "entities": _count("MATCH (e:Entity {speaker:$speaker}) RETURN count(e) AS n"),
        "states": _count(
            "MATCH (:Episode {speaker:$speaker})-[:HAS_STATE]->(s:State) "
            "RETURN count(DISTINCT s) AS n"
        ),
        "actions": _count(
            "MATCH (:Episode {speaker:$speaker})-[:HAS_ACTION]->(a:Action) "
            "RETURN count(DISTINCT a) AS n"
        ),
        "relations": _count(
            "MATCH (:Episode {speaker:$speaker})-[:HAS_RELATION]->(rel:Relation) "
            "RETURN count(DISTINCT rel) AS n"
        ),
        "locations": _count(
            "MATCH (l:Location {speaker:$speaker}) RETURN count(l) AS n"
        ),
        "images": _count(
            "MATCH (ep:Episode {speaker:$speaker}) WHERE ep.image_key IS NOT NULL "
            "RETURN count(ep) AS n"
        ),
    }

    if dry_run:
        from threelane_memory.write_buffer import get_write_buffer

        summary["queued_writes"] = len(get_write_buffer().get_pending_for_speaker(speaker))
        return summary

    # Cancel queued writes BEFORE deleting: an entry that slipped through in
    # between would otherwise be persisted after the delete and survive it.
    summary["queued_writes_cancelled"] = _cancel_queued_writes()

    # Delete retained photos BEFORE the episodes that reference them — and sweep
    # by key PREFIX rather than by the keys stored on those episodes. The prefix
    # is authoritative: it also catches photos uploaded for a write that was
    # cancelled above, which no episode points at and which nothing else would
    # ever find. Erasure has to mean the bytes, not just the graph.
    try:
        from threelane_memory import image_store

        summary["images_deleted"] = image_store.delete_speaker(speaker)
    except Exception as exc:  # pragma: no cover - best effort
        import logging

        logging.getLogger(__name__).error(
            "RETAINED PHOTOS MAY HAVE SURVIVED erasure for %s: %s", speaker, exc
        )
        summary["images_deleted"] = 0

    # Delete episodes and the State/Action/Relation nodes they own. DETACH DELETE
    # drops edges to shared Role nodes without deleting those nodes.
    run_query(
        """
        MATCH (ep:Episode {speaker:$speaker})
        OPTIONAL MATCH (ep)-[:HAS_STATE]->(s:State)
        OPTIONAL MATCH (ep)-[:HAS_ACTION]->(a:Action)
        OPTIONAL MATCH (ep)-[:HAS_RELATION]->(rel:Relation)
        DETACH DELETE s, a, rel, ep
        """,
        {"speaker": speaker},
    )
    # Delete entities and any State still attached to them (belt-and-suspenders:
    # States are single-speaker, so this only removes this speaker's residue).
    run_query(
        """
        MATCH (e:Entity {speaker:$speaker})
        OPTIONAL MATCH (e)<-[:OF_ENTITY]-(s:State)
        DETACH DELETE s, e
        """,
        {"speaker": speaker},
    )
    # Delete this speaker's Location nodes (keyed (name, speaker) — theirs alone).
    run_query(
        "MATCH (l:Location {speaker:$speaker}) DETACH DELETE l",
        {"speaker": speaker},
    )
    return summary


# ══════════════════════════════════════════════════════════════════════════════
#  EMBEDDING RE-INDEXING – migrate old embeddings to a new model
# ══════════════════════════════════════════════════════════════════════════════


def reindex_embeddings(speaker: str, old_model: str, batch_size: int = 100) -> int:
    """Re-embed all episodes that were embedded with *old_model*.

    Call this after changing EMBEDDING_MODEL_VERSION in config.  Processes
    in batches to avoid rate-limit issues.

    Returns the number of episodes re-indexed.
    """
    total = 0
    while True:
        rows = run_query(
            "MATCH (ep:Episode {speaker:$speaker}) "
            "WHERE ep.embedding_model = $old_model OR ep.embedding_model IS NULL "
            "RETURN ep.id AS id, ep.summary AS summary, ep.raw_text AS raw_text, "
            "       ep.searchable_text AS searchable_text "
            "LIMIT $limit",
            {"speaker": speaker, "old_model": old_model, "limit": batch_size},
        )
        if not rows:
            break
        for r in rows:
            text = r.get("searchable_text") or r.get("raw_text") or r["summary"]
            new_vec = embed(text)
            run_query(
                "MATCH (ep:Episode {id:$id}) "
                "SET ep.embedding = $vec, ep.embedding_model = $model, "
                "    ep.searchable_text = $searchable_text",
                {
                    "id": r["id"],
                    "vec": new_vec,
                    "model": EMBEDDING_MODEL_VERSION,
                    "searchable_text": text,
                },
            )
        total += len(rows)
    return total
