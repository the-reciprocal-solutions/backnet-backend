from __future__ import annotations

from fastapi import APIRouter, Query

from bacnet_lab.adapters.http.dependencies import get_container

router = APIRouter(prefix="/api/forecast", tags=["forecast"])


@router.get("/info")
async def forecast_info() -> dict:
    """Model availability + config (chronos_available=False → naive fallback)."""
    return get_container().forecast_service.info()


@router.get("/{object_name:path}")
async def forecast_point(
    object_name: str,
    res: str = Query("1m", description="raw | 1m | 15m | 1h"),
    horizon: int = Query(12, ge=1, le=288, description="steps ahead"),
    lookback_s: int = Query(3600, ge=60, description="history window seconds"),
    store: bool = Query(True),
) -> dict:
    """Forecast one point. object_name e.g. AHU-01/SupplyAirTemp."""
    svc = get_container().forecast_service
    r = await svc.forecast_point(
        object_name, lookback_s=lookback_s, resolution=res, horizon=horizon, store=store
    )
    return {
        "object_name": r.object_name,
        "model": r.model,
        "made_at": r.made_at.isoformat(),
        "resolution": res,
        "horizon": horizon,
        "forecast": [
            {"time": t.isoformat(), "p10": a, "p50": b, "p90": c}
            for t, a, b, c in zip(r.horizon_ts, r.p10, r.p50, r.p90)
        ],
    }
