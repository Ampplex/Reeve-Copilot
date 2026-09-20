"""Background worker that persists pending memory writes."""

from __future__ import annotations

import logging
import threading
import time
from queue import Empty, Queue

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0
CLEANUP_INTERVAL_SECONDS = 120.0


class PersistWorker(threading.Thread):
    """Drain pending ids and run the heavy extraction + Neo4j persistence path."""

    def __init__(self, work_queue: Queue[str]) -> None:
        super().__init__(name="reeve-persist-worker", daemon=True)
        self._queue = work_queue
        self._stop_event = threading.Event()
        self._last_cleanup = time.time()

    def run(self) -> None:
        logger.info("PersistWorker: started")
        while not self._stop_event.is_set():
            try:
                pending_id = self._queue.get(timeout=1.0)
            except Empty:
                self._maybe_cleanup()
                continue

            try:
                self._process(pending_id)
            finally:
                self._queue.task_done()
                self._maybe_cleanup()

        logger.info("PersistWorker: stopped")

    @staticmethod
    def _discard_episode(episode_id: str, speaker: str) -> None:
        """Remove an episode written after its speaker cleared their memory.

        Mirrors ``reconciler.clear_speaker``: the episode plus the
        State/Action/Relation nodes it owns, then any Entity/Location of that
        speaker left with no episodes at all. Writing an episode also creates
        those per-speaker nodes, and they can hold personal names, so leaving
        them behind would keep data the user asked to erase. Nodes still
        referenced by another episode are kept — only true orphans go.

        Best-effort: a failure must not crash the worker, and any leftover is
        bounded to this one write and removed by the next clear.
        """
        try:
            from threelane_memory.database import run_query

            run_query(
                """
                MATCH (ep:Episode {id:$id})
                OPTIONAL MATCH (ep)-[:HAS_STATE]->(s:State)
                OPTIONAL MATCH (ep)-[:HAS_ACTION]->(a:Action)
                OPTIONAL MATCH (ep)-[:HAS_RELATION]->(rel:Relation)
                DETACH DELETE s, a, rel, ep
                """,
                {"id": episode_id},
            )
            run_query(
                """
                MATCH (e:Entity {speaker:$speaker})
                WHERE NOT (e)<-[:INVOLVES]-(:Episode)
                  AND NOT (e)<-[:OF_ENTITY|BY_ENTITY|ON_ENTITY|FROM_ENTITY|TO_ENTITY]-()
                DETACH DELETE e
                """,
                {"speaker": speaker},
            )
            run_query(
                """
                MATCH (l:Location {speaker:$speaker})
                WHERE NOT (l)<-[:AT_LOCATION]-(:Episode)
                DETACH DELETE l
                """,
                {"speaker": speaker},
            )
        except Exception as exc:  # pragma: no cover - best effort
            logger.warning(
                "PersistWorker: could not discard cancelled episode_id=%s: %s",
                episode_id,
                exc,
            )

    @staticmethod
    def _discard_image(image_key: str | None) -> None:
        """Delete a retained photo whose write was cancelled.

        Unlike the graph nodes above this is NOT merely best-effort housekeeping:
        the object is the user's actual photograph, and it was uploaded before
        the write was queued. If the write is cancelled and nothing deletes it,
        it survives an erasure request with no episode left pointing at it — so
        no later clear would ever find it either.
        """
        if not image_key:
            return
        try:
            from threelane_memory import image_store

            image_store.delete_keys([image_key])
        except Exception as exc:  # pragma: no cover - best effort
            logger.error(
                "PersistWorker: could not delete retained image key=%s: %s", image_key, exc
            )

    def _process(self, pending_id: str) -> None:
        from threelane_memory.write_buffer import get_write_buffer

        buffer = get_write_buffer()
        entry = buffer.get_by_id(pending_id)
        if entry is None:
            logger.warning("PersistWorker: pending_id=%s not found", pending_id)
            return

        buffer.mark_persisting(pending_id)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                from threelane_memory import _store_sync
                from threelane_memory.database import update_log_tokens

                # The speaker may have wiped their memory while this write sat in
                # the queue. `discard_speaker` removes the entry, so a missing one
                # means "cancelled" — persisting it now would resurrect data the
                # user asked to erase.
                if buffer.get_by_id(pending_id) is None:
                    # The photo was uploaded before the write was queued, so it
                    # outlives the cancelled entry unless we delete it here.
                    self._discard_image(entry.image_key)
                    logger.info(
                        "PersistWorker: pending_id=%s cancelled before write (memory cleared)",
                        pending_id,
                    )
                    return

                episode_id, t_in, t_out = _store_sync(
                    entry.raw_text,
                    speaker=entry.speaker,
                    image_embedding=entry.image_embedding,
                    image_key=entry.image_key,
                )

                # Extraction takes seconds, so the clear can also land *during*
                # the write. Undo it rather than leave an episode the user
                # believes is gone.
                if buffer.get_by_id(pending_id) is None:
                    self._discard_episode(episode_id, entry.speaker)
                    self._discard_image(entry.image_key)
                    logger.info(
                        "PersistWorker: pending_id=%s cancelled mid-write; "
                        "removed episode_id=%s",
                        pending_id,
                        episode_id,
                    )
                    return

                buffer.mark_persisted(pending_id, episode_id)

                # Update the RequestLog with actual token counts
                try:
                    update_log_tokens(pending_id, t_in, t_out)
                except Exception:
                    pass

                logger.info(
                    "PersistWorker: persisted pending_id=%s episode_id=%s attempt=%s tokens=%s/%s",
                    pending_id,
                    episode_id,
                    attempt,
                    t_in,
                    t_out
                )
                return
            except Exception as exc:
                buffer.mark_retry(pending_id, str(exc))
                logger.warning(
                    "PersistWorker: attempt %s/%s failed for pending_id=%s: %s",
                    attempt,
                    MAX_RETRIES,
                    pending_id,
                    exc,
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS * attempt)

        error_msg = f"Failed after {MAX_RETRIES} attempts"
        buffer.mark_failed(pending_id, error_msg)
        # The photo was uploaded before this write was queued, and no episode
        # now exists to reference it. Without this it would sit in the store
        # unreferenced until the retention clock expired it.
        self._discard_image(entry.image_key)
        logger.error("PersistWorker: permanently failed pending_id=%s", pending_id)

    def _maybe_cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup <= CLEANUP_INTERVAL_SECONDS:
            return

        from threelane_memory.write_buffer import get_write_buffer

        removed = get_write_buffer().cleanup_done()
        if removed:
            logger.debug("PersistWorker: cleaned up %s terminal entries", removed)
        self._sync_payg_quantities()
        self._last_cleanup = now

    @staticmethod
    def _sync_payg_quantities() -> None:
        """Keep pay-as-you-go subscriptions charging the right amount.

        Quantity is what Razorpay debits at the next charge, and that charge
        fires on Razorpay's clock — the webhook tells us *after* the money has
        moved. So the amount has to be kept correct continuously rather than
        computed when a webhook arrives, or a customer gets billed a stale
        figure. This rides the worker's existing cleanup tick, off the request
        path, so nobody waits on it.

        Best-effort: a failed sync leaves the next charge stale, and the
        carry-forward in the billing path settles the difference next cycle.
        """
        try:
            from threelane_memory.database import run_query
            from threelane_memory.payg import PAYG_PLAN, sync_subscription_quantity

            rows = run_query(
                "MATCH (u:User) WHERE u.plan = $plan AND u.subscription_id IS NOT NULL "
                "AND u.subscription_status IN ['active', 'authenticated'] "
                "RETURN u.uid AS uid, u.subscription_id AS sid LIMIT 500",
                {"plan": PAYG_PLAN},
            )
            for row in rows:
                sync_subscription_quantity(row["uid"], row["sid"])
        except Exception as exc:  # pragma: no cover - never break the worker
            logger.debug("PersistWorker: PAYG quantity sync skipped: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()


_worker: PersistWorker | None = None
_work_queue: Queue[str] = Queue()
_lock = threading.Lock()


def start_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = PersistWorker(_work_queue)
        _worker.start()


def stop_worker() -> None:
    global _worker
    with _lock:
        if _worker is None:
            return
        _worker.stop()
        _worker.join(timeout=10.0)
        _worker = None


def enqueue(pending_id: str) -> None:
    _work_queue.put(pending_id)


def is_running() -> bool:
    return _worker is not None and _worker.is_alive()
