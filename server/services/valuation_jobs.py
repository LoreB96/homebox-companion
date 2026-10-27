"""Background job that values Homebox items on the used market.

One job runs at a time, in-process. It has two phases:

1. snapshot: read items from Homebox (read-only) with the starting user's
   credentials and save the queue to disk;
2. running: value each queued item from the snapshot. No Homebox access is
   needed, so the job can last for days and survive session expiry.

Every estimate is persisted as soon as it is ready, as a proposal. After a
restart the job is marked ``interrupted`` and can be resumed from the saved queue.

Estimates reach Homebox only through ``apply_valuations``, when the user
approves them: Homebox stays the single source of truth for the value.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from typing import Any

from loguru import logger

from homebox_companion import HomeboxAuthError, HomeboxCompanionError, HomeboxGateway
from homebox_companion.homebox.relationships import item_update_payload
from homebox_companion.tools.valuation import (
    ItemSnapshot,
    JobMode,
    JobState,
    ValuationData,
    ValuationError,
    ValuationRecord,
    ValuationStore,
    estimate_value,
)
from homebox_companion.tools.valuation.homebox_field import format_value, read_homebox_value, with_value_field
from homebox_companion.tools.valuation.store import ACTIVE_STATUSES, JobStatus, utc_now

LIST_PAGE_SIZE = 100
MAX_CONCURRENT_DETAIL_READS = 6
# Pause between items, on top of the per-search delay
ITEM_PAUSE_SECONDS = 2.0


class ValuationJobConflictError(HomeboxCompanionError):
    status_code = 409
    error_code = "VALUATION_JOB_CONFLICT"
    log_level = "info"


def is_location(entity: dict[str, Any]) -> bool:
    entity_type = entity.get("entityType")
    if isinstance(entity_type, dict) and entity_type.get("isLocation") is True:
        return True
    return entity.get("isLocation") is True


async def list_all_items(gateway: HomeboxGateway) -> list[dict[str, Any]]:
    """Read every item (not locations) visible to the gateway, page by page."""
    items: list[dict[str, Any]] = []
    seen = 0
    page = 1
    while True:
        response = await gateway.list_items(page=page, page_size=LIST_PAGE_SIZE)
        batch = response.get("items") or []
        seen += len(batch)
        items.extend(entity for entity in batch if not is_location(entity))
        total = response.get("total")
        if not batch or (isinstance(total, int) and seen >= total) or len(batch) < LIST_PAGE_SIZE:
            return items
        page += 1


def _price(value: Any) -> float | None:
    try:
        price = float(value)
    except TypeError, ValueError:
        return None
    return price if price > 0 else None


def snapshot_from(entity: dict[str, Any]) -> ItemSnapshot:
    """Keep only the product-identifying fields (no notes, serials or locations)."""
    return ItemSnapshot(
        id=entity["id"],
        name=entity.get("name") or "",
        description=entity.get("description") or None,
        manufacturer=entity.get("manufacturer") or None,
        model_number=entity.get("modelNumber") or None,
        purchase_price=_price(entity.get("purchasePrice")),
    )


async def read_item_details(gateway: HomeboxGateway, summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Full item details (custom fields, manufacturer...), which list summaries lack.

    A failed read falls back to the summary; authentication errors propagate.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_DETAIL_READS)

    async def details(summary: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                return {**summary, **await gateway.get_item(summary["id"])}
            except HomeboxAuthError:
                raise
            except Exception as e:
                logger.warning(f"Could not read details of '{summary.get('name')}', using summary: {e}")
                return summary

    return list(await asyncio.gather(*(details(item) for item in summaries)))


def select_targets(items: list[dict[str, Any]], data: ValuationData, mode: JobMode) -> list[dict[str, Any]]:
    """missing: no value in Homebox and no proposal awaiting approval; refresh: value in Homebox."""

    def pending(item_id: str) -> bool:
        record = data.records.get(item_id)
        return record is not None and record.is_pending

    if mode == "missing":
        return [item for item in items if read_homebox_value(item) is None and not pending(item["id"])]
    return [item for item in items if read_homebox_value(item) is not None]


def _already_running() -> ValuationJobConflictError:
    return ValuationJobConflictError("Valuation job already running", user_message="A valuation is already running.")


class ValuationJobManager:
    def __init__(self, store: ValuationStore) -> None:
        self.store = store
        self._task: asyncio.Task[None] | None = None
        self.store.mark_interrupted_jobs()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> JobState | None:
        job = self.store.load().job
        if job and job.status in ACTIVE_STATUSES and not self.is_running:
            # Task died without updating the file (should not happen); report it honestly
            job.status = "interrupted"
        return job

    def start(self, mode: JobMode, gateway: HomeboxGateway) -> JobState:
        if self.is_running:
            raise _already_running()
        job = JobState(run_id=uuid.uuid4().hex, mode=mode, status="snapshot")

        def begin(data: ValuationData) -> None:
            data.job = job

        self.store.mutate(begin)
        self._task = asyncio.create_task(self._run(job.run_id, gateway), name=f"valuation-{job.run_id}")
        logger.info(f"Valuation job {job.run_id} started (mode={mode})")
        return job

    def resume(self) -> JobState:
        if self.is_running:
            raise _already_running()
        job = self.store.load().job
        if job is None or job.status not in ("interrupted", "cancelled", "failed") or not job.queue:
            raise ValuationJobConflictError(
                "No resumable valuation job", user_message="There is no interrupted valuation to resume."
            )

        def restart(data: ValuationData) -> None:
            assert data.job is not None
            data.job.status = "running"
            data.job.error = None
            data.job.finished_at = None

        job = self.store.mutate(restart).job
        assert job is not None
        self._task = asyncio.create_task(self._run(job.run_id, None), name=f"valuation-{job.run_id}")
        logger.info(f"Valuation job {job.run_id} resumed ({len(job.pending())} item(s) left)")
        return job

    async def cancel(self) -> None:
        """Stop the running job. Values saved so far are kept; the job can be resumed."""
        if self.is_running and self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def shutdown(self) -> None:
        """Stop the task on app shutdown; the job stays resumable."""
        if self.is_running:
            await self.cancel()
            self._finish("interrupted")

    def _finish(self, status: JobStatus, error: str | None = None) -> None:
        def finish(data: ValuationData) -> None:
            if data.job is None:
                return
            data.job.status = status
            data.job.error = error
            data.job.current_item = None
            data.job.finished_at = utc_now()

        self.store.mutate(finish)

    async def _run(self, run_id: str, gateway: HomeboxGateway | None) -> None:
        try:
            if gateway is not None:
                await self._snapshot(gateway)
            await self._value_queue(run_id)
        except asyncio.CancelledError:
            self._finish("cancelled")
            logger.info(f"Valuation job {run_id} cancelled")
            raise
        except HomeboxAuthError as e:
            self._finish("failed", "Homebox session expired while reading items. Log in again and restart.")
            logger.warning(f"Valuation job {run_id} failed reading Homebox: {e}")
        except Exception as e:
            self._finish("failed", str(e) or type(e).__name__)
            logger.exception(f"Valuation job {run_id} failed")
        else:
            self._finish("completed")
            logger.info(f"Valuation job {run_id} completed")

    async def _snapshot(self, gateway: HomeboxGateway) -> None:
        """Read the items to value from Homebox. Read-only."""
        items = await read_item_details(gateway, await list_all_items(gateway))
        data = self.store.load()
        assert data.job is not None
        queue = [snapshot_from(item) for item in select_targets(items, data, data.job.mode)]

        def save_queue(data: ValuationData) -> None:
            assert data.job is not None
            data.job.queue = queue
            data.job.status = "running"

        self.store.mutate(save_queue)
        logger.info(f"Valuation snapshot: {len(queue)} of {len(items)} item(s) to value")

    async def _value_queue(self, run_id: str) -> None:
        job = self.store.load().job
        assert job is not None
        for index, item in enumerate(job.pending()):
            if index:
                await asyncio.sleep(ITEM_PAUSE_SECONDS)
            self._set_current(item.name)
            try:
                valuation = await estimate_value(item)
            except ValuationError as e:
                self._record_failure(item, str(e))
                continue
            except Exception as e:
                logger.exception(f"Unexpected error valuing '{item.name}'")
                self._record_failure(item, str(e) or type(e).__name__)
                continue

            def save(data: ValuationData, item: ItemSnapshot = item, valuation=valuation) -> None:
                record = data.records.get(item.id) or ValuationRecord(item_id=item.id, name=item.name)
                record.name = item.name
                record.current = valuation
                record.applied_at = None
                record.last_run_id = run_id
                record.last_error = None
                data.records[item.id] = record
                assert data.job is not None
                data.job.done_ids.append(item.id)

            self.store.mutate(save)

    def _set_current(self, name: str) -> None:
        def update(data: ValuationData) -> None:
            assert data.job is not None
            data.job.current_item = name

        self.store.mutate(update)

    def _record_failure(self, item: ItemSnapshot, error: str) -> None:
        logger.info(f"Could not value '{item.name}': {error}")

        def update(data: ValuationData) -> None:
            record = data.records.get(item.id) or ValuationRecord(item_id=item.id, name=item.name)
            # Keep any existing estimate: a failed refresh must not erase it
            record.last_error = error
            data.records[item.id] = record
            assert data.job is not None
            data.job.failed[item.id] = error

        self.store.mutate(update)


async def apply_valuations(gateway: HomeboxGateway, store: ValuationStore, item_ids: list[str]) -> dict[str, Any]:
    """Write approved estimates to the value custom field in Homebox.

    Each item is re-read right before the write and sent back whole
    (``item_update_payload``), because Homebox's PUT replaces the entire
    item: only the value field changes. The response is checked before the
    estimate is marked as applied. Items are processed one by one; an
    authentication error stops the batch.
    """
    applied: list[str] = []
    errors: dict[str, str] = {}
    records = store.load().records
    ordered_ids = list(dict.fromkeys(item_ids))

    for index, item_id in enumerate(ordered_ids):
        record = records.get(item_id)
        if record is None or record.current is None:
            errors[item_id] = "No estimate to write"
            continue
        text = format_value(record.current)
        try:
            current = await gateway.get_item(item_id)
            if is_location(current):
                errors[item_id] = "Not an item"
                continue
            payload = item_update_payload(current)
            payload["fields"] = with_value_field(payload["fields"], text)
            updated = await gateway.update_item(item_id, payload)
        except HomeboxAuthError:
            for remaining in ordered_ids[index:]:
                errors.setdefault(remaining, "Not written: Homebox session expired")
            break
        except Exception as e:
            logger.warning(f"Could not write value of '{record.name}' to Homebox: {e}")
            errors[item_id] = str(e) or type(e).__name__
            continue

        stored = read_homebox_value(updated)
        if stored is None or abs(stored.value - record.current.value) > 0.005:
            errors[item_id] = "Homebox did not confirm the new value"
            continue

        def mark_applied(data: ValuationData, item_id: str = item_id) -> None:
            if (saved := data.records.get(item_id)) is not None:
                saved.applied_at = utc_now()

        store.mutate(mark_applied)
        applied.append(item_id)
        logger.info(f"Wrote used value '{text}' to Homebox item '{record.name}'")

    return {"applied": applied, "errors": errors}


def discard_valuations(store: ValuationStore, item_ids: list[str]) -> int:
    """Drop pending proposals. Homebox is not touched; applied estimates are kept."""
    wanted = set(item_ids)
    discarded = 0

    def discard(data: ValuationData) -> None:
        nonlocal discarded
        for item_id in wanted:
            record = data.records.get(item_id)
            if record is not None and record.is_pending:
                record.current = None
                discarded += 1

    store.mutate(discard)
    return discarded
