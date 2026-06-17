from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import StreamingResponse

from bacnet_lab.adapters.http.dependencies import get_container
from bacnet_lab.adapters.http.schemas import SnapshotPointResponse

router = APIRouter(prefix="/api/simulation", tags=["simulation"])


def _snapshot() -> list[dict]:
    """Flat list of every point's current value across all in-memory devices."""
    container = get_container()
    out: list[dict] = []
    for device in container.device_service.get_all_in_memory_devices():
        for point in device.points:
            out.append({
                "device_id": device.device_id,
                "device_name": device.name,
                "point_name": point.object_name,
                "object_type": point.object_type.value,
                "value": point.present_value,
                "units": point.units,
            })
    return out


@router.get("/status")
async def get_status() -> dict:
    container = get_container()
    return container.simulation_engine.status()


@router.get("/generators")
async def list_generators() -> list[dict]:
    container = get_container()
    return container.simulation_engine.list_generators()


@router.post("/start")
async def start() -> dict:
    container = get_container()
    await container.simulation_engine.start()
    return container.simulation_engine.status()


@router.post("/stop")
async def stop() -> dict:
    container = get_container()
    await container.simulation_engine.stop()
    return container.simulation_engine.status()


@router.get("/faults")
async def list_faults() -> list[dict]:
    container = get_container()
    return container.simulation_engine.active_faults()


@router.post("/faults")
async def inject_fault(body: dict = Body(...)) -> dict:
    container = get_container()
    point_key = body["point_key"]
    kind = body["kind"]
    duration_s = body.get("duration_s")
    params = body.get("params") or {}
    return container.simulation_engine.inject_fault(
        point_key, kind, duration_s=duration_s, **params
    )


@router.delete("/faults")
async def clear_faults(point_key: str | None = Query(default=None)) -> dict:
    container = get_container()
    return container.simulation_engine.clear_faults(point_key)


@router.get("/snapshot", response_model=list[SnapshotPointResponse])
async def snapshot() -> list[SnapshotPointResponse]:
    return [SnapshotPointResponse(**p) for p in _snapshot()]


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            data = json.dumps(_snapshot())
            yield f"data: {data}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
