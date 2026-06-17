from __future__ import annotations

from fastapi import APIRouter, HTTPException

from bacnet_lab.adapters.http.dependencies import get_container
from bacnet_lab.adapters.http.schemas import (
    AssetHealth,
    FleetKpi,
    PredictionItem,
)

router = APIRouter(prefix="/api", tags=["predictions"])


@router.get("/predictions", response_model=list[PredictionItem])
async def list_predictions() -> list[PredictionItem]:
    """Scan critical analog points for forecast-projected envelope breaches."""
    svc = get_container().prediction_service
    return await svc.scan_predictions()


@router.get("/assets/{asset_id}/health", response_model=AssetHealth)
async def asset_health(asset_id: str) -> AssetHealth:
    """Per-asset health score, status, active alarms, predictions and RUL."""
    svc = get_container().prediction_service
    result = await svc.asset_health(asset_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return result


@router.get("/kpi", response_model=FleetKpi)
async def fleet_kpi() -> FleetKpi:
    """Fleet-wide health, at-risk count, active alarms and predicted failures."""
    svc = get_container().prediction_service
    return await svc.fleet_kpi()
