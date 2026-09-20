"""Interactive chat loop – stores memories and answers questions from the graph."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Logging Configuration ────────────────────────────────────────────────────

# Create a file-based logger in DEBUG mode
logger = logging.getLogger("ingestion_logger")
logger.setLevel(logging.DEBUG)
logger.propagate = False

if not any(
    isinstance(handler, logging.FileHandler)
    and Path(getattr(handler, "baseFilename", "")).name == "ingestion_debug.log"
    for handler in logger.handlers
):
    file_handler = logging.FileHandler("ingestion_debug.log", mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

# ── Retrieval + answering ────────────────────────────────────────────────────


def answer_question(question: str, speaker: str) -> str:
    """Retrieve the most relevant subgraph context and answer via LLM."""
    from threelane_memory.llm_interface import invoke_llm
    from threelane_memory.retriever import retrieve

    logger.debug(f"Answering question for speaker '{speaker}': {question}")
    ctx = retrieve(question, speaker=speaker)
    if not ctx.strip():
        logger.debug("No relevant memories found for the question.")
        return "I don't have any relevant memories. Tell me something first!"


    prompt = (
        "You are a personal memory assistant. Use ONLY the memory context below "
        "to answer the user's question. The context is ordered from most to least "
        "relevant; prefer the earliest matching context item when facts conflict. "
        "Answer concisely and do not add unrelated details. If the answer isn't "
        "in the context, say so.\n\n"
        f"Memory Context:\n{ctx}\n\n"
        f"Question: {question}"
    )
    return invoke_llm(prompt)


# ── Intent classification ────────────────────────────────────────────────────


def is_question(text: str) -> bool:
    """Simple heuristic: is the user asking a question or stating a fact?"""
    t = text.strip().lower()
    if t.endswith("?"):
        return True
    starters = (
        "what",
        "who",
        "where",
        "when",
        "why",
        "how",
        "do ",
        "did ",
        "does ",
        "is ",
        "are ",
        "was ",
        "were ",
        "can ",
        "could ",
        "tell me",
        "recall",
        "remember",
        "show me",
    )
    return any(t.startswith(s) for s in starters)


def get_dataset(dataset_path: str | None = None):
    """Load dataset JSON and return event records."""
    default_path = Path(__file__).resolve().parents[1] / "test_dataset" / "arthur_final_v4.json"
    path = Path(dataset_path) if dataset_path else default_path
    logger.debug(f"Loading dataset from: {path}")

    with path.open("r", encoding="utf-8") as f:
        dataset = json.load(f)

    if not isinstance(dataset, list):
        raise ValueError("Dataset root must be a JSON array of events.")

    return dataset


def get_dataset_compat(dataset_path: str | None = None):
    """Backward-compatible wrapper for legacy callers."""
    return get_dataset(dataset_path=dataset_path)


def _normalize_scene_lines(scene_description) -> list[str]:
    """Normalize scene_description into a list of non-empty lines."""
    if not scene_description:
        return []
    if isinstance(scene_description, str):
        line = scene_description.strip()
        return [line] if line else []
    if isinstance(scene_description, list):
        return [str(line).strip() for line in scene_description if str(line).strip()]
    line = str(scene_description).strip()
    return [line] if line else []


def _render_metadata_section(title: str, lines: list[str]) -> str:
    """Render a structured prompt section for ingestion metadata."""
    clean_lines = [str(line).strip() for line in lines if str(line).strip()]
    if not clean_lines:
        return ""
    body = "\n".join(f"- {line}" for line in clean_lines)
    return f"{title}:\n{body}"


def _build_ingestion_text(
    primary_text: str,
    *,
    scene_lines: list[str] | None = None,
    character_lines: list[str] | None = None,
    event_details: list[str] | None = None,
    referenced_time: str | None = None,
    retrospective: bool = False,
) -> str:
    """Build a structured extraction payload with one clear focal account."""
    sections = [f"Primary account:\n{primary_text.strip()}"]

    if scene_lines:
        scene_section = _render_metadata_section("Scene context", scene_lines)
        if scene_section:
            sections.append(scene_section)

    if character_lines:
        character_section = _render_metadata_section("Known characters", character_lines)
        if character_section:
            sections.append(character_section)

    if event_details:
        event_section = _render_metadata_section("Event metadata", event_details)
        if event_section:
            sections.append(event_section)

    temporal_lines = []
    if referenced_time is not None:
        temporal_lines.append(f"Referenced time: {referenced_time}")
    if retrospective:
        temporal_lines.append("Retrospective account: yes")
    if temporal_lines:
        temporal_section = _render_metadata_section("Temporal context", temporal_lines)
        if temporal_section:
            sections.append(temporal_section)

    return "\n\n".join(sections)


def _to_iso_datetime(value, fallback_year: int | None = None) -> str | None:
    """Convert a year/ISO-like input to timezone-aware ISO datetime."""
    if value is None and fallback_year is None:
        return None

    candidate = value if value is not None else fallback_year
    if isinstance(candidate, int):
        return datetime(candidate, 1, 1, tzinfo=timezone.utc).isoformat()

    text = str(candidate).strip()
    if not text:
        if fallback_year is None:
            return None
        return datetime(fallback_year, 1, 1, tzinfo=timezone.utc).isoformat()

    if text.isdigit() and len(text) == 4:
        return datetime(int(text), 1, 1, tzinfo=timezone.utc).isoformat()

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        if fallback_year is not None:
            return datetime(fallback_year, 1, 1, tzinfo=timezone.utc).isoformat()
        return None


def _character_ages(characters: list[object]) -> list[int]:
    """Extract primitive age values from dataset character records."""
    ages: list[int] = []
    for char in characters:
        if not isinstance(char, dict):
            continue
        try:
            ages.append(int(char["age"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ages


def _build_ingestion_entries(event: dict, fallback_speaker: str):
    """Build normalized ingestion entries with source speaker and event-time metadata."""
    event_id = event.get("event_id")
    year = event.get("year")
    timeline = event.get("timeline")
    phase_label = event.get("phase_label")
    title = event.get("title")
    location = event.get("location")
    characters = event.get("characters") or []
    dialogue_list = event.get("dialogue") or []
    scene_lines = _normalize_scene_lines(event.get("scene_description"))
    event_time = _to_iso_datetime(year)
    character_ages = _character_ages(characters)

    # Build character descriptions as metadata lines
    char_lines = []
    for char in characters:
        if isinstance(char, dict):
            name = char.get("name", "Unknown")
            age = char.get("age")
            aliases = char.get("aliases") or []
            char_desc = f"{name}"
            if age is not None:
                char_desc += f" (age {age})"
            if aliases and isinstance(aliases, list):
                clean_aliases = [str(a).strip() for a in aliases if str(a).strip()]
                if clean_aliases:
                    char_desc += f" also known as {', '.join(clean_aliases)}"
            char_lines.append(char_desc)

    # Build event context as structured metadata
    event_details = []
    if event_id:
        event_details.append(f"Event ID: {event_id}")
    if title:
        event_details.append(f"Title: {title}")
    if year:
        event_details.append(f"Year: {year}")
    if timeline:
        event_details.append(f"Timeline: {timeline}")
    if location:
        event_details.append(f"Location: {location}")
    if phase_label:
        event_details.append(f"Phase: {phase_label}")

    # Ingest dialogue if available
    if dialogue_list:
        for dialogue in dialogue_list:
            if not isinstance(dialogue, dict):
                continue
            speaker = str(dialogue.get("speaker") or fallback_speaker).strip() or fallback_speaker
            line = (dialogue.get("line") or "").strip()
            if not line:
                continue
            ref_time = dialogue.get("referenced_time")
            is_retro = dialogue.get("is_retrospective")
            text = _build_ingestion_text(
                line,
                scene_lines=scene_lines,
                character_lines=char_lines,
                event_details=event_details,
                referenced_time=str(ref_time).strip() if ref_time is not None else None,
                retrospective=bool(is_retro),
            )
            yield {
                "event_id": event_id,
                "event_title": title,
                "event_year": year,
                "event_timeline": timeline,
                "event_phase": phase_label,
                "event_location": location,
                "character_ages": character_ages,
                "source_speaker": speaker,
                "text": text,
                "event_time": _to_iso_datetime(ref_time, fallback_year=year)
                if ref_time
                else event_time,
            }
    else:
        # Ingest no-dialogue scenes as one event-level episode so related scene
        # facts stay together for retrieval and answering.
        narrator = str(event.get("narrator") or "narrator").strip() or "narrator"
        scene_text = " ".join(scene_lines).strip()
        if not scene_text:
            return
        text = _build_ingestion_text(
            scene_text,
            character_lines=char_lines,
            event_details=event_details,
        )
        yield {
            "event_id": event_id,
            "event_title": title,
            "event_year": year,
            "event_timeline": timeline,
            "event_phase": phase_label,
            "event_location": location,
            "character_ages": character_ages,
            "source_speaker": narrator,
            "text": text,
            "event_time": event_time,
        }


# ── Main loop ─────────────────────────────────────────────────────────────────


def main(speaker: str = "default", dataset_path: str | None = None) -> None:
    from threelane_memory.backup import save_backup
    from threelane_memory.database import close
    from threelane_memory.entity_dedup import deduplicate_entities
    from threelane_memory.operator import operator_extract
    from threelane_memory.reconciler import consolidate, reconcile

    print("╔══════════════════════════════════════════════╗")
    print("║       Memory Chat  (type 'quit' to exit)    ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  • Tell me facts → stored in the graph      ║")
    print("║  • Ask questions → answered from the graph   ║")
    print("║  • /consolidate  → merge old low-importance  ║")
    print("║  • /backup       → export graph to JSON      ║")
    print("║  • /dedup        → merge duplicate entities   ║")
    print("╚══════════════════════════════════════════════╝\n")

    memory_owner = speaker
    logger.info(f"Starting automated ingestion run for memory owner '{memory_owner}'.")

    # Retry configuration for storing
    max_retries = 3
    retry_delay = 2  # seconds

    try:
        dataset = get_dataset(dataset_path=dataset_path)
        for event in dataset:
            if not isinstance(event, dict):
                logger.warning(f"Skipping malformed event record: {event}")
                continue

            processed = False

            for entry in _build_ingestion_entries(event, fallback_speaker=memory_owner):
                event_id = entry.get("event_id")
                event_title = entry.get("event_title") or "Untitled Event"
                event_year = entry.get("event_year")
                event_timeline = entry.get("event_timeline")
                event_phase = entry.get("event_phase")
                event_location = entry.get("event_location")
                character_ages = entry.get("character_ages")
                source_speaker = entry["source_speaker"]
                text = entry["text"]
                event_time = entry.get("event_time")

                # Keep source-speaker text for extraction quality while
                # storing under one memory owner.
                user_input = f"{source_speaker} said: {text}"
                processed = True

                print(f"You: {user_input}\n")
                logger.debug(
                    "\n"
                    "===== INGESTION ENTRY START =====\n"
                    f"Event ID: {event_id}\n"
                    f"Title: {event_title}\n"
                    f"Owner: {memory_owner}\n"
                    f"Source Speaker: {source_speaker}\n"
                    f"Event Time: {event_time}\n"
                    "Input:\n"
                    f"{user_input}\n"
                    "===== INGESTION ENTRY END ====="
                )

                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit", "q"):
                    print("Bye!")
                    logger.info("Quit command encountered. Exiting.")
                    return  # Exit the function completely instead of just breaking the inner loop

                # ── Slash commands ──
                if user_input.lower() == "/consolidate":
                    print("  🔄 Running consolidation …")
                    try:
                        result = consolidate(memory_owner)
                        if result["merged"]:
                            print(
                                f"  ✅ Merged {result['merged']} episodes → "
                                f"{result['consolidated_episode_id']}"
                            )
                            logger.info(f"Consolidated {result['merged']} episodes.")
                        else:
                            print("  ℹ️  Nothing to consolidate right now.")
                    except Exception as e:
                        print(f"  ❌ Error: {e}")
                        logger.exception("Error during consolidation.")
                    print()
                    continue

                if user_input.lower().startswith("/backup"):
                    print("  📦 Exporting graph …")
                    try:
                        save_backup(speaker=memory_owner)
                        logger.info("Graph backed up successfully.")
                    except Exception as e:
                        print(f"  ❌ Error: {e}")
                        logger.exception("Error during backup.")
                    print()
                    continue

                if user_input.lower() == "/dedup":
                    print("  🔗 Scanning for duplicate entities …")
                    try:
                        result = deduplicate_entities(dry_run=False, speaker=memory_owner)
                        if result["merged"]:
                            print(f"  ✅ Merged {result['merged']} duplicate entity pair(s)")
                            logger.info(f"Deduplicated {result['merged']} entity pairs.")
                        else:
                            print("  ℹ️  No duplicate entities found.")
                    except Exception as e:
                        print(f"  ❌ Error: {e}")
                        logger.exception("Error during deduplication.")
                    print()
                    continue

                if is_question(user_input):
                    # ── Answer mode ──
                    print("  🔍 Searching memory …")
                    try:
                        answer = answer_question(user_input, memory_owner)
                        print(f"  🧠 {answer}\n")
                        logger.debug(f"Answer generated: {answer}")
                    except Exception as e:
                        print(f"  ❌ Error generating answer: {e}\n")
                        logger.exception("Error during question answering.")
                else:
                    # ── Store mode with Retry Logic ──
                    print("  📥 Extracting semantics …")

                    for attempt in range(1, max_retries + 1):
                        try:
                            semantics = operator_extract(user_input)
                            episode_id = reconcile(
                                semantics,
                                speaker=memory_owner,
                                raw_text=user_input,
                                source_speaker=source_speaker,
                                event_time=event_time,
                                event_id=event_id,
                                event_title=event_title,
                                event_year=event_year,
                                event_timeline=event_timeline,
                                event_phase=event_phase,
                                event_location=event_location,
                                character_ages=character_ages,
                            )

                            # Log and print success
                            logger.info(
                                f"Successfully stored episode {episode_id} on attempt {attempt}."
                            )
                            logger.debug(f"Semantics: {semantics}")

                            print(f"  ✅ Stored episode {episode_id}")
                            print(f"     Summary: {semantics['summary']}")
                            print(f"     Entities: {', '.join(semantics['entities'])}")
                            if semantics.get("location"):
                                print(f"     Location: {semantics['location']}")
                            print()

                            # Break out of the retry loop if successful
                            break

                        except Exception as e:
                            logger.error(
                                f"Attempt {attempt}/{max_retries} failed for "
                                f"input: '{user_input}'. Error: {e}"
                            )
                            if attempt < max_retries:
                                logger.debug(f"Retrying in {retry_delay} seconds...")
                                print(
                                    f"  ⚠️ Extraction/Storage failed. "
                                    f"Retrying ({attempt}/{max_retries})..."
                                )
                                time.sleep(retry_delay)
                            else:
                                logger.exception(
                                    f"Failed to store episode after {max_retries} attempts."
                                )
                                print(
                                    f"  ❌ Error: Could not store after {max_retries} "
                                    f"attempts. {e}\n"
                                )

            if not processed:
                logger.debug(f"No ingestible entries found for event_id={event.get('event_id')}")

    except (EOFError, KeyboardInterrupt):
        print("\nBye!")
        logger.info("Process interrupted by user (EOF/KeyboardInterrupt).")
    except Exception as e:
        logger.exception(f"Fatal error in main loop: {e}")
    finally:
        logger.info("Closing database connections.")
        close()


if __name__ == "__main__":
    main()
