from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from bacnet_lab.adapters.http.dependencies import get_container
from bacnet_lab.adapters.knx.ets_import import parse_ets_file
from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Device, Point

router = APIRouter(prefix="/api", tags=["discovery"])

# All supported protocols. Every bucket is always present in the response,
# even when empty, so the frontend can rely on a stable shape.
PROTOCOLS = ("bacnet", "mqtt", "knx", "modbus")


@router.get("/discovery")
async def discovery() -> dict:
    """List devices grouped by the protocol they speak.

    Returns one bucket per known protocol (always all four), plus per-protocol
    counts and a grand total.
    """
    container = get_container()
    devices = await container.device_service.list_devices()

    buckets: dict[str, list[dict]] = {p: [] for p in PROTOCOLS}
    for d in devices:
        protocol = d.protocol or "bacnet"
        buckets.setdefault(protocol, [])
        buckets[protocol].append(
            {
                "device_id": d.device_id,
                "name": d.name,
                "point_count": len(d.points),
                "status": d.status.value,
            }
        )

    counts = {protocol: len(items) for protocol, items in buckets.items()}

    # KNX live-link status: connected gateway + how many group addresses are
    # exposed/subscribed. Absent engine (KNX disabled) reports disabled.
    knx_engine = getattr(container, "knx_engine", None)
    if knx_engine is not None and hasattr(knx_engine, "status"):
        knx_status = knx_engine.status()
    else:
        knx_status = {"connected": False, "gateway": None, "exposed": 0,
                      "subscribed": 0, "enabled": False}

    return {
        "protocols": buckets,
        "counts": counts,
        "total": sum(counts.values()),
        "knx_status": knx_status,
    }


@router.post("/discovery/import/ets")
async def import_ets(file: UploadFile = File(...)) -> dict:
    """Import a KNX ETS group-address export as a saved KNX device.

    Accepts an ETS CSV/XML/.knxproj upload, builds one Point per parseable
    group address, and persists a new device speaking the ``knx`` protocol.
    """
    content = await file.read()
    try:
        entries = parse_ets_file(file.filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not entries:
        raise HTTPException(status_code=400, detail="No group addresses found in file")

    service = get_container().device_service
    device_id = await service.next_device_id()
    stem = Path(file.filename or "ets").stem
    name = f"KNX Import ({stem})"

    points: list[Point] = []
    skipped = 0
    for entry in entries:
        if not entry.group_address:
            skipped += 1
            continue
        i = len(points) + 1
        dpt = entry.dpt
        if dpt == "1" or dpt.startswith("1."):
            object_type = PointType.BINARY_VALUE
        else:
            object_type = PointType.ANALOG_VALUE
        points.append(
            Point(
                object_type=object_type,
                object_instance=i,
                object_name=entry.name or entry.group_address,
                group_address=entry.group_address,
                dpt=entry.dpt,
                present_value=0,
            )
        )

    device = Device(
        device_id=device_id,
        name=name,
        description=f"Imported from ETS file {file.filename}",
        protocol="knx",
        points=points,
    )
    await service.save_device(device)

    # Bring the device online: expose + subscribe its group addresses on the KNX
    # engine (so inbound telegrams update its points) and register it with the
    # historian so the regular-grid sampler stores its readings.
    container = get_container()
    await service.activate_device(device)
    if container.tsdb.ready:
        try:
            await container.tsdb.register_devices([device])
        except Exception:  # historian is best-effort; never fail the import
            pass

    return {
        "imported": len(points),
        "device_id": device_id,
        "device_name": name,
        "skipped": skipped,
        "protocol": "knx",
        "status": device.status.value,
    }
