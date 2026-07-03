"""Time-series query API over the TimescaleDB historian.

Two read endpoints:
  * GET /api/timeseries/points    — catalog of stored points (filter discovery)
  * GET /api/timeseries/readings  — filtered, paginated readings across points

Filters compose with AND. Bucketed resolutions (1m/15m/1h) return continuous
aggregate rows (avg/min/max/last/n); `raw` returns individual readings.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query

from bacnet_lab.adapters.http.dependencies import get_container

router = APIRouter(prefix="/api/timeseries", tags=["timeseries"])

_RES = {"raw", "1m", "15m", "1h"}
_ORDER = {"asc", "desc"}


def _parse_time(value: str | None, default: datetime) -> datetime:
    if not value:
        return default
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid ISO-8601 datetime: {value}")
    # Normalise naive timestamps to UTC so comparisons against tz-aware rows work.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/points")
async def list_points(
    device_id: int | None = Query(None, description="filter by device id"),
    object_type: str | None = Query(None, description="filter by object type, e.g. analogInput"),
    q: str | None = Query(None, description="case-insensitive object_name substring"),
    limit: int = Query(1000, ge=1, le=10000),
) -> dict:
    """Catalog of stored points, for discovering valid `readings` filters."""
    tsdb = get_container().tsdb
    items = await tsdb.list_points(device_id=device_id, object_type=object_type, q=q, limit=limit)
    return {
        "filters": {"device_id": device_id, "object_type": object_type, "q": q},
        "count": len(items),
        "points": items,
    }


@router.get("/storage")
async def storage_by_device() -> dict:
    """Per-device storage footprint + daily growth in the time-series DB.

    For each device: points stored, readings written per day, and estimated
    bytes/day (rows/day × on-disk bytes/reading). Plus a fleet summary. Sizes
    are estimates derived from live hypertable stats.
    """
    stats = await get_container().tsdb.storage_by_device()

    def _mb(b):
        return round((b or 0) / 1_048_576, 3)

    for d in stats.get("devices", []):
        d["mb_per_day"] = _mb(d.get("bytes_per_day"))
    s = stats.get("summary", {})
    if s:
        s["hypertable_total_mb"] = _mb(s.get("hypertable_total_bytes"))
        s["fleet_mb_per_day"] = _mb(s.get("fleet_bytes_per_day"))
    return stats


@router.get("/readings")
async def query_readings(
    res: str = Query("1m", description="raw | 1m | 15m | 1h"),
    frm: str | None = Query(None, alias="from", description="ISO-8601 start (default: to - 1h)"),
    to: str | None = Query(None, description="ISO-8601 end (default: now)"),
    device_id: int | None = Query(None, description="filter by device id"),
    point: list[str] | None = Query(None, description="exact object_name(s); repeatable"),
    object_type: str | None = Query(None, description="filter by object type"),
    q: str | None = Query(None, description="case-insensitive object_name substring"),
    order: str = Query("asc", description="asc | desc (by time)"),
    limit: int = Query(5000, ge=1, le=20000),
    offset: int = Query(0, ge=0, description="pagination offset"),
) -> dict:
    """Filtered, paginated time-series readings across one or many points."""
    if res not in _RES:
        raise HTTPException(status_code=400, detail=f"res must be one of {sorted(_RES)}")
    if order not in _ORDER:
        raise HTTPException(status_code=400, detail="order must be 'asc' or 'desc'")

    now = datetime.now(timezone.utc)
    end = _parse_time(to, now)
    start = _parse_time(frm, end - timedelta(hours=1))
    if start > end:
        raise HTTPException(status_code=400, detail="'from' must be <= 'to'")

    tsdb = get_container().tsdb
    rows = await tsdb.query_readings(
        frm=start,
        to=end,
        resolution=res,
        device_id=device_id,
        object_names=point,
        object_type=object_type,
        q=q,
        order=order,
        limit=limit,
        offset=offset,
    )
    return {
        "filters": {
            "resolution": res,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "device_id": device_id,
            "point": point,
            "object_type": object_type,
            "q": q,
            "order": order,
        },
        "paging": {"limit": limit, "offset": offset, "returned": len(rows),
                   "has_more": len(rows) == limit},
        "count": len(rows),
        "readings": rows,
    }
