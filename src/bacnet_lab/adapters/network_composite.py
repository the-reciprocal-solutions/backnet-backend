"""Composite device-network: run one set of devices over MANY protocols.

Wraps several ``DeviceNetworkPort`` engines (BACnet + MQTT + KNX + ...) behind
the single port the application depends on. Lifecycle/write calls fan out to
every engine; reads come from the PRIMARY engine (first in the list), which
owns the authoritative point state.

One engine failing never breaks the others — each call is isolated.
"""

from __future__ import annotations

import logging

from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Device
from bacnet_lab.domain.value_objects import PointValue
from bacnet_lab.ports.device_network import DeviceNetworkPort

logger = logging.getLogger(__name__)


class CompositeDeviceNetwork(DeviceNetworkPort):
    def __init__(self, engines: list[DeviceNetworkPort]) -> None:
        if not engines:
            raise ValueError("CompositeDeviceNetwork needs at least one engine")
        self._engines = engines
        self._primary = engines[0]

    async def _fan(self, method: str, *args) -> None:
        for engine in self._engines:
            try:
                await getattr(engine, method)(*args)
            except Exception as e:
                logger.error("%s.%s failed: %s", type(engine).__name__, method, e)

    async def start_device(self, device: Device, udp_port: int) -> None:
        await self._fan("start_device", device, udp_port)

    async def stop_device(self, device_id: int) -> None:
        await self._fan("stop_device", device_id)

    async def stop_all(self) -> None:
        await self._fan("stop_all")

    async def write_point_value(
        self, device_id: int, object_type: PointType, instance: int, value: PointValue
    ) -> None:
        await self._fan("write_point_value", device_id, object_type, instance, value)

    async def read_point_value(
        self, device_id: int, object_type: PointType, instance: int
    ) -> PointValue:
        # Primary engine owns authoritative state.
        return await self._primary.read_point_value(device_id, object_type, instance)
