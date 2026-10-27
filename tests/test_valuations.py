"""Tests for used-market valuation: store, background job, Homebox write-back and estimator."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homebox_companion import HomeboxAuthError
from homebox_companion.tools.valuation import (
    ItemSnapshot,
    JobState,
    Valuation,
    ValuationData,
    ValuationError,
    ValuationRecord,
    ValuationStore,
    ValuationStoreError,
    estimator,
)
from homebox_companion.tools.valuation.homebox_field import read_homebox_value, with_value_field
from homebox_companion.tools.vision.web_search import WebSearchResult
from server.services import valuation_jobs
from server.services.valuation_jobs import (
    ValuationJobConflictError,
    ValuationJobManager,
    apply_valuations,
    discard_valuations,
    select_targets,
)

VALUE_FIELD = {"id": "f1", "type": "text", "name": "Valore usato", "textValue": "10.00 EUR (2026-01-01)"}
OTHER_FIELD = {"id": "f2", "type": "text", "name": "Colore", "textValue": "rosso"}


def _items() -> list[dict[str, Any]]:
    return [
        {"id": "a", "name": "Drill", "thumbnailId": "t1", "fields": [OTHER_FIELD, VALUE_FIELD]},
        {"id": "b", "name": "Lamp", "fields": []},
        {"id": "loc", "name": "Garage", "entityType": {"isLocation": True}},
    ]


class FakeHomebox:
    """In-memory Homebox whose PUT, like the real one, replaces the whole item."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = {item["id"]: copy.deepcopy(item) for item in items}
        for item in self.items.values():
            item.update(manufacturer="Acme", modelNumber="X1", notes="private", purchasePrice=99, quantity=2)
        self.writes: list[tuple[str, dict[str, Any]]] = []

    async def list_items(self, *, page: int, page_size: int) -> dict[str, Any]:
        summaries = [{k: v for k, v in i.items() if k != "fields"} for i in self.items.values()]
        start = (page - 1) * page_size
        return {"items": summaries[start : start + page_size], "total": len(summaries)}

    async def get_item(self, item_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.items[item_id])

    async def update_item(self, item_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.writes.append((item_id, payload))
        self.items[item_id] = {"id": item_id, **copy.deepcopy(payload)}
        return copy.deepcopy(self.items[item_id])


def _valuation(value: float) -> Valuation:
    return Valuation(value=value, currency="EUR", confidence="medium")


def _pending(item_id: str, value: float) -> ValuationRecord:
    return ValuationRecord(item_id=item_id, name=item_id, current=_valuation(value))


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(valuation_jobs, "ITEM_PAUSE_SECONDS", 0)


@pytest.fixture
def store(tmp_path: Path) -> ValuationStore:
    return ValuationStore(tmp_path / "valuations.json")


async def _run_job(manager: ValuationJobManager, mode: str, gateway: Any) -> JobState:
    manager.start(mode, gateway)  # type: ignore[arg-type]
    assert manager._task is not None
    await manager._task
    job = manager.status()
    assert job is not None
    return job


@pytest.mark.unit
class TestValuationStore:
    def test_missing_file_is_empty(self, store: ValuationStore) -> None:
        assert store.load().records == {}

    def test_round_trip(self, store: ValuationStore) -> None:
        store.mutate(lambda d: d.records.__setitem__("a", _pending("a", 10)))

        record = store.load().records["a"]
        assert record.current.value == 10  # type: ignore[union-attr]
        assert record.is_pending

    def test_corrupt_file_is_never_overwritten(self, store: ValuationStore) -> None:
        store.path.write_text("{not json", encoding="utf-8")

        with pytest.raises(ValuationStoreError):
            store.mutate(lambda data: None)

        assert store.path.read_text(encoding="utf-8") == "{not json"

    def test_active_job_is_marked_interrupted_on_startup(self, store: ValuationStore) -> None:
        store.mutate(lambda d: setattr(d, "job", JobState(run_id="r", mode="missing", status="running")))

        ValuationJobManager(store)

        assert store.load().job.status == "interrupted"  # type: ignore[union-attr]


@pytest.mark.unit
class TestHomeboxField:
    @pytest.mark.parametrize(
        ("text", "value", "currency"),
        [
            ("10.00 EUR (2026-01-01)", 10.0, "EUR"),
            ("1.234,50 €", 1234.5, "EUR"),
            ("1,234.50 USD", 1234.5, "USD"),
            ("80", 80.0, None),
        ],
    )
    def test_reads_value(self, text: str, value: float, currency: str | None) -> None:
        item = {"fields": [{"name": "valore usato", "textValue": text}]}

        parsed = read_homebox_value(item)

        assert parsed is not None
        assert (parsed.value, parsed.currency) == (value, currency)

    def test_no_field_or_no_number(self) -> None:
        assert read_homebox_value({"fields": [OTHER_FIELD]}) is None
        assert read_homebox_value({"fields": [{"name": "Valore usato", "textValue": "boh"}]}) is None

    def test_updates_existing_field_in_place(self) -> None:
        fields = with_value_field([OTHER_FIELD, VALUE_FIELD], "20.00 EUR (2026-10-27)")

        assert fields[0] == OTHER_FIELD
        assert fields[1] == {**VALUE_FIELD, "textValue": "20.00 EUR (2026-10-27)"}

    def test_adds_missing_field(self) -> None:
        fields = with_value_field([OTHER_FIELD], "20.00 EUR (2026-10-27)")

        assert fields == [OTHER_FIELD, {"name": "Valore usato", "type": "text", "textValue": "20.00 EUR (2026-10-27)"}]


@pytest.mark.unit
def test_select_targets_uses_homebox_value_and_pending() -> None:
    items = [*_items()[:2], {"id": "c", "name": "Chair", "fields": []}]
    data = ValuationData(records={"c": _pending("c", 5)})

    assert [i["id"] for i in select_targets(items, data, "missing")] == ["b"]
    assert [i["id"] for i in select_targets(items, data, "refresh")] == ["a"]


@pytest.mark.integration
@pytest.mark.asyncio
class TestValuationJob:
    async def test_missing_values_items_without_homebox_value(self, store: ValuationStore) -> None:
        homebox = FakeHomebox(_items())
        manager = ValuationJobManager(store)

        with patch.object(valuation_jobs, "estimate_value", AsyncMock(return_value=_valuation(30))) as estimate:
            job = await _run_job(manager, "missing", homebox)

        assert job.status == "completed"
        assert job.done_ids == ["b"]
        snapshot = estimate.call_args.args[0]
        assert snapshot.manufacturer == "Acme"
        assert not hasattr(snapshot, "notes")
        assert store.load().records["b"].is_pending
        assert homebox.writes == []  # the job never writes to Homebox

    async def test_refresh_creates_new_pending_proposal(self, store: ValuationStore) -> None:
        homebox = FakeHomebox(_items())
        store.mutate(lambda d: d.records.__setitem__("a", _pending("a", 10).model_copy(update={"applied_at": None})))
        manager = ValuationJobManager(store)

        with patch.object(valuation_jobs, "estimate_value", AsyncMock(return_value=_valuation(20))):
            job = await _run_job(manager, "refresh", homebox)

        record = store.load().records["a"]
        assert record.current.value == 20  # type: ignore[union-attr]
        assert record.is_pending
        assert record.last_run_id == job.run_id
        assert homebox.writes == []

    async def test_failed_refresh_keeps_existing_estimate(self, store: ValuationStore) -> None:
        store.mutate(lambda d: d.records.__setitem__("a", _pending("a", 10)))
        manager = ValuationJobManager(store)

        failing = AsyncMock(side_effect=ValuationError("No listings found on the web"))
        with patch.object(valuation_jobs, "estimate_value", failing):
            job = await _run_job(manager, "refresh", FakeHomebox(_items()))

        record = store.load().records["a"]
        assert job.failed == {"a": "No listings found on the web"}
        assert record.current.value == 10  # type: ignore[union-attr]
        assert record.last_error == "No listings found on the web"

    async def test_second_job_conflicts_and_cancel_is_resumable(self, store: ValuationStore) -> None:
        manager = ValuationJobManager(store)
        started = asyncio.Event()
        items = [*_items(), {"id": "c", "name": "Chair", "fields": []}]

        async def slow(_item: ItemSnapshot) -> Valuation:
            started.set()
            await asyncio.sleep(60)
            return _valuation(1)

        with patch.object(valuation_jobs, "estimate_value", slow):
            manager.start("missing", FakeHomebox(items))  # type: ignore[arg-type]
            await started.wait()
            with pytest.raises(ValuationJobConflictError):
                manager.start("missing", FakeHomebox(items))  # type: ignore[arg-type]
            await manager.cancel()

        assert manager.status().status == "cancelled"  # type: ignore[union-attr]

        with patch.object(valuation_jobs, "estimate_value", AsyncMock(return_value=_valuation(7))):
            manager.resume()
            assert manager._task is not None
            await manager._task

        assert {r.item_id for r in store.load().records.values() if r.current} == {"b", "c"}


@pytest.mark.integration
@pytest.mark.asyncio
class TestApplyValuations:
    async def test_writes_only_the_value_field(self, store: ValuationStore) -> None:
        homebox = FakeHomebox(_items())
        before = await homebox.get_item("a")
        store.mutate(lambda d: d.records.__setitem__("a", _pending("a", 25.5)))

        result = await apply_valuations(homebox, store, ["a"])  # type: ignore[arg-type]

        assert result == {"applied": ["a"], "errors": {}}
        after = await homebox.get_item("a")
        for key in ("name", "manufacturer", "modelNumber", "notes", "purchasePrice", "quantity"):
            assert after[key] == before[key]
        assert after["fields"][0] == OTHER_FIELD
        assert after["fields"][1]["id"] == "f1"
        assert after["fields"][1]["textValue"].startswith("25.50 EUR (")
        assert not store.load().records["a"].is_pending

    async def test_adds_field_when_missing(self, store: ValuationStore) -> None:
        homebox = FakeHomebox(_items())
        store.mutate(lambda d: d.records.__setitem__("b", _pending("b", 12)))

        await apply_valuations(homebox, store, ["b"])  # type: ignore[arg-type]

        assert read_homebox_value(await homebox.get_item("b")).value == 12  # type: ignore[union-attr]

    async def test_items_without_estimate_are_not_written(self, store: ValuationStore) -> None:
        homebox = FakeHomebox(_items())

        result = await apply_valuations(homebox, store, ["a", "loc"])  # type: ignore[arg-type]

        assert set(result["errors"]) == {"a", "loc"}
        assert homebox.writes == []

    async def test_unconfirmed_write_stays_pending(self, store: ValuationStore) -> None:
        homebox = FakeHomebox(_items())
        homebox.update_item = AsyncMock(return_value={"id": "a", "fields": []})  # type: ignore[method-assign]
        store.mutate(lambda d: d.records.__setitem__("a", _pending("a", 25)))

        result = await apply_valuations(homebox, store, ["a"])  # type: ignore[arg-type]

        assert result["errors"] == {"a": "Homebox did not confirm the new value"}
        assert store.load().records["a"].is_pending

    async def test_auth_error_stops_batch(self, store: ValuationStore) -> None:
        homebox = FakeHomebox(_items())
        homebox.update_item = AsyncMock(side_effect=HomeboxAuthError("expired"))  # type: ignore[method-assign]
        store.mutate(lambda d: d.records.update({"a": _pending("a", 1), "b": _pending("b", 2)}))

        result = await apply_valuations(homebox, store, ["a", "b"])  # type: ignore[arg-type]

        assert result["applied"] == []
        assert set(result["errors"]) == {"a", "b"}
        assert homebox.update_item.await_count == 1


@pytest.mark.unit
def test_discard_drops_only_pending(store: ValuationStore) -> None:
    applied = _pending("b", 2).model_copy(update={"applied_at": _valuation(0).valued_at})
    store.mutate(lambda d: d.records.update({"a": _pending("a", 1), "b": applied}))

    assert discard_valuations(store, ["a", "b"]) == 1

    records = store.load().records
    assert records["a"].current is None
    assert records["b"].current is not None


@pytest.mark.integration
@pytest.mark.asyncio
class TestEstimator:
    ITEM = ItemSnapshot(id="a", name="Drill", manufacturer="Bosch", model_number="GSR 12V")
    LISTINGS = [WebSearchResult(title="Bosch GSR usato", url="https://subito.it/1", snippet="40 €")]

    async def test_drops_sources_not_in_results(self) -> None:
        estimate = {
            "estimated_value": 40,
            "low": 30,
            "high": 50,
            "currency": "EUR",
            "confidence": "medium",
            "reasoning": "Annunci simili a 40 €.",
            "sources": [
                {"title": "ok", "url": "https://subito.it/1", "price": "40 €"},
                {"title": "invented", "url": "https://fake.example", "price": "1 €"},
            ],
        }
        with (
            patch.object(estimator, "generate_queries", AsyncMock(return_value=["q"])),
            patch.object(estimator, "collect_listings", AsyncMock(return_value=self.LISTINGS)),
            patch.object(estimator, "chat_completion", AsyncMock(return_value=estimate)),
        ):
            valuation = await estimator.estimate_value(self.ITEM)

        assert valuation.value == 40
        assert [s.url for s in valuation.sources] == ["https://subito.it/1"]

    async def test_no_evidence_raises(self) -> None:
        estimate = {
            "estimated_value": None,
            "low": None,
            "high": None,
            "currency": "EUR",
            "confidence": "low",
            "reasoning": "Nessun annuncio comparabile.",
            "sources": [],
        }
        with (
            patch.object(estimator, "generate_queries", AsyncMock(return_value=["q"])),
            patch.object(estimator, "collect_listings", AsyncMock(return_value=self.LISTINGS)),
            patch.object(estimator, "chat_completion", AsyncMock(return_value=estimate)),
            pytest.raises(ValuationError, match="Nessun annuncio"),
        ):
            await estimator.estimate_value(self.ITEM)

    async def test_no_listings_skips_llm(self) -> None:
        completion = AsyncMock()
        with (
            patch.object(estimator, "generate_queries", AsyncMock(return_value=["q"])),
            patch.object(estimator, "collect_listings", AsyncMock(return_value=[])),
            patch.object(estimator, "chat_completion", completion),
            pytest.raises(ValuationError),
        ):
            await estimator.estimate_value(self.ITEM)

        completion.assert_not_called()

    async def test_query_generation_falls_back(self) -> None:
        with patch.object(estimator, "chat_completion", AsyncMock(side_effect=RuntimeError("down"))):
            queries = await estimator.generate_queries(self.ITEM)

        assert queries == ["Bosch GSR 12V usato prezzo", "Bosch GSR 12V usato"]
