"""In-memory write-ahead buffer for async memory ingestion."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from threelane_memory.config import TEMP_BUFFER_MAX_SIZE

logger = logging.getLogger(__name__)

FAILED_LOG_PATH = Path(
    os.getenv(
        "TEMP_BUFFER_FAILED_LOG_PATH",
        str(Path(__file__).resolve().parents[1] / "tmp_failed.json"),
    )
)


@dataclass
class PendingEntry:
    pending_id: str
    raw_text: str
    speaker: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"
    episode_id: str | None = None
    error: str | None = None
    retries: int = 0
    image_embedding: list[float] | None = None
    # S3 key of the retained photo, not the bytes: a 4 MB payload per queued
    # write would blow up this in-memory buffer. Held here so a cancelled write
    # can still delete the object it already uploaded.
    image_key: str | None = None


class WriteBuffer:
    """Thread-safe memory buffer for writes waiting on durable persistence."""

    def __init__(self, max_size: int = TEMP_BUFFER_MAX_SIZE) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, PendingEntry] = {}
        self._max_size = max_size

    def add_pending(
        self,
        raw_text: str,
        speaker: str,
        image_embedding: list[float] | None = None,
        image_key: str | None = None,
    ) -> str:
        """Add an entry and return a temporary pending id immediately."""
        pending_id = f"tmp_{uuid.uuid4().hex[:10]}"
        entry = PendingEntry(
            pending_id=pending_id,
            raw_text=raw_text,
            speaker=speaker,
            image_embedding=image_embedding,
            image_key=image_key,
        )
        with self._lock:
            self._drop_oldest_finished_locked()
            active_count = sum(
                1 for e in self._entries.values() if e.status not in ("done", "failed")
            )
            if active_count >= self._max_size:
                raise RuntimeError("Async write buffer is full; try again shortly.")
            self._entries[pending_id] = entry
        logger.debug("WriteBuffer: added pending_id=%s speaker=%s", pending_id, speaker)
        return pending_id

    def get_pending_for_speaker(
        self,
        speaker: str,
        max_age_seconds: float = 300.0,
    ) -> list[PendingEntry]:
        """Return non-terminal entries for a speaker within the age window."""
        cutoff = time.time() - max_age_seconds
        with self._lock:
            return [
                entry
                for entry in self._entries.values()
                if entry.speaker == speaker
                and entry.status not in ("done", "failed")
                and entry.created_at >= cutoff
            ]

    def get_by_id(self, pending_id: str) -> PendingEntry | None:
        with self._lock:
            return self._entries.get(pending_id)

    def discard_speaker(self, speaker: str) -> int:
        """Cancel every not-yet-persisted write for *speaker*; return the count.

        Deleting a speaker's memories only removes what already reached Neo4j.
        Anything still queued here would persist moments later and resurrect
        data the user asked to erase, so a hard reset has to cancel the queue
        too. Entries are dropped outright rather than marked done: they never
        became memories, so they should leave no trace.

        Entries already handed to the worker (``persisting``) are dropped as
        well, and the worker treats a vanished entry as a cancelled write —
        see ``background_worker._process``.
        """
        with self._lock:
            doomed = [
                pending_id
                for pending_id, entry in self._entries.items()
                if entry.speaker == speaker and entry.status not in ("done", "failed")
            ]
            for pending_id in doomed:
                del self._entries[pending_id]
        if doomed:
            logger.info(
                "WriteBuffer: cancelled %s queued write(s) for speaker=%s on clear",
                len(doomed),
                speaker,
            )
        return len(doomed)

    def mark_persisting(self, pending_id: str) -> None:
        with self._lock:
            entry = self._entries.get(pending_id)
            if entry:
                entry.status = "persisting"

    def mark_retry(self, pending_id: str, error: str) -> None:
        with self._lock:
            entry = self._entries.get(pending_id)
            if entry:
                entry.retries += 1
                entry.error = error

    def mark_persisted(self, pending_id: str, episode_id: str) -> None:
        with self._lock:
            entry = self._entries.get(pending_id)
            if entry:
                entry.status = "done"
                entry.episode_id = episode_id
                entry.error = None
        logger.debug("WriteBuffer: persisted pending_id=%s episode_id=%s", pending_id, episode_id)

    def mark_failed(self, pending_id: str, error: str) -> None:
        entry: PendingEntry | None = None
        with self._lock:
            entry = self._entries.get(pending_id)
            if entry:
                entry.status = "failed"
                entry.error = error
        if entry:
            self._write_failed_log(entry)

    def drain_pending(self) -> list[PendingEntry]:
        """Return pending entries sorted oldest first."""
        with self._lock:
            return sorted(
                [entry for entry in self._entries.values() if entry.status == "pending"],
                key=lambda entry: entry.created_at,
            )

    def cleanup_done(self, max_age_seconds: float = 600.0) -> int:
        """Remove terminal entries older than max_age_seconds."""
        cutoff = time.time() - max_age_seconds
        with self._lock:
            before = len(self._entries)
            self._entries = {
                pending_id: entry
                for pending_id, entry in self._entries.items()
                if not (entry.status in ("done", "failed") and entry.created_at < cutoff)
            }
            return before - len(self._entries)

    def _drop_oldest_finished_locked(self) -> None:
        if len(self._entries) < self._max_size:
            return
        finished = [
            entry for entry in self._entries.values() if entry.status in ("done", "failed")
        ]
        if not finished:
            return
        oldest = min(finished, key=lambda entry: entry.created_at)
        self._entries.pop(oldest.pending_id, None)

    def _write_failed_log(self, entry: PendingEntry) -> None:
        """Append failed entries to disk so they can be replayed manually."""
        try:
            existing: list[dict] = []
            if FAILED_LOG_PATH.exists():
                with FAILED_LOG_PATH.open("r", encoding="utf-8") as handle:
                    existing = json.load(handle)
            existing.append(asdict(entry))
            with FAILED_LOG_PATH.open("w", encoding="utf-8") as handle:
                json.dump(existing, handle, indent=2)
            logger.warning("WriteBuffer: wrote failed entry to %s", FAILED_LOG_PATH)
        except Exception as exc:
            logger.error("WriteBuffer: could not write failed log: %s", exc)


_buffer = WriteBuffer()


def get_write_buffer() -> WriteBuffer:
    return _buffer
