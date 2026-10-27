"""Used-market valuation API routes.

Homebox is the source of truth for an item's used value (a custom field).
Estimates are kept locally as proposals and written to Homebox only by
``POST /valuations/apply``, after the user approves them.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from homebox_companion import HomeboxGateway
from homebox_companion.core import config
from homebox_companion.tools.valuation import JobState, Valuation, ValuationStore
from homebox_companion.tools.valuation.homebox_field import read_homebox_value

from ..dependencies import get_verified_gateway, require_auth, require_llm_configured
from ..services.valuation_jobs import (
    ValuationJobManager,
    apply_valuations,
    discard_valuations,
    list_all_items,
    read_item_details,
)

router = APIRouter()


class StartJobRequest(BaseModel):
    mode: Literal["missing", "refresh"]


class ItemIdsRequest(BaseModel):
    item_ids: list[str] = Field(alias="itemIds", min_length=1, max_length=5000)


def get_valuation_jobs(request: Request) -> ValuationJobManager:
    manager = getattr(request.app.state, "valuation_jobs", None)
    if manager is None:
        manager = ValuationJobManager(ValuationStore())
        request.app.state.valuation_jobs = manager
    return manager


def _job_view(job: JobState | None) -> dict[str, Any] | None:
    """Job progress without the (large) queue."""
    if job is None:
        return None
    return {
        "runId": job.run_id,
        "mode": job.mode,
        "status": job.status,
        "total": len(job.queue),
        "done": len(job.done_ids),
        "failed": len(job.failed),
        "currentItem": job.current_item,
        "error": job.error,
        "startedAt": job.started_at.isoformat(),
        "finishedAt": job.finished_at.isoformat() if job.finished_at else None,
    }


def _valuation_view(valuation: Valuation | None) -> dict[str, Any] | None:
    if valuation is None:
        return None
    return {
        "value": valuation.value,
        "currency": valuation.currency,
        "low": valuation.low,
        "high": valuation.high,
        "confidence": valuation.confidence,
        "reasoning": valuation.reasoning,
        "sources": [s.model_dump() for s in valuation.sources],
        "valuedAt": valuation.valued_at.isoformat(),
    }


@router.get("/valuations")
async def list_valuations(
    gateway: Annotated[HomeboxGateway, Depends(get_verified_gateway)],
    manager: Annotated[ValuationJobManager, Depends(get_valuation_jobs)],
) -> dict[str, Any]:
    """Every current Homebox item with its value in Homebox and any pending estimate.

    Only items visible to the caller are returned.
    """
    data = manager.store.load()
    rows = []
    for item in await read_item_details(gateway, await list_all_items(gateway)):
        record = data.records.get(item["id"])
        homebox_value = read_homebox_value(item)
        pending = record is not None and record.is_pending
        estimate = record.current if record else None
        change_pct = None
        if pending and estimate and homebox_value and homebox_value.value > 0:
            change_pct = round((estimate.value - homebox_value.value) / homebox_value.value * 100, 1)
        rows.append(
            {
                "id": item["id"],
                "name": item.get("name") or "",
                "thumbnailId": item.get("thumbnailId"),
                "homeboxValue": (
                    {"value": homebox_value.value, "currency": homebox_value.currency, "text": homebox_value.text}
                    if homebox_value
                    else None
                ),
                "estimate": _valuation_view(estimate),
                "pending": pending,
                "changePct": change_pct,
                "lastRunId": record.last_run_id if record else None,
                "lastError": record.last_error if record else None,
            }
        )
    return {
        "items": rows,
        "job": _job_view(manager.status()),
        "fieldName": config.settings.valuation_field_name,
        "currency": config.settings.valuation_currency,
    }


@router.post("/valuations/apply")
async def apply(
    body: ItemIdsRequest,
    gateway: Annotated[HomeboxGateway, Depends(get_verified_gateway)],
    manager: Annotated[ValuationJobManager, Depends(get_valuation_jobs)],
) -> dict[str, Any]:
    """Write the approved estimates to Homebox (only the value custom field changes)."""
    return await apply_valuations(gateway, manager.store, body.item_ids)


@router.post("/valuations/discard", dependencies=[Depends(require_auth)])
async def discard(
    body: ItemIdsRequest,
    manager: Annotated[ValuationJobManager, Depends(get_valuation_jobs)],
) -> dict[str, Any]:
    """Drop pending estimates without touching Homebox."""
    return {"discarded": discard_valuations(manager.store, body.item_ids)}


@router.get("/valuations/job", dependencies=[Depends(require_auth)])
async def get_job(manager: Annotated[ValuationJobManager, Depends(get_valuation_jobs)]) -> dict[str, Any]:
    return {"job": _job_view(manager.status())}


@router.post("/valuations/job", dependencies=[Depends(require_llm_configured)])
async def start_job(
    body: StartJobRequest,
    gateway: Annotated[HomeboxGateway, Depends(get_verified_gateway)],
    manager: Annotated[ValuationJobManager, Depends(get_valuation_jobs)],
) -> dict[str, Any]:
    return {"job": _job_view(manager.start(body.mode, gateway))}


@router.post("/valuations/job/resume", dependencies=[Depends(require_auth), Depends(require_llm_configured)])
async def resume_job(manager: Annotated[ValuationJobManager, Depends(get_valuation_jobs)]) -> dict[str, Any]:
    return {"job": _job_view(manager.resume())}


@router.post("/valuations/job/cancel", dependencies=[Depends(require_auth)])
async def cancel_job(manager: Annotated[ValuationJobManager, Depends(get_valuation_jobs)]) -> dict[str, Any]:
    await manager.cancel()
    return {"job": _job_view(manager.status())}
