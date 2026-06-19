from __future__ import annotations

from fastapi import APIRouter

from bacnet_lab.adapters.http.dependencies import get_container
from bacnet_lab.domain.enums import DeviceStatus

router = APIRouter(prefix="/api/discovery", tags=["discovery"])


@router.get("")
async def get_discovery() -> dict:
    container = get_container()
    devices = await container.device_service.list_devices()

    # Initialize protocol groupings
    result = {
        "bacnet": [],
        "mqtt": [],
        "knx": [],
        "modbus": []
    }

    # Populate device cards grouped by protocol
    for d in devices:
        protocol = d.protocol or "bacnet"
        if protocol not in result:
            result[protocol] = []
        result[protocol].append({
            "device_id": d.device_id,
            "name": d.name,
            "point_count": len(d.points),
            "status": d.status.value,
            "protocol": protocol,
        })

    # Calculate configured and discovered counts per protocol
    counts = {}
    for protocol in ["bacnet", "mqtt", "knx", "modbus"]:
        proto_devices = result[protocol]
        configured = len(proto_devices)
        if protocol == "bacnet":
            # For BACnet, discovered count is the number of online devices (responded to Who-Is)
            discovered = sum(1 for d in proto_devices if d["status"] == DeviceStatus.ONLINE.value)
        else:
            # Modbus, MQTT, KNX: list configured devices (discovered = configured)
            discovered = configured
        
        counts[protocol] = {
            "discovered": discovered,
            "configured": configured
        }

    # Add counts to the response
    result["counts"] = counts
    return result
