"""Web search lookups for identifying detected items.

Uses DDGS (DuckDuckGo and other backends, no API key required) to look up
manufacturer and model codes read from item photos. Results are passed to
the LLM as reference material so it can identify what an item actually is.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from loguru import logger

from ...core import config
from .models import DetectedItem

# Per-result snippet cap keeps the enrichment prompt small for local models
MAX_SNIPPET_CHARS = 300


@dataclass(frozen=True)
class WebSearchResult:
    """A single web search hit."""

    title: str
    url: str
    snippet: str


def build_search_queries(item: DetectedItem) -> list[str]:
    """Build search queries from the identifying codes of an item.

    Only manufacturer and model number are used: they are what the LLM
    cannot interpret on its own. Serial numbers are never searched
    (privacy, and they don't identify a product type).

    Returns:
        Queries ordered from most to least specific, empty if the item
        has no identifying codes.
    """
    manufacturer = (item.manufacturer or "").strip()
    model_number = (item.model_number or "").strip()

    queries: list[str] = []
    if manufacturer and model_number:
        queries.append(f'{manufacturer} "{model_number}"')
    if model_number:
        queries.append(f'"{model_number}"')
    elif manufacturer:
        queries.append(f"{manufacturer} {item.name}")
    return queries


def _search_sync(query: str, max_results: int) -> list[WebSearchResult]:
    """Run a blocking DDGS text search."""
    from ddgs import DDGS

    raw_results = DDGS(timeout=config.settings.web_search_timeout).text(
        query,
        region=config.settings.web_search_region,
        safesearch="moderate",
        max_results=max_results,
    )
    return [
        WebSearchResult(
            title=str(r.get("title", "")).strip(),
            url=str(r.get("href", "")).strip(),
            snippet=str(r.get("body", "")).strip()[:MAX_SNIPPET_CHARS],
        )
        for r in raw_results
    ]


async def search_item(item: DetectedItem) -> list[WebSearchResult]:
    """Search the web for an item using its identifying codes.

    Tries queries from most to least specific and stops at the first one
    that returns results. Never raises: search failures return an empty
    list so detection keeps working without internet access.
    """
    max_results = config.settings.web_search_max_results

    for query in build_search_queries(item):
        try:
            results = await asyncio.to_thread(_search_sync, query, max_results)
        except Exception as e:
            logger.warning(f"Web search failed for query {query!r}: {e}")
            continue

        # Drop hits without a URL and duplicate URLs
        unique: dict[str, WebSearchResult] = {}
        for result in results:
            if result.url and result.url not in unique:
                unique[result.url] = result

        if unique:
            logger.debug(f"Web search {query!r} returned {len(unique)} result(s)")
            return list(unique.values())

    return []
