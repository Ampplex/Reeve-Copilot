"""
reeve_ingest.py
---------------
Resilient Arthur dataset ingestion for Reeve only.

This is the Reeve-side counterpart to dual_ingest.py's checkpoint/retry flow:
  - Progress is saved after every successfully stored entry.
  - Failed entries are written to a retry file with their full payload.
  - Network/model/database failures are retried in quick and hard rounds.
  - Re-runs resume from the checkpoint without duplicating completed entries.

Usage:
    python -m threelane_memory.reeve_ingest
    python -m threelane_memory.reeve_ingest --fresh
    python -m threelane_memory.reeve_ingest --retry-failed
    python -m threelane_memory.reeve_ingest --from 167
    python -m threelane_memory.reeve_ingest --dataset test_dataset/arthur_final_v4.json
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from threelane_memory.ingestion import _build_ingestion_entries, get_dataset

load_dotenv(verbose=True)

# Paths are intentionally separate from the Supermemory dual_ingest files and
# pinned to the project root so they do not move with the shell's cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROGRESS_FILE = PROJECT_ROOT / "reeve_progress.json"
FAILED_FILE = PROJECT_ROOT / "reeve_failed_entries.json"
INGESTION_LOG_FILE = PROJECT_ROOT / "reeve_ingest.log"

# Retry config mirrors dual_ingest.py.
MAX_RETRIES = 10
RETRY_DELAY = 3
LONG_WAIT_SECONDS = 30
MAX_HARD_FAILURES = 3

logger = logging.getLogger("reeve_ingest")
logger.setLevel(logging.DEBUG)
logger.propagate = False

if not any(
    isinstance(handler, logging.FileHandler)
    and Path(getattr(handler, "baseFilename", "")).name == INGESTION_LOG_FILE.name
    for handler in logger.handlers
):
    file_handler = logging.FileHandler(INGESTION_LOG_FILE, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(file_handler)

if not any(type(handler) is logging.StreamHandler for handler in logger.handlers):
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    logger.addHandler(stream_handler)


def _load_progress() -> dict:
    """Load checkpoint state from disk."""
    if PROGRESS_FILE.exists():
        try:
            with PROGRESS_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                data.setdefault("completed_keys", [])
                data.setdefault("last_event_id", None)
                data.setdefault("last_entry_index", -1)
                return data
        except Exception as exc:
            logger.warning("Could not read %s: %s", PROGRESS_FILE, exc)
    return {"last_event_id": None, "last_entry_index": -1, "completed_keys": []}


def _save_progress(event_id, entry_index: int, completed_keys: list[str]) -> None:
    """Persist checkpoint state after a successful Reeve store."""
    payload = {
        "last_event_id": event_id,
        "last_entry_index": entry_index,
        "completed_keys": sorted(completed_keys),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with PROGRESS_FILE.open("w", encoding="utf-8") as file:
            json.dump(payload, file, indent=2)
    except Exception as exc:
        logger.error("Could not save %s: %s", PROGRESS_FILE, exc)


def _load_failed() -> list[dict]:
    """Load failed-entry records."""
    if FAILED_FILE.exists():
        try:
            with FAILED_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Could not read %s: %s", FAILED_FILE, exc)
    return []


def _save_failed(failed_entries: list[dict]) -> None:
    """Persist failed-entry records."""
    try:
        with FAILED_FILE.open("w", encoding="utf-8") as file:
            json.dump(failed_entries, file, indent=2)
    except Exception as exc:
        logger.error("Could not save %s: %s", FAILED_FILE, exc)


def _entry_key(event_id, entry_index: int) -> str:
    return f"{event_id}:{entry_index}"


def _clear_progress() -> None:
    for path in (PROGRESS_FILE, FAILED_FILE):
        if path.exists():
            path.unlink()
    logger.info("Cleared %s and %s for fresh Reeve ingest.", PROGRESS_FILE, FAILED_FILE)


def _store_with_retry(label: str, store_fn: Callable[[], str]) -> tuple[bool, str]:
    """Call store_fn with quick retries and hard backoff rounds."""
    hard_failures = 0

    while hard_failures < MAX_HARD_FAILURES:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return True, store_fn()
            except Exception as exc:
                logger.warning(
                    "[%s] Quick attempt %s/%s (hard round %s/%s) failed: %s",
                    label,
                    attempt,
                    MAX_RETRIES,
                    hard_failures + 1,
                    MAX_HARD_FAILURES,
                    exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)

        hard_failures += 1
        if hard_failures < MAX_HARD_FAILURES:
            logger.error(
                "[%s] All quick retries failed. Waiting %ss before hard round %s/%s.",
                label,
                LONG_WAIT_SECONDS,
                hard_failures + 1,
                MAX_HARD_FAILURES,
            )
            print(
                f"\n     Waiting {LONG_WAIT_SECONDS}s before retrying {label} "
                f"(hard round {hard_failures}/{MAX_HARD_FAILURES - 1} done) ...",
                flush=True,
            )
            time.sleep(LONG_WAIT_SECONDS)

    message = f"Failed after {MAX_HARD_FAILURES} hard rounds x {MAX_RETRIES} attempts each"
    logger.error("[%s] %s", label, message)
    return False, message


def _store_reeve(
    user_input: str,
    memory_owner: str,
    source_speaker: str,
    event_time: str | None,
    event_id,
    event_title: str | None,
    event_year: int | None,
    event_timeline: str | None,
    event_phase: str | None,
    event_location: str | None,
    character_ages: list[int] | None,
) -> tuple[bool, str]:
    """Extract semantics and store one memory in Reeve with retry wrapping."""
    from threelane_memory.operator import operator_extract
    from threelane_memory.reconciler import reconcile

    def _do_store() -> str:
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
        summary = str(semantics.get("summary") or "").replace("\n", " ")
        logger.info(
            "[Reeve] Stored episode %s | event_id=%s | summary=%s",
            episode_id,
            event_id,
            summary,
        )
        return f"episode_id={episode_id} | summary={summary[:180]}"

    return _store_with_retry("Reeve", _do_store)


def _failed_record(entry: dict, key: str, user_input: str) -> dict:
    return {
        "key": key,
        "event_id": entry.get("event_id"),
        "entry_index": entry.get("entry_index"),
        "event_title": entry.get("event_title"),
        "event_year": entry.get("event_year"),
        "event_timeline": entry.get("event_timeline"),
        "event_phase": entry.get("event_phase"),
        "event_location": entry.get("event_location"),
        "character_ages": entry.get("character_ages"),
        "source_speaker": entry.get("source_speaker"),
        "user_input": user_input,
        "event_time": entry.get("event_time"),
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }


def _preflight() -> None:
    """Run lightweight checks that do not block ingestion."""
    try:
        from threelane_memory.database import (
            check_index_dimension,
            ensure_episode_fulltext_index,
            ensure_entity_vector_index,
        )

        check_index_dimension()
        ensure_episode_fulltext_index(quiet=True)
        ensure_entity_vector_index()
    except Exception as exc:
        logger.warning("Preflight check failed; continuing with retries: %s", exc)


def ingest(
    speaker: str = "default",
    dataset_path: str | None = None,
    start_from_event: int | None = None,
    fresh: bool = False,
) -> None:
    """Ingest the Arthur dataset into Reeve with checkpointed retry behavior."""
    from threelane_memory.database import close

    if fresh:
        _clear_progress()

    _preflight()

    progress = _load_progress()
    completed_keys = set(progress.get("completed_keys") or [])
    last_event_id = progress.get("last_event_id")

    failed_entries = _load_failed()
    failed_keys = {entry.get("key") for entry in failed_entries}

    print("Reeve Ingestion")
    print("----------------")
    if last_event_id is not None:
        print(f"Resuming from checkpoint: last completed event_id={last_event_id}")
    elif start_from_event is not None:
        print(f"Starting from event_id >= {start_from_event}")
    else:
        print("Starting from the beginning.")
    print(f"Completed entries: {len(completed_keys)}")
    print(f"Failed entries on disk: {len(failed_entries)}\n")

    logger.info(
        "Reeve ingestion started | speaker=%s | checkpoint_event=%s | completed=%s | failed=%s",
        speaker,
        last_event_id,
        len(completed_keys),
        len(failed_entries),
    )

    dataset = get_dataset(dataset_path=dataset_path)

    total = 0
    skipped = 0
    stored = 0
    failed = 0

    try:
        for event in dataset:
            if not isinstance(event, dict):
                logger.warning("Skipping malformed event: %s", event)
                continue

            try:
                event_numeric_id = int(event.get("event_id") or 0)
            except (TypeError, ValueError):
                event_numeric_id = 0

            if (
                last_event_id is None
                and start_from_event is not None
                and event_numeric_id < start_from_event
            ):
                skipped += 1
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
                user_input = f"{source_speaker} said: {text}"
                key = _entry_key(event_id, entry_index)
                entry["entry_index"] = entry_index

                if key in completed_keys:
                    skipped += 1
                    logger.debug("Skipping already completed entry key=%s", key)
                    continue

                total += 1
                print(f"\n[{event_id}:{entry_index}] {event_title} | speaker: {source_speaker}")
                print("  Reeve -> storing ... ", end="", flush=True)

                ok, message = _store_reeve(
                    user_input=user_input,
                    memory_owner=speaker,
                    source_speaker=source_speaker,
                    event_time=entry.get("event_time"),
                    event_id=event_id,
                    event_title=event_title,
                    event_year=entry.get("event_year"),
                    event_timeline=entry.get("event_timeline"),
                    event_phase=entry.get("event_phase"),
                    event_location=entry.get("event_location"),
                    character_ages=entry.get("character_ages"),
                )

                if ok:
                    stored += 1
                    completed_keys.add(key)
                    _save_progress(event_id, entry_index, list(completed_keys))
                    print(f"ok  {message}")
                    logger.info("Checkpoint saved | key=%s", key)
                else:
                    failed += 1
                    print(f"failed  {message}")
                    if key not in failed_keys:
                        failed_keys.add(key)
                        failed_entries.append(_failed_record(entry, key, user_input))
                        _save_failed(failed_entries)
                    print(f"  Entry {key} saved to {FAILED_FILE}; continuing.")

            if not has_entry:
                logger.debug("No ingestible entries for event_id=%s", event.get("event_id"))

    except (EOFError, KeyboardInterrupt):
        print("\nInterrupted. Progress is saved; rerun to resume.")
        logger.info("Reeve ingestion interrupted by user.")
    except Exception as exc:
        logger.exception("Fatal error in Reeve ingestion: %s", exc)
        print(f"\nFatal error: {exc}\nProgress is saved; rerun to resume.")
    finally:
        close()

    print("\nReeve Ingestion Summary")
    print("-----------------------")
    print(f"Skipped: {skipped}")
    print(f"Attempted this run: {total}")
    print(f"Stored: {stored}")
    print(f"Failed this run: {failed}")
    print(f"Failed entries on disk: {len(failed_entries)}")
    print(f"Progress file: {PROGRESS_FILE}")
    print(f"Failed file: {FAILED_FILE}")

    logger.info(
        "Reeve ingestion done | skipped=%s | attempted=%s | "
        "stored=%s | failed=%s | failed_on_disk=%s",
        skipped,
        total,
        stored,
        failed,
        len(failed_keys),
    )


def retry_failed(speaker: str = "default") -> None:
    """Retry only entries listed in reeve_failed_entries.json."""
    from threelane_memory.database import close

    _preflight()

    failed_entries = _load_failed()
    if not failed_entries:
        print("No failed Reeve entries found.")
        close()
        return

    progress = _load_progress()
    completed_keys = set(progress.get("completed_keys") or [])

    print("Retrying Failed Reeve Entries")
    print("-----------------------------")
    print(f"Found {len(failed_entries)} failed entries.\n")

    recovered = 0
    still_failed: list[dict] = []

    try:
        for failed_entry in failed_entries:
            key = failed_entry["key"]
            if key in completed_keys:
                continue

            event_id = failed_entry.get("event_id")
            entry_index = int(failed_entry.get("entry_index") or 0)
            event_title = failed_entry.get("event_title") or "Untitled Event"
            source_speaker = failed_entry.get("source_speaker") or speaker
            user_input = failed_entry["user_input"]

            print(f"\n[{event_id}:{entry_index}] {event_title}")
            print("  Reeve -> retrying ... ", end="", flush=True)

            ok, message = _store_reeve(
                user_input=user_input,
                memory_owner=speaker,
                source_speaker=source_speaker,
                event_time=failed_entry.get("event_time"),
                event_id=event_id,
                event_title=event_title,
                event_year=failed_entry.get("event_year"),
                event_timeline=failed_entry.get("event_timeline"),
                event_phase=failed_entry.get("event_phase"),
                event_location=failed_entry.get("event_location"),
                character_ages=failed_entry.get("character_ages"),
            )

            if ok:
                recovered += 1
                completed_keys.add(key)
                _save_progress(event_id, entry_index, list(completed_keys))
                print(f"ok  {message}")
            else:
                failed_entry["last_retry"] = datetime.now(timezone.utc).isoformat()
                failed_entry["last_error"] = message
                still_failed.append(failed_entry)
                print(f"failed  {message}")

    except (EOFError, KeyboardInterrupt):
        print("\nInterrupted. Remaining failed entries are saved.")
        logger.info("Reeve retry interrupted by user.")
    finally:
        _save_failed(still_failed)
        close()

    print("\nRetry Summary")
    print("-------------")
    print(f"Recovered: {recovered}")
    print(f"Still failed: {len(still_failed)}")
    print(f"Failed file: {FAILED_FILE}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Resilient Reeve-only Arthur dataset ingestion")
    parser.add_argument("--speaker", default="default", help="Memory owner/speaker namespace")
    parser.add_argument("--dataset", default=None, help="Path to dataset JSON")
    parser.add_argument(
        "--from", dest="start_from_event", type=int, default=None, help="Start at event_id"
    )
    parser.add_argument(
        "--fresh", action="store_true", help="Clear Reeve progress and failed files first"
    )
    parser.add_argument(
        "--retry-failed", action="store_true", help="Retry only failed Reeve entries"
    )
    args = parser.parse_args(argv)

    if args.retry_failed:
        retry_failed(speaker=args.speaker)
    else:
        ingest(
            speaker=args.speaker,
            dataset_path=args.dataset,
            start_from_event=args.start_from_event,
            fresh=args.fresh,
        )


if __name__ == "__main__":
    main()
