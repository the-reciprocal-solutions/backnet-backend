from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from bacnet_lab.adapters.http.dependencies import get_container

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


class AskRequest(BaseModel):
    question: str
    object_name: str | None = None


@router.get("/info")
async def copilot_info() -> dict:
    return get_container().copilot_service.info()


@router.get("/explain/{object_name:path}")
async def explain(
    object_name: str,
    horizon: int = Query(6, ge=1, le=288),
    res: str = Query("1m"),
    window_s: int = Query(1800, ge=60),
) -> dict:
    """Forecast + grounded reasoning for one point (Chronos + DB evidence + LLM)."""
    r = await get_container().copilot_service.explain(
        object_name, horizon=horizon, resolution=res, window_s=window_s
    )
    return {
        "object_name": r.object_name,
        "predicted_value": r.predicted_value,
        "units": r.units,
        "horizon": r.horizon,
        "forecast": r.forecast,
        "answer": r.answer,
        "evidence": r.evidence,
        "llm_model": r.llm_model,
        "grounded": r.grounded,
        **r.extras,
    }


@router.post("/ask")
async def ask(req: AskRequest) -> dict:
    """Free-form question, optionally grounded on a named point's evidence."""
    return await get_container().copilot_service.ask(req.question, req.object_name)
