"""Item identification refinement using web search results.

Second pass after detection: items with a manufacturer or model number are
searched on the web, and the LLM re-identifies them using the photos plus
the search results. Enabled with HBC_WEB_SEARCH_ENABLED.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from loguru import logger

from ...ai.llm import vision_completion
from ...ai.response_models import get_single_item_response_model
from ...core import config
from .models import DetectedItem, get_single_item_adapter
from .prompts import build_enrichment_system_prompt, build_enrichment_user_prompt
from .web_search import search_item

if TYPE_CHECKING:
    from ...core.persistent_settings import CustomFieldDefinition

# Local LLMs usually serve one request at a time; avoid piling up long vision calls
MAX_CONCURRENT_ENRICHMENTS = 2


def _summarize_item(item: DetectedItem) -> str:
    """Compact JSON of the fields relevant for identification."""
    summary = item.model_dump(
        by_alias=True,
        exclude_none=True,
        include={"name", "description", "manufacturer", "model_number"},
    )
    return json.dumps(summary, ensure_ascii=False)


async def enrich_item(
    item: DetectedItem,
    image_data_uris: list[str],
    tags: list[dict[str, str]] | None = None,
    field_preferences: dict[str, str] | None = None,
    output_language: str | None = None,
    custom_fields: list[CustomFieldDefinition] | None = None,
) -> DetectedItem:
    """Refine a detected item using web search results for its codes.

    Never raises: on search miss or LLM failure the original item is returned.
    Quantity, serial number and purchase info always come from detection,
    since they describe this specific item, not the product.
    """
    results = await search_item(item)
    if not results:
        logger.debug(f"No web results for '{item.name}', keeping detection")
        return item

    system_prompt = build_enrichment_system_prompt(
        tags=tags,
        field_preferences=field_preferences,
        output_language=output_language,
        custom_fields=custom_fields,
    )
    user_prompt = build_enrichment_user_prompt(
        _summarize_item(item),
        [(r.title, r.url, r.snippet) for r in results],
    )

    try:
        parsed_content = await vision_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_data_uris=image_data_uris,
            response_model=get_single_item_response_model(custom_fields),
        )
        enriched = get_single_item_adapter(custom_fields).validate_python(parsed_content)
    except Exception as e:
        logger.warning(f"Web enrichment failed for '{item.name}', keeping detection: {e}")
        return item

    enriched = enriched.model_copy(
        update={
            "quantity": item.quantity,
            "parent_id": item.parent_id,
            "serial_number": item.serial_number,
            "purchase_price": item.purchase_price,
            "purchase_from": item.purchase_from,
            "tag_ids": enriched.tag_ids or item.tag_ids,
        }
    )

    logger.info(f"Web enrichment: '{item.name}' -> '{enriched.name}' ({len(results)} result(s))")
    logger.debug(f"  Sources: {[r.url for r in results]}")
    return enriched


async def enrich_items(
    items: list[DetectedItem],
    image_data_uris: list[str],
    tags: list[dict[str, str]] | None = None,
    field_preferences: dict[str, str] | None = None,
    output_language: str | None = None,
    custom_fields: list[CustomFieldDefinition] | None = None,
) -> list[DetectedItem]:
    """Refine detected items with web search when HBC_WEB_SEARCH_ENABLED is set.

    Returns the items unchanged (same order) when web search is disabled.
    """
    if not config.settings.web_search_enabled or not items:
        return items

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_ENRICHMENTS)

    async def enrich_one(item: DetectedItem) -> DetectedItem:
        async with semaphore:
            return await enrich_item(
                item,
                image_data_uris,
                tags=tags,
                field_preferences=field_preferences,
                output_language=output_language,
                custom_fields=custom_fields,
            )

    logger.info(f"Web enrichment for {len(items)} item(s)")
    return list(await asyncio.gather(*[enrich_one(item) for item in items]))
