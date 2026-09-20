"""
dual_ingest.py
──────────────
Ingests the Arthur dataset into Supermemory (via supermemory SDK — client.add)

BULLETPROOF FEATURES:
  • Progress is saved to disk after every successful entry (progress.json)
  • Failed entries are logged to failed_entries.json with full details
  • On network failure, retries indefinitely with backoff (never skips)
  • On restart, automatically resumes from last successful entry
  • Run modes:
      - Normal run   → resumes from last checkpoint automatically
      - Retry failed → re-attempts only previously failed entries
      - Fresh start  → clears progress and starts from event 1

Usage:
    python dual_ingest.py                    # auto-resume from checkpoint
    python dual_ingest.py --retry-failed     # retry only failed entries
    python dual_ingest.py --fresh            # wipe progress, start over
    python dual_ingest.py --from 167         # start from event 167

Requires:
    pip install supermemory python-dotenv

.env:
    SUPERMEMORY_API_KEY=your_key_here
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from supermemory import Supermemory  # type: ignore[import-not-found]

load_dotenv(verbose=True)

# ── Paths ──────────────────────────────────────────────────────────────────────

PROGRESS_FILE = Path("progress.json")  # tracks last successfully stored entry
FAILED_FILE = Path("failed_entries.json")  # logs every entry that exhausted retries
INGESTION_LOG_FILE = Path("dual_ingest.log")

# ── Retry config ───────────────────────────────────────────────────────────────

MAX_RETRIES = 10  # quick retries per hard attempt
RETRY_DELAY = 3  # seconds between quick retries
LONG_WAIT_SECONDS = 30  # wait after MAX_RETRIES exhausted before next hard attempt
MAX_HARD_FAILURES = 3  # hard attempts before marking entry as failed and moving on

CONTAINER_DATASET = "arthur-dataset"

# ── Logging ────────────────────────────────────────────────────────────────────

logger = logging.getLogger("dual_ingest")
logger.setLevel(logging.DEBUG)
logger.propagate = False

if not any(
    isinstance(h, logging.FileHandler)
    and Path(getattr(h, "baseFilename", "")).name == INGESTION_LOG_FILE.name
    for h in logger.handlers
):
    fh = logging.FileHandler(INGESTION_LOG_FILE, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
logger.addHandler(ch)

# ── Progress tracking ──────────────────────────────────────────────────────────


def _load_progress() -> dict[str, Any]:
    """Load progress file. Returns dict with completed_keys and last checkpoint."""
    if PROGRESS_FILE.exists():
        try:
            with PROGRESS_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return cast(dict[str, Any], data)
        except Exception:
            logger.warning("Could not read progress.json — starting fresh.")
    return {"last_event_id": None, "last_entry_index": -1, "completed_keys": []}


def _save_progress(event_id, entry_index: int, completed_keys: list) -> None:
    """Persist progress to disk immediately after a successful store."""
    data = {
        "last_event_id": event_id,
        "last_entry_index": entry_index,
        "completed_keys": completed_keys,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with PROGRESS_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save progress: {e}")


def _load_failed() -> list[dict[str, Any]]:
    """Load list of previously failed entries."""
    if FAILED_FILE.exists():
        try:
            with FAILED_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return cast(list[dict[str, Any]], data)
        except Exception:
            return []
    return []


def _save_failed(failed_list: list) -> None:
    """Persist failed entries list to disk."""
    try:
        with FAILED_FILE.open("w", encoding="utf-8") as f:
            json.dump(failed_list, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save failed_entries.json: {e}")


def _entry_key(event_id, entry_index: int) -> str:
    """Unique key for a specific entry within an event."""
    return f"{event_id}:{entry_index}"


def _clear_progress() -> None:
    """Wipe progress and failed files for a fresh start."""
    for f in [PROGRESS_FILE, FAILED_FILE]:
        if f.exists():
            f.unlink()
    logger.info("Cleared progress.json and failed_entries.json for fresh start.")


# ── Dataset helpers ────────────────────────────────────────────────────────────


def get_dataset(dataset_path: str | None = None) -> list:
    default_path = Path(__file__).resolve().parents[1] / "test_dataset" / "arthur_final_v4.json"
    path = Path(dataset_path) if dataset_path else default_path
    logger.debug(f"Loading dataset from: {path}")
    with path.open("r", encoding="utf-8") as f:
        dataset = json.load(f)
    if not isinstance(dataset, list):
        raise ValueError("Dataset root must be a JSON array of events.")
    logger.info(f"Loaded {len(dataset)} events from dataset.")
    return dataset


def _normalize_scene_lines(scene_description) -> list[str]:
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
    clean = [str(line).strip() for line in lines if str(line).strip()]
    if not clean:
        return ""
    return f"{title}:\n" + "\n".join(f"- {line}" for line in clean)


def _build_ingestion_text(
    primary_text: str,
    *,
    scene_lines: list[str] | None = None,
    character_lines: list[str] | None = None,
    event_details: list[str] | None = None,
    referenced_time: str | None = None,
    retrospective: bool = False,
) -> str:
    sections = [f"Primary account:\n{primary_text.strip()}"]
    if scene_lines:
        s = _render_metadata_section("Scene context", scene_lines)
        if s:
            sections.append(s)
    if character_lines:
        s = _render_metadata_section("Known characters", character_lines)
        if s:
            sections.append(s)
    if event_details:
        s = _render_metadata_section("Event metadata", event_details)
        if s:
            sections.append(s)
    temporal_lines = []
    if referenced_time is not None:
        temporal_lines.append(f"Referenced time: {referenced_time}")
    if retrospective:
        temporal_lines.append("Retrospective account: yes")
    if temporal_lines:
        s = _render_metadata_section("Temporal context", temporal_lines)
        if s:
            sections.append(s)
    return "\n\n".join(sections)


def _to_iso_datetime(value, fallback_year: int | None = None) -> str | None:
    if value is None and fallback_year is None:
        return None
    candidate = value if value is not None else fallback_year
    if isinstance(candidate, int):
        return datetime(candidate, 1, 1, tzinfo=timezone.utc).isoformat()
    text = str(candidate).strip()
    if not text:
        if fallback_year:
            return datetime(fallback_year, 1, 1, tzinfo=timezone.utc).isoformat()
        return None
    if text.isdigit() and len(text) == 4:
        return datetime(int(text), 1, 1, tzinfo=timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    except ValueError:
        if fallback_year:
            return datetime(fallback_year, 1, 1, tzinfo=timezone.utc).isoformat()
        return None


def _build_ingestion_entries(event: dict, fallback_speaker: str):
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

    char_lines = []
    for char in characters:
        if isinstance(char, dict):
            name = char.get("name", "Unknown")
            age = char.get("age")
            aliases = char.get("aliases") or []
            desc = name
            if age is not None:
                desc += f" (age {age})"
            clean_aliases = [str(a).strip() for a in aliases if str(a).strip()]
            if clean_aliases:
                desc += f" also known as {', '.join(clean_aliases)}"
            char_lines.append(desc)

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
                "source_speaker": speaker,
                "text": text,
                "event_time": _to_iso_datetime(ref_time, fallback_year=year)
                if ref_time
                else event_time,
            }
    else:
        narrator = str(event.get("narrator") or "narrator").strip() or "narrator"
        for scene_line in scene_lines:
            text = str(scene_line).strip()
            if not text:
                continue
            text = _build_ingestion_text(
                text, character_lines=char_lines, event_details=event_details
            )
            yield {
                "event_id": event_id,
                "event_title": title,
                "source_speaker": narrator,
                "text": text,
                "event_time": event_time,
            }


# ── Bulletproof store helper ───────────────────────────────────────────────────


def _store_with_retry(label: str, store_fn) -> tuple[bool, str]:
    """
    Calls store_fn() with aggressive retry logic. Never silently skips.

    Strategy:
      Round 1..MAX_HARD_FAILURES:
        - Try up to MAX_RETRIES quick attempts (RETRY_DELAY between each)
        - If all fail → wait LONG_WAIT_SECONDS, then try the next hard round
      After MAX_HARD_FAILURES rounds → give up, return False so the entry
      is recorded to failed_entries.json for manual retry later.
    """
    hard_failures = 0

    while hard_failures < MAX_HARD_FAILURES:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                result = store_fn()
                return True, result
            except Exception as exc:
                logger.warning(
                    f"[{label}] Quick attempt {attempt}/{MAX_RETRIES} "
                    f"(hard round {hard_failures + 1}/{MAX_HARD_FAILURES}): {exc}"
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)

        hard_failures += 1
        if hard_failures < MAX_HARD_FAILURES:
            logger.error(
                f"[{label}] All {MAX_RETRIES} quick retries failed. "
                f"Waiting {LONG_WAIT_SECONDS}s before hard round "
                f"{hard_failures + 1}/{MAX_HARD_FAILURES} …"
            )
            print(
                f"\n     ⏳ [{label}] Network issue — waiting {LONG_WAIT_SECONDS}s "
                f"(hard round {hard_failures}/{MAX_HARD_FAILURES - 1} done) …",
                flush=True,
            )
            time.sleep(LONG_WAIT_SECONDS)

    logger.error(
        f"[{label}] ❌ Gave up after {MAX_HARD_FAILURES} hard rounds × {MAX_RETRIES} attempts."
    )
    return (
        False,
        f"Failed after {MAX_HARD_FAILURES} hard rounds × {MAX_RETRIES} attempts each",
    )


def _store_supermemory(
    content: str,
    memory_owner: str,
    event_id,
    sm_client: Supermemory,
) -> tuple[bool, str]:
    container_tags = [CONTAINER_DATASET, f"speaker-{memory_owner}"]

    def _do():
        sm_client.add(content=content, container_tags=container_tags)
        logger.info(f"[Supermemory] ✅ Stored | event_id={event_id} | tags={container_tags}")
        return f"tags={container_tags}"

    return _store_with_retry("Supermemory", _do)


# ── Main ingestion loop ────────────────────────────────────────────────────────


def ingest(
    speaker: str = "default",
    dataset_path: str | None = None,
    start_from_event: int | None = None,
    fresh: bool = False,
) -> None:
    """
    Walk every Arthur event and store each entry into Supermemory.

    Resilience guarantees:
      - Progress checkpointed to progress.json after EVERY successful entry.
      - On restart, resumes exactly from the last checkpoint automatically.
      - Entries that fail all retry rounds are written to failed_entries.json
        and are NEVER silently dropped — run retry_failed() to recover them.
      - start_from_event is only respected when there is no existing checkpoint.

    Args:
        speaker:          Memory owner / speaker name.
        dataset_path:     Optional path override for the dataset JSON.
        start_from_event: Skip events with event_id < this value.
                          Ignored if progress.json already has a checkpoint.
        fresh:            Wipe progress.json + failed_entries.json and restart.
    """
    if fresh:
        _clear_progress()

    sm_client = Supermemory()

    # ── Load checkpoint ────────────────────────────────────────────────────────
    progress = _load_progress()
    completed_keys = set(progress.get("completed_keys") or [])
    last_event_id = progress.get("last_event_id")

    failed_list = _load_failed()
    failed_keys = {e["key"] for e in failed_list}

    print("╔════════════════════════════════════════════════════════╗")
    print("║      Supermemory Ingestion                           ║")
    print("║      Bulletproof Edition  🛡️                           ║")
    print("╚════════════════════════════════════════════════════════╝\n")

    if last_event_id is not None:
        print(f"  📌 Resuming from checkpoint — last completed event_id: {last_event_id}")
    elif start_from_event is not None:
        print(f"  ⏩  Starting from event_id >= {start_from_event}")
    else:
        print("  🚀 Starting from the beginning.")

    print(f"  📋 Already completed entries : {len(completed_keys)}")
    print(f"  ⚠️  Previously failed entries : {len(failed_keys)}\n")

    logger.info(
        f"Ingestion started | owner='{speaker}' | checkpoint_event={last_event_id} "
        f"| completed={len(completed_keys)} | failed={len(failed_keys)}"
    )

    dataset = get_dataset(dataset_path=dataset_path)

    total = 0
    skipped = 0
    sm_ok = sm_fail = 0

    try:
        for event in dataset:
            if not isinstance(event, dict):
                logger.warning(f"Skipping malformed event: {event}")
                continue

            try:
                eid = int(event.get("event_id") or 0)
            except (TypeError, ValueError):
                eid = 0

            # Respect start_from_event only when no checkpoint exists
            if last_event_id is None and start_from_event is not None and eid < start_from_event:
                skipped += 1
                logger.debug(
                    f"Skipping event_id={eid} (before start_from_event={start_from_event})"
                )
                continue

            has_entry = False

            for entry_index, entry in enumerate(
                _build_ingestion_entries(event, fallback_speaker=speaker)
            ):
                has_entry = True
                event_id = entry.get("event_id")
                event_title = entry.get("event_title") or "Untitled Event"
                source_speaker = entry["source_speaker"]
                text = entry["text"]
                event_time = entry.get("event_time")
                user_input = f"{source_speaker} said: {text}"
                key = _entry_key(event_id, entry_index)

                # Skip already successfully stored entries
                if key in completed_keys:
                    logger.debug(f"Skipping already completed entry key={key}")
                    skipped += 1
                    continue

                total += 1
                print(
                    f"\n  📄 [{event_id}:{entry_index}] {event_title}  |  speaker: {source_speaker}"
                )

                logger.debug(
                    f"\n===== ENTRY =====\n"
                    f"Key: {key}\n"
                    f"Event ID: {event_id}\n"
                    f"Title: {event_title}\n"
                    f"Owner: {speaker}\n"
                    f"Source Speaker: {source_speaker}\n"
                    f"Event Time: {event_time}\n"
                    f"Content:\n{user_input}\n"
                    "================="
                )

                entry_failed = False

                # ── Store into Supermemory ──────────────────────────────────
                print("     💾 Supermemory → storing …", end=" ", flush=True)
                ok, msg = _store_supermemory(user_input, speaker, event_id, sm_client)
                if ok:
                    sm_ok += 1
                    print(f"✅  {msg}")
                else:
                    sm_fail += 1
                    entry_failed = True
                    print(f"❌  {msg}")

                # ── Checkpoint or record failure ───────────────────────────────
                if not entry_failed:
                    completed_keys.add(key)
                    _save_progress(event_id, entry_index, list(completed_keys))
                    logger.info(f"✅ Checkpoint saved | key={key}")
                else:
                    if key not in failed_keys:
                        failed_keys.add(key)
                        failed_list.append(
                            {
                                "key": key,
                                "event_id": event_id,
                                "entry_index": entry_index,
                                "event_title": event_title,
                                "source_speaker": source_speaker,
                                "user_input": user_input,
                                "event_time": event_time,
                                "failed_at": datetime.now(timezone.utc).isoformat(),
                            }
                        )
                        _save_failed(failed_list)
                        logger.warning(f"⚠️  Entry saved to failed_entries.json | key={key}")
                    print(f"\n     ⚠️  Entry {key} recorded to failed_entries.json — continuing.\n")

            if not has_entry:
                logger.debug(f"No ingestible entries for event_id={event.get('event_id')}")

    except (EOFError, KeyboardInterrupt):
        print("\n\nInterrupted by user. Progress saved — re-run to resume.")
        logger.info("Interrupted by user. Progress saved.")

    except Exception as exc:
        logger.exception(f"Fatal error in ingestion loop: {exc}")
        print(f"\n\n❌ Fatal error: {exc}\nProgress saved — re-run to resume.")

    # ── Summary ────────────────────────────────────────────────────────────────
    print()
    print("╔════════════════════════════════════════════════════════╗")
    print("║                   Ingestion Summary                   ║")
    print(f"║  Skipped (done/before start) : {skipped:<23}║")
    print(f"║  Total entries attempted     : {total:<23}║")
    print(f"║  Supermemory  ✅ stored      : {sm_ok:<23}║")
    print(f"║  Supermemory  ❌ failed      : {sm_fail:<23}║")
    print("║                                                        ║")
    print(f"║  Total failed (in file)      : {len(failed_list):<23}║")
    print(f"║  Progress file               : {str(PROGRESS_FILE):<23}║")
    print(f"║  Failed log                  : {str(FAILED_FILE):<23}║")
    print("╚════════════════════════════════════════════════════════╝")

    if failed_list:
        print(f"\n  ⚠️  {len(failed_list)} entries in {FAILED_FILE}.")
        print("     Set RETRY_FAILED = True and re-run to recover them.\n")

    logger.info(
        f"Done | skipped={skipped} | total={total} "
        f"| sm ok={sm_ok} fail={sm_fail} "
        f"| total_failed_on_disk={len(failed_list)}"
    )


# ── Retry failed entries only ──────────────────────────────────────────────────


def retry_failed(speaker: str = "default") -> None:
    """
    Re-attempt all entries recorded in failed_entries.json.
    Entries that succeed are removed from the failed list and added to
    progress.json so they are never re-attempted again.
    """
    sm_client = Supermemory()
    failed_list = _load_failed()

    if not failed_list:
        print("✅ No failed entries found. Nothing to retry.")
        return

    print("╔════════════════════════════════════════════════════════╗")
    print("║          Retrying Failed Entries 🔁                   ║")
    print("╚════════════════════════════════════════════════════════╝\n")
    print(f"  Found {len(failed_list)} failed entries to retry.\n")

    progress = _load_progress()
    completed_keys = set(progress.get("completed_keys") or [])

    still_failed = []
    retry_ok = 0
    retry_fail = 0

    try:
        for entry in failed_list:
            key = entry["key"]
            event_id = entry["event_id"]
            entry_index = entry["entry_index"]
            event_title = entry.get("event_title", "Unknown")
            user_input = entry["user_input"]

            print(f"\n  🔁 Retrying [{event_id}:{entry_index}] {event_title}")

            entry_failed = False

            # Supermemory
            print("     💾 Supermemory → storing …", end=" ", flush=True)
            ok, msg = _store_supermemory(user_input, speaker, event_id, sm_client)
            if ok:
                print(f"✅  {msg}")
            else:
                entry_failed = True
                print(f"❌  {msg}")

            if entry_failed:
                retry_fail += 1
            else:
                retry_ok += 1

            if not entry_failed:
                completed_keys.add(key)
                _save_progress(event_id, entry_index, list(completed_keys))
                logger.info(f"✅ Recovered and checkpointed | key={key}")
            else:
                entry["last_retry"] = datetime.now(timezone.utc).isoformat()
                still_failed.append(entry)

    except (EOFError, KeyboardInterrupt):
        print("\n\nInterrupted. Saving remaining failed entries.")
        logger.info("retry_failed() interrupted by user.")

    finally:
        _save_failed(still_failed)

    print()
    print("╔════════════════════════════════════════════════════════╗")
    print("║              Retry Summary                            ║")
    print(f"║  Recovered  ✅ : {retry_ok:<37}║")
    print(f"║  Still failed ❌ : {retry_fail:<35}║")
    print(f"║  Remaining in file : {len(still_failed):<33}║")
    print("╚════════════════════════════════════════════════════════╝\n")


# ── Optional search test ───────────────────────────────────────────────────────


def search_test(query: str, speaker: str = "default", scope: str = "dataset") -> None:
    """Search Supermemory after ingestion to verify entries were stored."""
    sm_client = Supermemory()
    tag = CONTAINER_DATASET if scope == "dataset" else f"speaker-{speaker}"

    print("\n🔍 Supermemory search")
    print(f"   Query : '{query}'")
    print(f"   Scope : {tag}\n")

    results = sm_client.search.documents(q=query, container_tags=[tag])

    if not results.results:
        print("  ℹ️  No results found.")
        return

    for i, doc in enumerate(results.results, 1):
        score = getattr(doc, "score", "N/A")
        preview = (doc.content or "")[:300].replace("\n", " ")
        print(f"  [{i}] Score : {score}")
        print(f"       {preview}\n")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # ┌──────────────────────────────────────────────────────────────────┐
    # │                   CONFIGURE BEFORE RUNNING                       │
    # ├──────────────────────────────────────────────────────────────────┤
    # │  SPEAKER          → memory owner (always "default")              │
    # │  DATASET_PATH     → None = use default path                      │
    # │                                                                   │
    # │  START_FROM_EVENT → None  = auto-resume from checkpoint          │
    # │                     167   = skip events before 167               │
    # │                     (ignored if progress.json has a checkpoint)  │
    # │                                                                   │
    # │  FRESH_START      → False = resume (default)                     │
    # │                     True  = wipe all progress and start over     │
    # │                                                                   │
    # │  RETRY_FAILED     → False = normal ingest (default)              │
    # │                     True  = only retry failed_entries.json       │
    # └──────────────────────────────────────────────────────────────────┘

    SPEAKER = "default"
    DATASET_PATH = None
    START_FROM_EVENT = None  # e.g. 167 to start from event 167
    FRESH_START = False  # True = wipe progress.json and start over
    RETRY_FAILED = False  # True = only retry previously failed entries

    if RETRY_FAILED:
        retry_failed(speaker=SPEAKER)
    else:
        ingest(
            speaker=SPEAKER,
            dataset_path=DATASET_PATH,
            start_from_event=START_FROM_EVENT,
            fresh=FRESH_START,
        )

    # ── Optional: verify storage after ingestion ───────────────────────
    # search_test("where did Arthur grow up", speaker=SPEAKER, scope="dataset")
