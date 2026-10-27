"""Local persistence for used-market valuation proposals and job state.

Estimates live in ``data/valuations.json`` until the user approves them; the
approved value is then stored in Homebox (see ``homebox_field``), which stays
the single source of truth. The file only references Homebox items by ID.

Writes are atomic (temp file + ``os.replace``) and a file that cannot be
parsed is never overwritten, so a corrupt file cannot silently lose data.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from loguru import logger
from pydantic import BaseModel, Field

from ...core.exceptions import HomeboxCompanionError
from ...core.persistent_settings import DATA_DIR

VALUATIONS_FILE = DATA_DIR / "valuations.json"

JobMode = Literal["missing", "refresh"]
JobStatus = Literal["snapshot", "running", "completed", "cancelled", "interrupted", "failed"]
ACTIVE_STATUSES: frozenset[str] = frozenset({"snapshot", "running"})


def utc_now() -> datetime:
    return datetime.now(UTC)


class ValuationSource(BaseModel):
    """A web listing the estimate was based on."""

    title: str = ""
    url: str = ""
    price: str | None = None


class Valuation(BaseModel):
    """One used-market estimate for an item."""

    value: float = Field(ge=0)
    currency: str
    low: float | None = None
    high: float | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    reasoning: str = ""
    sources: list[ValuationSource] = Field(default_factory=list)
    valued_at: datetime = Field(default_factory=utc_now)


class ValuationRecord(BaseModel):
    """Latest estimate of a single Homebox item.

    Homebox is the source of truth for the approved value: the estimate stays
    a proposal here until the user writes it to Homebox (``applied_at``).
    """

    item_id: str
    name: str
    current: Valuation | None = None
    applied_at: datetime | None = None
    last_run_id: str | None = None
    last_error: str | None = None

    @property
    def is_pending(self) -> bool:
        """An estimate exists that has not been written to Homebox yet."""
        return self.current is not None and self.applied_at is None


class ItemSnapshot(BaseModel):
    """Item data captured from Homebox when a job starts.

    The job runs from this snapshot, so it keeps working for days without
    needing a valid Homebox session.
    """

    id: str
    name: str
    description: str | None = None
    manufacturer: str | None = None
    model_number: str | None = None
    purchase_price: float | None = None


class JobState(BaseModel):
    """Progress of the current (or last) valuation job."""

    run_id: str
    mode: JobMode
    status: JobStatus
    queue: list[ItemSnapshot] = Field(default_factory=list)
    done_ids: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)
    current_item: str | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None

    def pending(self) -> list[ItemSnapshot]:
        handled = set(self.done_ids) | set(self.failed)
        return [item for item in self.queue if item.id not in handled]


class ValuationData(BaseModel):
    version: int = 1
    records: dict[str, ValuationRecord] = Field(default_factory=dict)
    job: JobState | None = None


class ValuationStoreError(HomeboxCompanionError):
    """The valuations file exists but cannot be read."""

    error_code = "VALUATIONS_FILE_UNREADABLE"


class ValuationStore:
    """Thread-safe JSON store for valuations and job state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else VALUATIONS_FILE
        self._lock = threading.RLock()

    def load(self) -> ValuationData:
        with self._lock:
            if not self.path.exists():
                return ValuationData()
            try:
                return ValuationData.model_validate_json(self.path.read_text(encoding="utf-8"))
            except Exception as e:
                raise ValuationStoreError(
                    f"Cannot read {self.path}: {e}",
                    user_message=f"The valuations file {self.path} is unreadable. Fix or move it aside.",
                ) from e

    def mutate(self, mutate: Callable[[ValuationData], None]) -> ValuationData:
        """Read fresh, mutate and atomically persist under the store lock."""
        with self._lock:
            data = self.load()
            mutate(data)
            self._write(data)
            return data.model_copy(deep=True)

    def _write(self, data: ValuationData) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data.model_dump(mode="json"), ensure_ascii=False, indent=2)
        file_descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        temporary_path = Path(temporary_name)
        try:
            temporary_file = os.fdopen(file_descriptor, "w", encoding="utf-8")
            file_descriptor = -1  # The file object now owns the descriptor.
            with temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except BaseException:
            if file_descriptor != -1:
                with suppress(OSError):
                    os.close(file_descriptor)
            temporary_path.unlink(missing_ok=True)
            raise

    def mark_interrupted_jobs(self) -> None:
        """A job still marked active at startup was killed by a restart."""
        with self._lock:
            try:
                data = self.load()
            except ValuationStoreError as e:
                logger.error(str(e))
                return
            if data.job and data.job.status in ACTIVE_STATUSES:

                def interrupt(d: ValuationData) -> None:
                    assert d.job is not None
                    d.job.status = "interrupted"
                    d.job.current_item = None

                self.mutate(interrupt)
                logger.warning(f"Valuation job {data.job.run_id} was interrupted by a restart; it can be resumed")
