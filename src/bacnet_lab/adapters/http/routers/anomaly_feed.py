from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from bacnet_lab.adapters.http.dependencies import get_container

router = APIRouter(prefix="/api", tags=["anomaly-feed"])


class InjectRequest(BaseModel):
    point: str
    value: float


@router.get("/anomaly-feed")
async def anomaly_feed(include_acked: bool = False) -> dict:
    """Live grid source: enriched anomalies + auto-assigned work orders.

    Each item carries device, point, what (anomaly.kind), when (eta_hours),
    why (reason/explanation), severity and failure_prob.
    """
    feed = get_container().anomaly_feed
    items = feed.list_all() if include_acked else feed.list_active()
    return {"items": items, "status": feed.status()}


@router.post("/anomaly-feed/{feed_id}/ack")
async def ack_feed_item(feed_id: str) -> dict:
    feed = get_container().anomaly_feed
    if not feed.ack(feed_id):
        raise HTTPException(status_code=404, detail="Feed item not found")
    return {"ok": True, "feed_id": feed_id}


@router.post("/devices/{device_id}/inject-anomaly")
async def inject_anomaly(device_id: int, body: InjectRequest) -> dict:
    """One-shot fault injection: write an out-of-band value to a point so the
    detector fires and the pipeline produces an enriched anomaly + work order."""
    ds = get_container().device_service
    try:
        await ds.write_point_by_name(device_id, body.point, body.value)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "device_id": device_id, "point": body.point, "value": body.value}
