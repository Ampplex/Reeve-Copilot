"""Geospatial enrichment for Location nodes.

The first time a memory mentions a place, its Location node is enriched with:
  • coordinates — ``latitude``/``longitude`` plus a native Neo4j point in
    ``location_point``, so spatial predicates (``point.distance``) work
  • geocoder context — country, region, category, canonical display name
  • a compact LLM-written "place card" describing the place's character
    ("Goa: coastal Indian state known for beaches, nightlife …"), embedded with
    the active embedding provider into ``vibe_embedding``

The place card is also folded into each mentioning episode's searchable text at
write time (see ``reconciler.reconcile``), so vibe-level queries ("somewhere
with beaches") reach those episodes through the existing vector and full-text
retrieval lanes.

Location nodes are keyed ``(name, speaker)`` — per tenant, like entities
(``location_speaker_v1`` migration). A place *name* is not a universal fact:
"City Palace" is Udaipur for one user and Jaipur for another, and "Home" is a
different point on Earth for everyone. Each speaker's node is enriched with
*their* footprint as disambiguation context, so the same name can carry
different coordinates, cards and vibes per tenant. (Only the query-side anchor
geocode cache stays global — it persists nothing and is just a fallback for
anchors the speaker never stored.)

Everything here is best-effort — a geocoder outage, LLM failure, or embedding
error must never break the write path. Outcomes (including "not found") are
persisted on the node so each unique place name is resolved at most once; a
*transient* geocoder failure is deliberately not persisted so a later mention
retries.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import requests

from threelane_memory.config import (
    EMBEDDING_MODEL_VERSION,
    GEO_DISAMBIGUATION_MAX_KM,
    GEO_DISAMBIGUATION_MAX_TRIES,
    GEO_ENRICHMENT_ENABLED,
    GEO_NEAR_RADIUS_KM,
    GEO_VIBE_MIN_SIMILARITY,
    GEOCODER_MIN_INTERVAL_SECONDS,
    GEOCODER_TIMEOUT_SECONDS,
    GEOCODER_USER_AGENT,
    NOMINATIM_BASE_URL,
    PLACE_CARD_MAX_CHARS,
)
from threelane_memory.database import ensure_location_point_index, run_query

logger = logging.getLogger(__name__)

GEO_STATUS_ENRICHED = "enriched"
GEO_STATUS_NOT_FOUND = "not_found"

_CONTEXT_KEYS = (
    "place_card", "latitude", "longitude", "country", "region", "city", "category",
)


def location_descriptor(ctx: dict[str, Any]) -> str:
    """One-line geographic + vibe descriptor for a place.

    Combines the administrative hierarchy the geocoder resolved ("in Agra,
    Uttar Pradesh, India") with the LLM vibe card, so both the place's *where*
    and its *character* enter the episode's searchable text and the answer
    context — letting "which city?" be answered, not just "which country?".
    """
    where = ", ".join(
        str(bit) for bit in (ctx.get("city"), ctx.get("region"), ctx.get("country")) if bit
    )
    parts = []
    if where:
        parts.append(f"in {where}")
    if ctx.get("place_card"):
        parts.append(str(ctx["place_card"]))
    return ". ".join(parts)

# Nominatim's usage policy caps clients at 1 request/second.
_throttle_lock = threading.Lock()
_last_geocode_at = 0.0

PLACE_CARD_PROMPT = """\
You write compact place profiles for a memory system.

Describe the character of the place below in ONE or TWO sentences: what kind of
place it is and the atmosphere, activities, geography, or culture it is known
for. Prefer concrete evocative nouns ("beaches", "nightlife", "Portuguese
heritage") over adjectives. No preamble, no quotes.

If the place is generic or private (a home, an office, an unnamed shop) or you
do not reliably know it, respond with exactly: NONE

Place name: {name}
Geocoder context: {context}
"""

# Used only to *qualify* an ambiguous geocode — the coordinates still come from
# Nominatim. Harnesses the model's strong place-name knowledge (empirically far
# better than Nominatim's importance ranking for obscure towns) to steer the
# geocoder away from a coincidental foreign namesake, e.g. plain "Ziro" resolves
# to Burkina Faso, but "Ziro, Arunachal Pradesh, India" to the real one.
GEO_REGION_HINT_PROMPT = """\
A user mentioned a place called "{name}". Which real-world location is this most \
likely to refer to?

Reply with ONLY the region and country, in the form "<State or Region>, \
<Country>" (for example "Arunachal Pradesh, India"). Do not add any \
explanation or extra words. If the name is generic, or you are not reasonably \
confident it denotes one specific place, reply with exactly: NONE
"""


def _geocoder_get(path: str, params: dict[str, Any]) -> Any:
    """Throttled GET against the geocoder (Nominatim policy: ≤ 1 req/s)."""
    global _last_geocode_at
    with _throttle_lock:
        wait = GEOCODER_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_geocode_at)
        if wait > 0:
            time.sleep(wait)
        _last_geocode_at = time.monotonic()

    response = requests.get(
        f"{NOMINATIM_BASE_URL.rstrip('/')}{path}",
        params=params,
        headers={"User-Agent": GEOCODER_USER_AGENT},
        timeout=GEOCODER_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt

    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def _min_footprint_km(lat: float, lon: float, footprint: list[dict[str, Any]]) -> float | None:
    """Distance from (lat, lon) to the nearest place in *footprint*, or None."""
    dists = [
        _haversine_km(lat, lon, f["lat"], f["lon"])
        for f in footprint
        if f.get("lat") is not None
    ]
    return min(dists) if dists else None


# A comma-separated run of capitalized words — the shape of "<Region>,
# <Country>" (or "Town, Region, Country") wherever it sits inside a reply.
# Same structural-capitalization idea as _ANCHOR_WORD: no place vocabulary.
_PLACE_WORD = r"[A-Z][\w'’.-]*"
_PLACE_SPAN = rf"{_PLACE_WORD}(?:\s+{_PLACE_WORD})*"
_REGION_PAIR_RE = re.compile(rf"({_PLACE_SPAN}(?:\s*,\s*{_PLACE_SPAN})+)")
_QUOTED_RE = re.compile(r"[\"“”']([^\"“”']+)[\"“”']")


def _parse_region_hint(raw: str) -> str | None:
    """Extract the "<region>, <country>" payload from a model reply.

    Models differ in discipline: Mistral Large answers bare ("Arunachal
    Pradesh, India"), Nova Lite wraps the same knowledge in prose ('"Ziro"
    most likely refers to "Ziro, Arunachal Pradesh, India".'). Losing the
    payload to wrapping means losing the pin, so extraction is structural,
    in order of confidence:

    1. an explicit NONE anywhere the reply starts → the model declined;
    2. the LAST quoted span containing a comma (models quote the answer);
    3. the LAST capitalized comma-group anywhere in the text ("…refers to
       Ziro, Arunachal Pradesh, India." → "Ziro, Arunachal Pradesh, India");
    4. the first line as-is (a clean bare answer has no wrapping at all).
    """
    def _clean(span: str) -> str | None:
        """Trim wrapping punctuation and cap length; None when nothing
        survives (e.g. a reply of ",,,,"), so the contract stays str | None
        rather than leaking an empty string to callers."""
        return span.strip().strip(" .,;:!?").strip()[:80] or None

    text = (raw or "").strip()
    if not text:
        return None
    first_line = text.splitlines()[0].strip().strip('"').strip()
    if not first_line or first_line.upper().startswith("NONE"):
        return None

    quoted = [m for m in _QUOTED_RE.findall(text) if "," in m]
    if quoted:
        return _clean(quoted[-1])

    pairs = _REGION_PAIR_RE.findall(text)
    if pairs:
        return _clean(pairs[-1])

    return _clean(first_line)


def _llm_region_hint(name: str) -> str | None:
    """LLM's best guess at the "<region>, <country>" a place name denotes.

    Returns None only when the model confidently DECLINES (replies NONE — a
    personal or generic name with no single home), which callers may treat as
    "proximity is the right signal". A transport/throttle failure PROPAGATES
    instead: "we could not ask" must never be conflated with "the model said
    there is nothing to know", or a Bedrock blip silently turns into a
    permanently mis-pinned place.
    """
    from threelane_memory.llm_interface import invoke_llm

    hint = _parse_region_hint(invoke_llm(GEO_REGION_HINT_PROMPT.format(name=name)))
    # Models often echo the asked name as the first segment ("Ziro, India" or
    # "Ziro, Arunachal Pradesh, India"). That echo is not a region — strip it,
    # or the strict region check would compare candidates against the name
    # itself and reject legitimate resolutions.
    if hint and "," in hint:
        head, rest = hint.split(",", 1)
        if head.strip().lower() == name.strip().lower():
            hint = rest.strip() or None
    return hint


def _hit_in_region(hit: dict[str, Any], hint: str, strict: bool = False) -> bool:
    """Whether *hit* sits in the "<region>, <country>" the LLM named.

    Structural comparison of the geocoder's own address fields against the
    hint — no place vocabulary. The hint's country (its last comma segment)
    is always a hard gate when both sides name one, so "Punjab, India" can
    never match a Punjab in Pakistan.

    Two strictness levels for the two roles this check plays:
    • non-strict (agreement: "is the plain hit where the model expected?") —
      country-level agreement suffices. Cross-country namesakes are the
      catastrophic class; within the right country the plain hit is at least
      plausible, and dragging it elsewhere risks more than it fixes.
    • strict (candidate acceptance: "may this qualified retry win?") — when
      the hint names a region, the candidate's region must match it. Country
      alone is NOT enough, else any same-country venue near home ("Ziro" café
      in Pune, India) would slip past the veto meant to stop exactly that.
    """

    def _same(a: str, b: str) -> bool:
        return bool(a and b) and (a in b or b in a)

    hint = hint.strip()
    country = str(hit.get("country") or "").strip().lower()
    region = str(hit.get("region") or "").strip().lower()

    if "," not in hint:
        # Single segment — could be a country OR a region; match either field,
        # no hard gate possible.
        return _same(region, hint.lower()) or _same(country, hint.lower())

    hint_region, hint_country = (part.strip().lower() for part in hint.rsplit(",", 1))

    if country and hint_country and not _same(country, hint_country):
        return False  # both sides name a country and they differ — hard veto

    if strict:
        return _same(region, hint_region)

    return _same(country, hint_country) or _same(region, hint_region)


def _footprint_anchors(name: str, footprint: list[dict[str, Any]]) -> list[str]:
    """The speaker's place names usable to qualify a geocode of *name*
    (deduped, the name itself excluded, capped at GEO_DISAMBIGUATION_MAX_TRIES)."""
    anchors: list[str] = []
    seen: set[str] = set()
    for place in footprint:
        anchor = str(place.get("name") or "").strip()
        if not anchor or anchor.lower() == name.lower() or anchor.lower() in seen:
            continue
        seen.add(anchor.lower())
        anchors.append(anchor)
        if len(anchors) >= GEO_DISAMBIGUATION_MAX_TRIES:
            break
    return anchors


def geocode_place(
    name: str, context_places: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Resolve *name* via Nominatim, arbitrated by knowledge before proximity.

    Two failure modes pull in opposite directions. Nominatim ranks by global
    importance, so an obscure town loses to a famous foreign namesake ("Ziro"
    → Burkina Faso). But the speaker's footprint is a biased magnet: qualify a
    genuine far trip by the home city and any same-named venue near home wins
    on distance ("Goa" → the "Goa" bus stop in Pune, 3 km away). Proximity
    alone cannot tell "ambiguous name needing my city" from "famous place I
    actually traveled to" — knowledge of what the NAME denotes can.

    Order of trust:

    1. Plain hit inside the speaker's world (≤ GEO_DISAMBIGUATION_MAX_KM) —
       keep it. No LLM call, no retries.
    2. Otherwise ask the model for the name's "<region>, <country>":
       • hint AGREES with the plain hit (or is already part of the stored
         name) → a genuine far place; keep it, retry nothing.
       • hint DISAGREES → qualified retries (the speaker's own places first,
         then the hint itself), but only hint-consistent candidates may win;
         among those, nearest to the footprint (per-user disambiguation:
         "City Palace, Jaipur" beats Udaipur's for a Jaipur speaker). A
         hint-consistent result wins even when it is farther than an
         inconsistent one — knowledge vetoes proximity. Every retry answered
         cleanly but nothing consistent exists → keep the plain hit (never
         invent locality). But if any retry ERRORED and nothing consistent
         was found, the disagreement is UNRESOLVED — raise so enrichment
         defers and retries later: an error is not evidence, and persisting
         a hit the model disputes just because the tiebreaker was down is
         how a throttle becomes a permanent wrong pin.
       • hint DECLINED (the model replies NONE — a personal name like "Home"
         with no single real-world home) → legacy footprint disambiguation:
         nearest qualified candidate wins if closer than the plain hit. For
         such names the footprint is in fact the right signal.
       • hint call FAILS (throttle/outage) → the exception propagates. The
         write path already treats any geocode_place failure as transient
         ("retry on next mention"), so enrichment is DEFERRED rather than
         mis-pinned: a place we could not verify stays unenriched instead of
         being permanently cached at the wrong namesake. `reeve geo-backfill`
         sweeps such deferred places too.

    The model only steers queries and vetoes candidates; coordinates always
    come from Nominatim. Everything here is structural — no place vocabulary.
    """
    hit = _geocode_once(name)
    if hit is None:
        return None

    footprint = context_places or []
    plain_km = (
        _min_footprint_km(hit["latitude"], hit["longitude"], footprint)
        if footprint
        else None
    )
    if plain_km is not None and plain_km <= GEO_DISAMBIGUATION_MAX_KM:
        return hit  # already inside the speaker's world — trust it

    hint = _llm_region_hint(name)
    if hint and (hint.lower() in name.lower() or _hit_in_region(hit, hint)):
        return hit  # the name's known home confirms the plain hit — genuine trip

    lookup_errors = 0

    def _qualified(anchor: str) -> dict[str, Any] | None:
        nonlocal lookup_errors
        try:
            return _geocode_once(f"{name}, {anchor}")
        except Exception:
            lookup_errors += 1  # an error is not evidence — track it
            return None

    if hint and "," not in hint:
        # Country-only hint ("Japan"): there is no region to gate candidates
        # with, so the strict veto would degrade to "anything in that country"
        # — and proximity would happily pick a same-named road near home over
        # the real city ("Nara" → a Kyoto street, not Nara). Without a region
        # gate, proximity gets NO vote: qualify by the hint alone and let the
        # geocoder's own within-country ranking decide.
        candidate = _qualified(hint)
        if candidate is not None and _hit_in_region(candidate, hint):
            return candidate
        if lookup_errors:
            raise RuntimeError(
                f"geocode of {name!r} disputed by region hint {hint!r} but "
                f"the qualified lookup failed; deferring"
            )
        return hit  # clean miss — the world offered nothing better

    if hint:
        # Region-ful hint disagrees with the plain hit: hunt for a
        # hint-consistent resolution, preferring the one nearest the
        # speaker's world.
        #
        # The gate differs by where a candidate came from. A candidate found by
        # qualifying with one of the SPEAKER'S OWN places is the hijack vector
        # (a same-named venue near home), so it must land in the hint's region,
        # not merely its country. A candidate found by qualifying with the HINT
        # ITSELF only has to land in the hint's country: the model chose where
        # to look, and the geocoder's admin naming often differs from the
        # model's local one — Iceland's Vík í Mýrdal comes back as "Southern
        # Region" (or no region at all) against a hint of "Mýrdal, Iceland",
        # and rejecting it would strand the pin in Norway.
        best: dict[str, Any] | None = None
        best_km: float | None = None
        anchors = [(a, True) for a in _footprint_anchors(name, footprint)]
        anchors.append((hint, False))
        for anchor, from_footprint in anchors:
            candidate = _qualified(anchor)
            if candidate is None or not _hit_in_region(
                candidate, hint, strict=from_footprint
            ):
                continue  # knowledge veto: near-home namesakes cannot hijack
            cand_km = (
                _min_footprint_km(candidate["latitude"], candidate["longitude"], footprint)
                if footprint
                else None
            )
            if best is None or (
                cand_km is not None and (best_km is None or cand_km < best_km)
            ):
                best, best_km = candidate, cand_km
        if best is not None:
            return best
        if lookup_errors:
            # The model disputes the plain hit and the retries that could have
            # settled it ERRORED (throttle/outage) — unresolved, so defer:
            # the write path retries on the next mention / geo-backfill.
            raise RuntimeError(
                f"geocode of {name!r} disputed by region hint {hint!r} but "
                f"{lookup_errors} qualified lookup(s) failed; deferring"
            )
        return hit  # every retry answered cleanly; the world has nothing better

    # No hint available: legacy proximity-only disambiguation.
    best, best_km = hit, plain_km
    for anchor in _footprint_anchors(name, footprint):
        candidate = _qualified(anchor)
        if candidate is None:
            continue
        cand_km = _min_footprint_km(
            candidate["latitude"], candidate["longitude"], footprint
        )
        if cand_km is not None and (best_km is None or cand_km < best_km):
            best, best_km = candidate, cand_km
    if best is hit and lookup_errors:
        # Same principle as above: the plain hit is far from the speaker's
        # world and the lookups that could have localized it errored —
        # defer rather than persist an unexamined far pin.
        raise RuntimeError(
            f"geocode of {name!r} unresolved: far from footprint and "
            f"{lookup_errors} qualified lookup(s) failed; deferring"
        )
    return best


def _geocode_once(name: str) -> dict[str, Any] | None:
    """Single Nominatim lookup for *name* (no disambiguation)."""
    hits = _geocoder_get(
        "/search",
        {
            "q": name,
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1,
            "accept-language": "en",
        },
    )
    if not isinstance(hits, list) or not hits:
        return None

    hit = hits[0]
    address = hit.get("address") or {}
    return {
        "latitude": float(hit["lat"]),
        "longitude": float(hit["lon"]),
        "display_name": hit.get("display_name"),
        "category": hit.get("type") or hit.get("class"),
        "country": address.get("country"),
        # Countries differ in what they call the first admin level: India ->
        # `state`, Japan/China/Canada/NL -> `province`, others -> `region`.
        # Missing it is not just cosmetic: an empty region makes the strict
        # candidate check in `_hit_in_region` unsatisfiable for those countries.
        "region": (
            address.get("state") or address.get("province") or address.get("region")
        ),
        "city": (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        ),
    }


def reverse_geocode(lat: float, lon: float) -> dict[str, Any] | None:
    """Resolve coordinates (e.g. photo EXIF GPS) to a place name via Nominatim.

    Returns {"name", "country", "display_name"} at roughly city granularity,
    or None when unresolvable. Never raises.
    """
    try:
        data = _geocoder_get(
            "/reverse",
            {
                "lat": lat,
                "lon": lon,
                "format": "jsonv2",
                "zoom": 10,
                "addressdetails": 1,
                "accept-language": "en",
            },
        )
    except Exception as exc:
        logger.info("Reverse geocoding failed for (%s, %s): %s", lat, lon, exc)
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    address = data.get("address") or {}
    name = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("county")
        or address.get("state_district")
        or address.get("state")
    )
    if not name:
        return None
    return {
        "name": str(name),
        "country": address.get("country"),
        "display_name": data.get("display_name"),
    }


def _place_card(name: str, geocode: dict[str, Any]) -> str | None:
    """Ask the LLM for a 1-2 sentence character profile of a geocoded place.

    Runs through ``invoke_llm`` so the tokens land in the request-scoped usage
    accumulator and are metered like every other internal LLM call.
    """
    from threelane_memory.llm_interface import invoke_llm

    context_bits = [
        geocode.get("display_name"),
        geocode.get("category"),
        geocode.get("region"),
        geocode.get("country"),
    ]
    context = ", ".join(str(bit) for bit in context_bits if bit)
    card = invoke_llm(PLACE_CARD_PROMPT.format(name=name, context=context or "none")).strip()
    card = card.strip('"').strip()
    if not card or card.upper().startswith("NONE"):
        return None
    return card[:PLACE_CARD_MAX_CHARS].strip()


def _looks_like_named_place(name: str) -> bool:
    """Whether *name* is a proper-noun place worth geocoding.

    Structural signal, not a word list: a real place the operator extracts is
    capitalized ("Goa", "Osho Garden", "Pune"). Generic scenery the operator
    sometimes emits ("beach", "garden", "forest") is lowercase — and geocoding
    it collides with coincidental same-named towns (a plain "beach" resolves to
    "Beach, North Dakota"). Requiring at least one capitalized word filters
    those out without hardcoding which words are generic.
    """
    return any(word[:1].isupper() for word in name.split())


def _speaker_footprint(speaker: str | None) -> list[dict[str, Any]]:
    """The speaker's other known places — [{"name","lat","lon"}] — used to
    disambiguate a new ambiguous place mention toward their geographic world."""
    if not speaker:
        return []
    try:
        rows = run_query(
            "MATCH (ep:Episode {speaker:$speaker})-[:AT_LOCATION]->(l:Location) "
            "WHERE l.location_point IS NOT NULL "
            "RETURN DISTINCT l.name AS name, l.latitude AS lat, l.longitude AS lon",
            {"speaker": speaker},
        )
    except Exception:
        return []
    return [
        {"name": r["name"], "lat": r["lat"], "lon": r["lon"]}
        for r in rows
        if r.get("lat") is not None
    ]


def get_location_context(name: str, speaker: str | None = None) -> dict[str, Any] | None:
    """Return enrichment context for *speaker*'s *name*, enriching on first sight.

    Location nodes are keyed ``(name, speaker)``, so *speaker* is required to
    address the node; geocoding is disambiguated by that speaker's other known
    places (an ambiguous "City Palace" resolves near their world, not another
    tenant's).

    Returns None when the feature is disabled, the speaker is missing, the name
    is blank/not a named place, or the place could not be resolved. Never
    raises.
    """
    if not GEO_ENRICHMENT_ENABLED or not speaker:
        return None
    name = (name or "").strip()
    if not name or not _looks_like_named_place(name):
        return None
    try:
        return _get_or_enrich(name, speaker)
    except Exception as exc:
        logger.warning("Location enrichment failed for %r: %s", name, exc)
        return None


def regeocode_location(name: str, speaker: str | None = None) -> dict[str, Any] | None:
    """Re-resolve *speaker*'s already-enriched Location node (repair a wrong or
    ambiguous geocode). Clears the cached status so enrichment re-runs with the
    speaker's footprint and returns the fresh context. Never raises."""
    name = (name or "").strip()
    if not name or not speaker or not GEO_ENRICHMENT_ENABLED:
        return None
    try:
        run_query(
            "MATCH (loc:Location {name:$name, speaker:$speaker}) "
            "REMOVE loc.geo_status, loc.geo_checked_at",
            {"name": name, "speaker": speaker},
        )
        return _get_or_enrich(name, speaker)
    except Exception as exc:
        logger.warning("Re-geocode failed for %r: %s", name, exc)
        return None


def _get_or_enrich(name: str, speaker: str) -> dict[str, Any] | None:
    if not speaker:  # Location nodes are keyed (name, speaker); can't MERGE on null
        return None
    rows = run_query(
        "MERGE (loc:Location {name:$name, speaker:$speaker}) RETURN properties(loc) AS props",
        {"name": name, "speaker": speaker},
    )
    props = rows[0].get("props") if rows else {}
    props = props if isinstance(props, dict) else {}

    status = props.get("geo_status")
    if status == GEO_STATUS_ENRICHED:
        return {key: props.get(key) for key in _CONTEXT_KEYS}
    if status == GEO_STATUS_NOT_FOUND:
        return None

    try:
        geocode = geocode_place(name, context_places=_speaker_footprint(speaker))
    except Exception as exc:
        logger.info("Geocoder unavailable for %r (will retry on next mention): %s", name, exc)
        return None

    if geocode is None:
        run_query(
            "MATCH (loc:Location {name:$name, speaker:$speaker}) "
            "SET loc.geo_status = $status, loc.geo_checked_at = datetime()",
            {"name": name, "speaker": speaker, "status": GEO_STATUS_NOT_FOUND},
        )
        return None

    place_card = None
    try:
        place_card = _place_card(name, geocode)
    except Exception as exc:
        logger.info("Place card generation failed for %r: %s", name, exc)

    vibe_embedding = None
    if place_card:
        try:
            from threelane_memory.embeddings import embed

            # Card text only — prefixing the place name drowns the vibe signal
            # (measured: with names, Goa↔Nice scored below Goa↔Manali).
            vibe_embedding = embed(place_card)
        except Exception as exc:
            logger.info("Place card embedding failed for %r: %s", name, exc)

    ensure_location_point_index()
    run_query(
        """
        MATCH (loc:Location {name:$name, speaker:$speaker})
        SET loc.latitude = $latitude,
            loc.longitude = $longitude,
            loc.location_point = point({latitude: $latitude, longitude: $longitude}),
            loc.display_name = $display_name,
            loc.category = $category,
            loc.country = $country,
            loc.region = $region,
            loc.city = $city,
            loc.place_card = $place_card,
            loc.vibe_embedding = $vibe_embedding,
            loc.vibe_embedding_model = $vibe_embedding_model,
            loc.geo_status = $status,
            loc.geo_checked_at = datetime()
        """,
        {
            "name": name,
            "speaker": speaker,
            "latitude": geocode["latitude"],
            "longitude": geocode["longitude"],
            "display_name": geocode.get("display_name"),
            "category": geocode.get("category"),
            "country": geocode.get("country"),
            "region": geocode.get("region"),
            "city": geocode.get("city"),
            "place_card": place_card,
            "vibe_embedding": vibe_embedding,
            "vibe_embedding_model": EMBEDDING_MODEL_VERSION if vibe_embedding else None,
            "status": GEO_STATUS_ENRICHED,
        },
    )
    return {
        "place_card": place_card,
        "latitude": geocode["latitude"],
        "longitude": geocode["longitude"],
        "country": geocode.get("country"),
        "region": geocode.get("region"),
        "city": geocode.get("city"),
        "category": geocode.get("category"),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  RETRIEVAL-SIDE GEO LANE — spatial cues and vibe-similar places
# ══════════════════════════════════════════════════════════════════════════════
#
# The retriever merges ranked candidate "lanes" (temporal, metadata, fulltext,
# vector, recent). ``geo_lane_candidates`` adds a sixth: when the question
# carries an explicit spatial cue, episodes are sparse-filtered by real
# ``point.distance`` geometry (the Spatial-RAG pattern) or, for "places like X",
# ranked by vibe-card similarity — always scoped to the caller's speaker.


@dataclass
class SpatialIntent:
    kind: str  # "near" (distance filter) or "vibe" (similar-place ranking)
    anchor: str  # raw captured span; resolved against the graph, not word lists
    radius_km: float
    allow_geocode: bool = True


_ANCHOR_WORD = r"[A-Za-z][\w'’.-]*"
_ANCHOR = rf"({_ANCHOR_WORD}(?:\s+{_ANCHOR_WORD}){{0,3}})"
_WITHIN_RE = re.compile(
    rf"\bwithin\s+(\d+)\s*(?:km|kms|kilometers|kilometres)\s+(?:of|from)\s+{_ANCHOR}",
    re.IGNORECASE,
)
_VIBE_RE = re.compile(
    rf"\b(?:place|places|somewhere|anywhere|city|cities|town|towns|destination|destinations)\s+"
    rf"(?:like|similar\s+to)\s+{_ANCHOR}",
    re.IGNORECASE,
)
_NEAR_RE = re.compile(rf"\b(?:near|nearby|around|close\s+to)\s+{_ANCHOR}", re.IGNORECASE)
# "in X" is the noisiest cue ("in March", "in college"), so its anchors resolve
# strictly against Location names already in the graph — never the geocoder.
_IN_RE = re.compile(rf"\bin\s+{_ANCHOR}")

def extract_spatial_intent(text: str) -> SpatialIntent | None:
    """Parse an explicit spatial cue; the anchor is the raw captured span.

    Deliberately vocabulary-free: the span is resolved against the graph's own
    Location names in ``_graph_anchor`` (longest matching sub-span wins), so
    the graph — not an English word list — decides what counts as a place.
    Only unambiguous cues (near/within) may fall back to the geocoder; "in X"
    resolves strictly against the graph because "in" is the noisiest
    preposition ("in March", "in college").
    """
    m = _WITHIN_RE.search(text)
    if m:
        return SpatialIntent("near", m.group(2), float(m.group(1)), allow_geocode=True)

    m = _VIBE_RE.search(text)
    if m:
        return SpatialIntent("vibe", m.group(1), 0.0, allow_geocode=False)

    m = _NEAR_RE.search(text)
    if m:
        return SpatialIntent("near", m.group(1), GEO_NEAR_RADIUS_KM, allow_geocode=True)

    m = _IN_RE.search(text)
    if m:
        return SpatialIntent("near", m.group(1), GEO_NEAR_RADIUS_KM, allow_geocode=False)

    return None


def _candidate_spans(raw: str) -> list[str]:
    """Sub-spans of the captured text, longest first.

    Trailing words are shed one at a time ("goa last year" → "goa last" →
    "goa") and one leading word may be dropped ("the beach house" → "beach
    house") — pure structure, no vocabulary.
    """
    words = [w.strip(" ?.!,;:'\"") for w in raw.split()]
    words = [w for w in words if w]
    spans: list[str] = []
    for start in (0, 1):
        for end in range(len(words), start, -1):
            span = " ".join(words[start:end])
            if span and span not in spans:
                spans.append(span)
    spans.sort(key=len, reverse=True)
    return spans


def _graph_anchor(raw_span: str, speaker: str) -> dict[str, Any] | None:
    """Resolve a captured span against *speaker*'s enriched Location names.

    Per-speaker by design: the same name can be a different place per tenant
    ("City Palace" — Udaipur vs Jaipur), so the anchor must carry *this*
    speaker's coordinates, never another tenant's.
    """
    spans = _candidate_spans(raw_span)
    if not spans:
        return None
    rows = run_query(
        "MATCH (l:Location {speaker:$speaker}) WHERE toLower(l.name) IN $cands "
        "  AND l.location_point IS NOT NULL "
        "RETURN l.name AS name, l.latitude AS lat, l.longitude AS lon, "
        "       l.vibe_embedding AS vibe",
        {"speaker": speaker, "cands": [s.lower() for s in spans]},
    )
    if not rows:
        return None
    by_name = {str(r["name"]).lower(): r for r in rows}
    for span in spans:  # longest match wins
        hit = by_name.get(span.lower())
        if hit is not None:
            return hit
    return None


def _leading_proper_span(raw_span: str) -> str | None:
    """The leading run of capitalized words ("Mumbai please" → "Mumbai").

    Structural proper-noun shape check for the geocoder fallback, so free text
    like "near my office" can never geocode to a random real place.
    """
    words = [w.strip(" ?.!,;:'\"") for w in raw_span.split()]
    run: list[str] = []
    for word in words:
        if word and word[0].isupper():
            run.append(word)
        else:
            break
    return " ".join(run) if run else None


def is_distance_query(question: str) -> bool:
    """True when the question is a distance-bounded spatial query ("near X",
    "within N km of X") — as opposed to a vibe query ("places like X").

    For these the distance is a hard filter: only episodes whose place falls
    inside the radius are eligible, and the rest of the question ranks them.
    """
    intent = extract_spatial_intent(question)
    return intent is not None and intent.kind == "near"


def strip_spatial_clause(question: str) -> str:
    """Remove the spatial phrase from a distance query, leaving the semantic
    residual: "which restaurants did I enjoy within 50 km of Pune" →
    "which restaurants did I enjoy". Returns the question unchanged if no
    distance clause is present."""
    for rx in (_WITHIN_RE, _NEAR_RE, _IN_RE):
        m = rx.search(question)
        if m:
            residual = question[: m.start()] + " " + question[m.end() :]
            return " ".join(residual.split()).strip(" ?.!,;:")
    return question.strip()


def distance_filter_ids(question: str, speaker: str, limit: int = 500) -> list[str] | None:
    """The FULL set of the speaker's episodes whose place is within the query's
    radius (the hard distance filter), ordered nearest-first. None when the
    question isn't a distance query or the anchor can't be resolved."""
    if not GEO_ENRICHMENT_ENABLED:
        return None
    intent = extract_spatial_intent(question)
    if intent is None or intent.kind != "near":
        return None
    try:
        coords = _resolve_anchor(intent, speaker)
        if coords is None:
            return None
        return _near_episode_ids(coords[0], coords[1], intent.radius_km, speaker, limit)
    except Exception as exc:
        logger.debug("distance_filter_ids failed for %r: %s", question, exc)
        return None


@lru_cache(maxsize=256)
def _geocode_anchor_cached(name: str) -> tuple[float, float] | None:
    """Geocode a query anchor without persisting anything (query-side only).

    Exceptions deliberately propagate: lru_cache does not memoize a raise, so
    a transient LLM/geocoder failure is retried on the next query instead of
    pinning None for the process lifetime. Callers (the geo lane and the
    distance filter) already catch and degrade to "no geo lane this query".
    """
    hit = geocode_place(name)
    if hit is None:
        return None
    return float(hit["latitude"]), float(hit["longitude"])


def _resolve_anchor(intent: SpatialIntent, speaker: str) -> tuple[float, float] | None:
    """Anchor coordinates: the speaker's own graph first, geocoder fallback."""
    hit = _graph_anchor(intent.anchor, speaker)
    if hit is not None and hit.get("lat") is not None:
        return float(hit["lat"]), float(hit["lon"])
    if not intent.allow_geocode:
        return None
    proper = _leading_proper_span(intent.anchor)
    if not proper:
        return None
    return _geocode_anchor_cached(proper)


def _near_episode_ids(
    lat: float, lon: float, radius_km: float, speaker: str, limit: int
) -> list[str]:
    rows = run_query(
        "MATCH (ep:Episode {speaker:$speaker})-[:AT_LOCATION]->(loc:Location) "
        "WHERE loc.location_point IS NOT NULL AND ep.consolidated_into IS NULL "
        "  AND point.distance(loc.location_point, "
        "      point({latitude:$lat, longitude:$lon})) <= $radius_m "
        "RETURN ep.id AS id "
        "ORDER BY point.distance(loc.location_point, "
        "         point({latitude:$lat, longitude:$lon})) ASC, ep.importance DESC "
        "LIMIT $limit",
        {"speaker": speaker, "lat": lat, "lon": lon,
         "radius_m": radius_km * 1000.0, "limit": limit},
    )
    return [r["id"] for r in rows]


def _vibe_similar_episode_ids(
    anchor: str, anchor_vec: list[float], speaker: str, limit: int
) -> list[str]:
    """Episodes at this speaker's places whose vibe cards are closest to *anchor*'s."""
    from threelane_memory.embeddings import cosine_similarity

    candidates = run_query(
        "MATCH (ep:Episode {speaker:$speaker})-[:AT_LOCATION]->(loc:Location) "
        "WHERE loc.vibe_embedding IS NOT NULL AND toLower(loc.name) <> $n "
        "  AND ep.consolidated_into IS NULL "
        "RETURN DISTINCT loc.name AS name, loc.vibe_embedding AS emb",
        {"speaker": speaker, "n": anchor.lower()},
    )
    scored = sorted(
        ((cosine_similarity(anchor_vec, row["emb"]), row["name"]) for row in candidates),
        reverse=True,
    )
    top_names = [name for sim, name in scored if sim >= GEO_VIBE_MIN_SIMILARITY][:5]
    if not top_names:
        return []

    rows = run_query(
        "MATCH (ep:Episode {speaker:$speaker})-[:AT_LOCATION]->(loc:Location) "
        "WHERE loc.name IN $names AND ep.consolidated_into IS NULL "
        "RETURN ep.id AS id ORDER BY ep.importance DESC LIMIT $limit",
        {"speaker": speaker, "names": top_names, "limit": limit},
    )
    return [r["id"] for r in rows]


def geo_lane_candidates(question: str, speaker: str, limit: int = 12) -> list[tuple[str, float]]:
    """Scored episode candidates for the retriever's geo lane.

    Returns [] fast when the feature is disabled, the question has no spatial
    cue, or anything fails — the lane must never break retrieval.
    """
    if not GEO_ENRICHMENT_ENABLED:
        return []
    intent = extract_spatial_intent(question)
    if intent is None:
        return []
    try:
        if intent.kind == "vibe":
            hit = _graph_anchor(intent.anchor, speaker)
            if hit is None or not hit.get("vibe"):
                return []
            ids = _vibe_similar_episode_ids(str(hit["name"]), hit["vibe"], speaker, limit)
            return [(eid, 1.1) for eid in ids]
        coords = _resolve_anchor(intent, speaker)
        if coords is None:
            return []
        ids = _near_episode_ids(coords[0], coords[1], intent.radius_km, speaker, limit)
        return [(eid, 1.2) for eid in ids]
    except Exception as exc:
        logger.debug("Geo lane failed for %r: %s", question, exc)
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  BACKFILL — enrich Location nodes that predate geo enrichment
# ══════════════════════════════════════════════════════════════════════════════


def refresh_vibe_embeddings(limit: int = 500) -> int:
    """Re-embed the place cards of already-enriched Location nodes.

    Repairs vectors computed under an older embedding formula or model.
    Idempotent; returns the number of nodes refreshed.
    """
    from threelane_memory.embeddings import embed

    # Keyed by elementId, not name: per-speaker copies of the same name can
    # carry different place cards (different actual places), and a name-keyed
    # SET would overwrite them all with one card's vector.
    rows = run_query(
        "MATCH (l:Location) WHERE l.geo_status = $status AND l.place_card IS NOT NULL "
        "RETURN elementId(l) AS id, l.name AS name, l.place_card AS card LIMIT $limit",
        {"status": GEO_STATUS_ENRICHED, "limit": limit},
    )
    refreshed = 0
    for row in rows:
        try:
            vector = embed(row["card"])
        except Exception as exc:
            logger.info("Vibe refresh embedding failed for %r: %s", row["name"], exc)
            continue
        run_query(
            "MATCH (l:Location) WHERE elementId(l) = $id "
            "SET l.vibe_embedding = $vec, l.vibe_embedding_model = $model",
            {"id": row["id"], "vec": vector, "model": EMBEDDING_MODEL_VERSION},
        )
        refreshed += 1
    return refreshed


def backfill_locations(limit: int = 200, dry_run: bool = False) -> dict[str, Any]:
    """Enrich existing never-resolved Location nodes (CLI: ``reeve geo-backfill``).

    Throttled by the geocoder's own 1 req/s pacing. Safe to re-run: enriched and
    not-found nodes are skipped by the geo_status marker, transient failures
    stay unresolved for the next run.

    Also the eager repair path after ``location_speaker_v1``: contested copies
    are stamped with geo_status NULL, so this re-enriches each with its own
    speaker's footprint. Legacy speaker-null orphans are skipped — they are
    unreachable by retrieval and cannot be keyed.
    """
    rows = run_query(
        "MATCH (l:Location) WHERE l.geo_status IS NULL AND l.speaker IS NOT NULL "
        "RETURN l.name AS name, l.speaker AS speaker ORDER BY l.name LIMIT $limit",
        {"limit": limit},
    )
    targets = [(r["name"], r["speaker"]) for r in rows if r.get("name") and r.get("speaker")]
    result: dict[str, Any] = {
        "pending": len(targets),
        "enriched": 0,
        "not_found": 0,
        "unresolved": 0,
        "sample": [name for name, _ in targets[:20]],
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    if not GEO_ENRICHMENT_ENABLED:
        raise RuntimeError("Set GEO_ENRICHMENT_ENABLED=true to run the geo backfill.")

    for name, speaker in targets:
        get_location_context(name, speaker=speaker)
        status_rows = run_query(
            "MATCH (l:Location {name:$n, speaker:$speaker}) RETURN l.geo_status AS s",
            {"n": name, "speaker": speaker},
        )
        status = status_rows[0].get("s") if status_rows else None
        if status == GEO_STATUS_ENRICHED:
            result["enriched"] += 1
        elif status == GEO_STATUS_NOT_FOUND:
            result["not_found"] += 1
        else:
            result["unresolved"] += 1
    return result
