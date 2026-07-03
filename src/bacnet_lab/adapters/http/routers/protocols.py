"""Per-protocol device discovery + onboarding API.

One namespace per protocol — ``/api/<protocol>/...`` — all sharing the same
contract (verb, request/response shape, auth, error format); only the path
segment and the protocol-specific scan-config fields differ. Backs the
"Discover Devices & Points" wizard: select protocol -> configure scan ->
start discovery -> pick devices -> attach to an asset.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from bacnet_lab.adapters.http.dependencies import get_container
from bacnet_lab.ports.discovery import DiscoveryError

router = APIRouter(prefix="/api", tags=["discovery"])


def _svc():
    return get_container().discovery_service


@router.get("/protocols")
async def list_protocols() -> dict:
    """Protocol cards for the wizard: id, label, live device count, availability."""
    return {"protocols": _svc().protocols()}


@router.get("/{protocol}/config/schema")
async def config_schema(protocol: str) -> dict:
    """Scan-configuration field spec for one protocol (drives the config form)."""
    try:
        return {"protocol": protocol, "schema": _svc().config_schema(protocol)}
    except DiscoveryError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{protocol}/discover")
async def start_discover(protocol: str, config: dict = Body(default={})) -> dict:
    """Start a live discovery scan with protocol-specific settings.

    Returns a ``scan_id`` to poll — scans run in the background because network
    probes (Who-Is, unit sweep, broker wait) are slow.
    """
    try:
        scan_id = _svc().start_scan(protocol, config)
    except DiscoveryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"scan_id": scan_id, "protocol": protocol, "status": "running"}


@router.get("/{protocol}/discover/{scan_id}")
async def get_discover(protocol: str, scan_id: str) -> dict:
    """Poll a scan: status + discovered device list."""
    job = _svc().get_scan(scan_id)
    if not job or job.protocol != protocol:
        raise HTTPException(status_code=404, detail="Unknown scan_id for this protocol")
    return job.to_status()


@router.get("/{protocol}/discover/{scan_id}/devices/{ref}/objects")
async def get_device_objects(protocol: str, scan_id: str, ref: str) -> dict:
    """Per-device object/point detail (for the mapping step)."""
    objects = _svc().device_objects(scan_id, ref)
    if objects is None:
        raise HTTPException(status_code=404, detail="Unknown scan_id or device ref")
    return {"ref": ref, "count": len(objects), "objects": objects}


@router.post("/assets/{asset_id}/devices")
async def add_devices(asset_id: str, body: dict = Body(...)) -> dict:
    """Add selected discovered devices: persist, bring online, register storage.

    Body: ``{"scan_id": str, "refs": [str, ...]}``. ``asset_id`` ties the new
    devices to the asset being mapped in the wizard.
    """
    scan_id = body.get("scan_id")
    refs = body.get("refs") or []
    if not scan_id or not refs:
        raise HTTPException(status_code=400, detail="scan_id and refs are required")
    try:
        results = await _svc().add_devices(scan_id, refs)
    except DiscoveryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    added = [r for r in results if r.get("added")]
    return {"asset_id": asset_id, "added": len(added), "results": results}
