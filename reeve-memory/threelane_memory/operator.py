"""GSW-style Semantic Operator – extracts structured event semantics from text."""

import json
import re
import warnings
from datetime import datetime, timezone
from typing import cast

from threelane_memory.schemas import SemanticExtraction

OPERATOR_PROMPT = """\
You are a semantic operator extracting structured event information from a memory record.
The input may be either plain text or a structured ingestion record with sections such as:
- Primary account
- Scene context
- Known characters
- Event metadata
- Temporal context

Return STRICT JSON with the following keys:

{
  "asserts_fact": boolean,
  "summary": string,
  "emotion": string,
  "importance": float (0-1),
  "entities": [string],
  "roles": [{"entity": string, "role": string}],
  "relations": [{"subject": string, "relation": string, "object": string}],
  "actions": [{"actor": string, "verb": string, "object": string|null}],
  "states": [{"entity": string, "attribute": string, "value": string}],
  "location": string|null,
  "time": ISO timestamp string|null
}

CRITICAL RULES:
- Treat `Primary account` as the focal event. The summary should center on that.
- Use `Scene context`, `Known characters`, `Event metadata`, and `Temporal context`
  only to resolve names, place, time, and relationships.
- Do NOT automatically copy every background fact from supporting sections into
  the summary. Include supporting details only when they materially clarify the
  focal event.
- summary must preserve the main action, participants, place, time, and salient
  names or numbers needed to understand the event.
  BAD:  "A person states their age."
  GOOD: "The speaker is 20 years old."
  BAD:  "Someone has a pet."
  GOOD: "Max is the speaker's dog and is 3 years old."
- asserts_fact is false when the record only ASKS for something and states nothing:
  "what is my friend's name", "tell me my friends name", "my friends name",
  "show me the photos I uploaded". These are requests to recall, not things to
  recall. Note the second and third: a request does not need a question mark, or
  even a verb, to be a request.
  asserts_fact is true whenever the record states anything at all about the
  world, however small, INCLUDING when it also asks something:
  "my friend Anant is a classmate, what is his number?" states a fact and asks a
  question, so it is true.
  When genuinely unsure, answer true. A wrongly kept request is noise; a wrongly
  dropped statement is a memory the person never gets back.
  The summaries this file already calls BAD are the tell: if the only summary
  you can write is "the speaker mentions their friend's name", there is no fact
  here and asserts_fact is false.
- entities must be specific named things, not generic words like "person" or "speaker".
  If the user says "my dog Max", entities = ["Max"].
  If the user says "my age is 20", entities = ["speaker"].
- states MUST capture explicit attribute-value pairs that are present in the
  primary account or clearly supplied as supporting metadata.
  "My age is 20" → states: [{"entity": "speaker", "attribute": "age", "value": "20 years old"}]
  "Max is 3 years old" → states: [{"entity": "Max", "attribute": "age", "value": "3 years old"}]
- roles should be meaningful and stable when possible, such as father, mother,
  son, daughter, neighbour, teacher, manager, newborn. Avoid generic scene-only
  labels like holder, held, watcher, person, family member unless the text gives
  no more specific role.
- relations should capture explicit subject-object relationships, especially
  family and neighbour relationships. Use simple relation names such as
  "mother of", "father of", "son of", "neighbour of", "spouse of".
  "William watched his tiny son Arthur" →
    relations: [{"subject": "William Jennings", "relation": "father of",
                             "object": "Arthur Jennings"}]
  "Arthur's mother Margaret held him" →
    relations: [{"subject": "Margaret Jennings", "relation": "mother of",
                             "object": "Arthur Jennings"}]
- actions should capture the central event, not every incidental verb from
  supporting metadata.
- location: a specific NAMED place only — a city, region, country, landmark, or
  named venue (e.g. "Goa", "Pune", "Taj Mahal"). Set location to null for generic
  scenery or place-types that have no proper name (a plain "beach", "garden",
  "forest", or "office" is NOT a location). Prefer the explicit event location
  when one is provided.
- time: prefer explicit referenced time, otherwise explicit event year/time,
  otherwise null.
- importance guidance:
  FIRST, before the scale below: does this record something the speaker will
  need to look up again? Identifiers and codes, room and building numbers,
  deadlines and appointment times, a person's name or how to reach them, a
  decision made, a preference stated, where an object was put, a commitment
  given. If so, score it 0.6-0.8 no matter how undramatic it is, and skip the
  rest of this scale. "My locker code is 4417" has no action, no participants
  and no feeling, and it is the single most useful thing its owner will ever ask
  for. The scale below rates events by how much they matter as a story; that is
  the wrong question for a fact somebody stored on purpose.

  0.1-0.2 = purely incidental filler with no named participant action
  0.35-0.45 = sensory/object detail, atmosphere, secondary character interaction
              (e.g. "Mrs Davies brought a shawl", "Arthur felt the rough blanket")
              NEVER score these below 0.35.
  0.5-0.6 = notable interaction or emotionally meaningful moment
  0.7-0.85 = major change, conflict, or milestone
  0.9-1.0 = life-defining event such as birth, death, marriage, severe diagnosis
- If states[] contains any physical/sensory attribute (texture, sound, temperature,
  smell, visual observation), 0.35 is the MINIMUM importance — never score lower.
  It is a floor, NOT a ceiling: when the underlying event is significant, score
  the event on the scale above. A photograph of a wedding is a wedding, not a
  colour observation.
- A memory beginning with "[Photo]" is a picture the user chose to keep. Judge it
  by WHAT IT DEPICTS using the scale above — a milestone, a person, a trip, or
  just lunch. Being a visual description does not by itself make it incidental.
- When a scene has 3+ named participants each performing an observable action,
  set importance at minimum 0.45.
- When a scene involves multiple participants each performing distinct actions,
  extract ALL of them in actions[] — do not collapse multi-actor scenes to a
  single focal action. Every named participant's observable action must appear.
  BAD:  actions: [{"actor": "Arthur", "verb": "lay", "object": "basket"}]
        (drops Margaret reading and William mending)
  GOOD: actions: [
          {"actor": "Arthur Jennings", "verb": "lay", "object": "basket"},
          {"actor": "Margaret Jennings", "verb": "read", "object": null},
          {"actor": "William Jennings", "verb": "mended", "object": "work coat"}
        ]
- Secondary character interactions (neighbours, visitors, non-family) MUST
  produce their own actions[] and states[] entries. Do not absorb them into
  the primary character's summary only.
  BAD:  summary: "Margaret received a visitor after Arthur's birth."
  GOOD: summary: "Mrs Davies visited the Jennings family after Arthur's birth,
        bringing a knitted shawl. Margaret offered her tea."
        actions: [
          {"actor": "Mrs Davies", "verb": "visited", "object": null},
          {"actor": "Mrs Davies", "verb": "brought", "object": "knitted shawl"},
          {"actor": "Margaret Jennings", "verb": "offered", "object": "cup of tea"}
        ]
- Sensory experiences MUST be captured in states[] using prefixed attributes:
  sensory-sound, sensory-texture, sensory-visual, sensory-smell, sensory-temperature.
  "Arthur heard the distant rumble of coal carts"
  → states: [{"entity": "Arthur Jennings", "attribute": "sensory-sound",
               "value": "distant rumble of coal carts"}]
  "Arthur felt the rough texture of a wool blanket"
  → states: [{"entity": "Arthur Jennings", "attribute": "sensory-texture",
               "value": "rough wool blanket"}]
- Only extract explicit information. Do not hallucinate missing fields.
- emotion must be simple: neutral, happy, sad, stressed, excited, etc.
- Return ONLY valid JSON, no markdown fences, no commentary.
"""


def _fallback_extraction(text: str) -> SemanticExtraction:
    """Return a minimal extraction when model output cannot be parsed."""
    return {
        # True on purpose. Failing to parse the extractor says nothing about
        # whether the record was a statement, and the safe reading of "unknown"
        # is to keep it.
        "asserts_fact": True,
        "summary": text,
        "emotion": "neutral",
        "importance": 0.1,
        "entities": [],
        "roles": [],
        "relations": [],
        "actions": [],
        "states": [],
        "location": None,
        "time": None,
    }


_REFERENCED_TIME_RE = re.compile(r"^\s*-?\s*Referenced time:\s*(.+?)\s*$", re.MULTILINE)
_YEAR_RE = re.compile(r"^\s*-?\s*Year:\s*(\d{4})\s*$", re.MULTILINE)


def _coerce_iso_datetime(value: str | None, fallback_year: int | None = None) -> str | None:
    """Convert a referenced time or year hint into an ISO datetime when possible."""
    if value is None and fallback_year is None:
        return None

    candidate = value.strip() if isinstance(value, str) else value
    if not candidate:
        if fallback_year is None:
            return None
        return datetime(fallback_year, 1, 1, tzinfo=timezone.utc).isoformat()

    if isinstance(candidate, str) and candidate.isdigit() and len(candidate) == 4:
        return datetime(int(candidate), 1, 1, tzinfo=timezone.utc).isoformat()

    try:
        parsed = datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        if fallback_year is not None:
            return datetime(fallback_year, 1, 1, tzinfo=timezone.utc).isoformat()
        return None


def _backfill_structured_time(text: str, extracted_time: str | None) -> str | None:
    """Backfill a missing time field from structured ingestion metadata."""
    if extracted_time:
        return extracted_time

    referenced_match = _REFERENCED_TIME_RE.search(text)
    year_match = _YEAR_RE.search(text)
    fallback_year = int(year_match.group(1)) if year_match else None

    if referenced_match:
        resolved = _coerce_iso_datetime(referenced_match.group(1), fallback_year=fallback_year)
        if resolved:
            return resolved

    if fallback_year is not None:
        return _coerce_iso_datetime(str(fallback_year))

    return None


def operator_extract(text: str) -> tuple[SemanticExtraction, int, int]:
    """Run the semantic operator on *text* and return (extraction, tokens_in, tokens_out)."""
    from threelane_memory.llm_interface import invoke_llm_tracked

    prompt = OPERATOR_PROMPT + f"\n\nText:\n{text}"
    tracked = invoke_llm_tracked(prompt)
    response = tracked["text"]
    tokens_in = tracked["tokens_in"]
    tokens_out = tracked["tokens_out"]

    # Strip markdown fences if the LLM wraps them
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        cleaned = cleaned.rsplit("```", 1)[0]

    decode_errors = []
    data = None
    candidates = [
        cleaned,
        # Local models occasionally over-escape object fragments.
        cleaned.replace('\\"', '"'),
    ]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError as exc:
            decode_errors.append(str(exc))

    if data is None:
        warnings.warn(
            "Operator output was not valid JSON; falling back to a minimal extraction. "
            + " | ".join(decode_errors),
            RuntimeWarning,
            stacklevel=2,
        )
        return _fallback_extraction(text), tokens_in, tokens_out

    # ── Normalize: guard against local LLMs returning null for list fields ──
    for key in ("entities", "roles", "relations", "actions", "states"):
        if not data.get(key) or not isinstance(data.get(key), list):
            data[key] = []
    # Absent, null or non-boolean all mean "keep it". Only an explicit false
    # drops a record from memory, so a model that ignores the field, or a local
    # model that cannot follow the schema, costs noise rather than data.
    data["asserts_fact"] = data.get("asserts_fact") is not False
    if not data.get("summary"):
        data["summary"] = text
    if not data.get("emotion"):
        data["emotion"] = "neutral"
    if data.get("importance") is None:
        data["importance"] = 0.5
    data.setdefault("location", None)
    data.setdefault("time", None)
    data["time"] = _backfill_structured_time(text, data.get("time"))

    return cast(SemanticExtraction, data), tokens_in, tokens_out
