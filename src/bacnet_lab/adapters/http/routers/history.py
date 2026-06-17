from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query

from bacnet_lab.adapters.http.dependencies import get_container

router = APIRouter(prefix="/api/history", tags=["history"])

_RES = {"raw", "1m", "15m", "1h"}


def _parse_time(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return default


@router.get("/devices/latest")
async def devices_latest() -> list[dict]:
    """Pivot: every device with its points' latest values (JSON per device)."""
    return await get_container().tsdb.device_latest()


@router.get("/{object_name:path}")
async def point_history(
    object_name: str,
    res: str = Query("1m", description="raw | 1m | 15m | 1h"),
    frm: str | None = Query(None, alias="from", description="ISO-8601 start"),
    to: str | None = Query(None, description="ISO-8601 end"),
    limit: int = Query(5000, le=20000),
) -> dict:
    """Time series for one point. object_name e.g. AHU-01/SupplyAirTemp."""
    now = datetime.now(timezone.utc)
    resolution = res if res in _RES else "1m"
    end = _parse_time(to, now)
    start = _parse_time(frm, end - timedelta(hours=1))
    container = get_container()
    points = await container.tsdb.query_history(object_name, start, end, resolution, limit)
    return {
        "object_name": object_name,
        "resolution": resolution,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "count": len(points),
        "points": points,
    }
