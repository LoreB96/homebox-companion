"""Tests for web search enrichment of detected items."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homebox_companion.core import config
from homebox_companion.tools.vision import enricher
from homebox_companion.tools.vision.enricher import enrich_item, enrich_items
from homebox_companion.tools.vision.models import DetectedItem
from homebox_companion.tools.vision.prompts import build_enrichment_user_prompt
from homebox_companion.tools.vision.web_search import WebSearchResult, build_search_queries, search_item

IMAGES = ["data:image/jpeg;base64,AAAA"]
RESULTS = [WebSearchResult(title="Bosch GSR 12V-15", url="https://example.com/gsr", snippet="Cordless drill")]


@pytest.fixture
def web_search_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.settings, "web_search_enabled", True)


@pytest.mark.unit
class TestBuildSearchQueries:
    """Queries come only from manufacturer and model number."""

    def test_manufacturer_and_model(self) -> None:
        item = DetectedItem(name="Drill", manufacturer="Bosch", model_number="GSR 12V-15")

        assert build_search_queries(item) == ['Bosch "GSR 12V-15"', '"GSR 12V-15"']

    def test_manufacturer_only_uses_name(self) -> None:
        item = DetectedItem(name="Drill", manufacturer="Bosch")

        assert build_search_queries(item) == ["Bosch Drill"]

    def test_no_codes_returns_empty(self) -> None:
        item = DetectedItem(name="Drill", serial_number="SN123")

        assert build_search_queries(item) == []


@pytest.mark.unit
def test_user_prompt_wraps_results_as_data() -> None:
    prompt = build_enrichment_user_prompt('{"name": "Drill"}', [("Title", "https://x.y", "Snippet")])

    assert "<web_results>" in prompt
    assert "[1] Title" in prompt
    assert "https://x.y" in prompt


@pytest.mark.integration
@pytest.mark.asyncio
class TestSearchItem:
    """search_item falls back between queries and never raises."""

    async def test_falls_back_to_next_query_on_error(self) -> None:
        item = DetectedItem(name="Drill", manufacturer="Bosch", model_number="GSR")
        calls: list[str] = []

        def fake_search(query: str, _max: int) -> list[WebSearchResult]:
            calls.append(query)
            if len(calls) == 1:
                raise RuntimeError("rate limited")
            return RESULTS + RESULTS  # duplicate URL is dropped

        with patch("homebox_companion.tools.vision.web_search._search_sync", side_effect=fake_search):
            results = await search_item(item)

        assert calls == ['Bosch "GSR"', '"GSR"']
        assert results == RESULTS

    async def test_item_without_codes_skips_search(self) -> None:
        with patch("homebox_companion.tools.vision.web_search._search_sync") as mock_search:
            results = await search_item(DetectedItem(name="Drill"))

        assert results == []
        mock_search.assert_not_called()


@pytest.mark.integration
@pytest.mark.asyncio
class TestEnrichItem:
    """enrich_item merges LLM output and falls back to the original item."""

    async def test_keeps_instance_fields_from_detection(self) -> None:
        item = DetectedItem(
            name="Black Drill",
            quantity=2,
            model_number="GSR 12V-15",
            serial_number="SN123",
            purchase_price=50.0,
            tag_ids=["t1"],
        )
        llm_output = {
            "name": "Bosch GSR 12V-15 Cordless Drill",
            "manufacturer": "Bosch",
            "modelNumber": "GSR 12V-15",
            "serialNumber": "INVENTED",
            "purchasePrice": 999,
            "quantity": 1,
        }

        with (
            patch.object(enricher, "search_item", AsyncMock(return_value=RESULTS)),
            patch.object(enricher, "vision_completion", AsyncMock(return_value=llm_output)),
        ):
            result = await enrich_item(item, IMAGES)

        assert result.name == "Bosch GSR 12V-15 Cordless Drill"
        assert result.manufacturer == "Bosch"
        assert result.quantity == 2
        assert result.serial_number == "SN123"
        assert result.purchase_price == 50.0
        assert result.tag_ids == ["t1"]

    async def test_no_results_skips_llm(self) -> None:
        item = DetectedItem(name="Drill", model_number="X1")
        mock_llm = AsyncMock()

        with (
            patch.object(enricher, "search_item", AsyncMock(return_value=[])),
            patch.object(enricher, "vision_completion", mock_llm),
        ):
            result = await enrich_item(item, IMAGES)

        assert result is item
        mock_llm.assert_not_called()

    async def test_llm_failure_returns_original(self) -> None:
        item = DetectedItem(name="Drill", model_number="X1")

        with (
            patch.object(enricher, "search_item", AsyncMock(return_value=RESULTS)),
            patch.object(enricher, "vision_completion", AsyncMock(side_effect=RuntimeError("timeout"))),
        ):
            result = await enrich_item(item, IMAGES)

        assert result is item


@pytest.mark.integration
@pytest.mark.asyncio
class TestEnrichItems:
    """enrich_items is gated by HBC_WEB_SEARCH_ENABLED."""

    async def test_disabled_returns_items_unchanged(self) -> None:
        items = [DetectedItem(name="Drill", model_number="X1")]
        mock_search = AsyncMock()

        with patch.object(enricher, "search_item", mock_search):
            result = await enrich_items(items, IMAGES)

        assert result is items
        mock_search.assert_not_called()

    @pytest.mark.usefixtures("web_search_enabled")
    async def test_enabled_preserves_order(self) -> None:
        items = [DetectedItem(name="A"), DetectedItem(name="B")]

        async def fake_enrich(item: DetectedItem, *_args: object, **_kwargs: object) -> DetectedItem:
            return item.model_copy(update={"name": f"{item.name}!"})

        with patch.object(enricher, "enrich_item", side_effect=fake_enrich):
            result = await enrich_items(items, IMAGES)

        assert [i.name for i in result] == ["A!", "B!"]
