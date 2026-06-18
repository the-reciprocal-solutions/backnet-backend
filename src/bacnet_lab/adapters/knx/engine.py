"""KNX device-network adapter.

Exposes simulated points over KNX/IP via ``xknx``. KNX addresses are
group addresses (e.g. ``1/2/3``), so each point is mapped to a deterministic
group address derived from (device_id, point index). The mapping is kept in
memory and logged so it can be exported to an ETS-style table later.

Implements ``DeviceNetworkPort`` for the ``CompositeDeviceNetwork``. ``xknx``
is imported lazily; if missing or the KNX gateway is unreachable, the engine
logs and degrades to a no-op (boot never breaks).
"""

from __future__ import annotations

import logging

from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Device
from bacnet_lab.domain.value_objects import PointValue
from bacnet_lab.ports.device_network import DeviceNetworkPort

logger = logging.getLogger(__name__)


def _group_address(device_id: int, point_index: int) -> str:
    """Deterministic 3-level group address: main/middle/sub.

    main  = device_id mod 32 (KNX main group is 0-31)
    middle= point_index // 256 (0-7)
    sub   = point_index mod 256 (0-255)
    """
    main = device_id % 32
    middle = (point_index // 256) % 8
    sub = point_index % 256
    return f"{main}/{middle}/{sub}"


class KnxEngine(DeviceNetworkPort):
    def __init__(self, gateway_ip: str = "", gateway_port: int = 3671) -> None:
        self._gateway_ip = gateway_ip
        self._gateway_port = gateway_port
        self._xknx = None
        self._devices: dict[int, Device] = {}
        # (device_id, object_name) -> group address
        self._ga: dict[tuple[int, str], str] = {}

    async def _ensure_xknx(self) -> bool:
        if self._xknx is not None:
            return True
        try:
            from xknx import XKNX
            from xknx.io import ConnectionConfig, ConnectionType
        except ImportError:
            logger.warning("xknx not installed; KNX engine disabled (pip install xknx)")
            return False
        try:
            if self._gateway_ip:
                conn = ConnectionConfig(
                    connection_type=ConnectionType.TUNNELING,
                    gateway_ip=self._gateway_ip,
                    gateway_port=self._gateway_port,
                )
            else:
                conn = ConnectionConfig(connection_type=ConnectionType.ROUTING)
            xknx = XKNX(connection_config=conn)
            await xknx.start()
            self._xknx = xknx
            logger.info("KNX engine started (gateway=%s)", self._gateway_ip or "multicast-routing")
            return True
        except Exception as e:
            logger.warning("KNX start failed (gateway=%s): %s — engine disabled",
                           self._gateway_ip or "routing", e)
            return False

    async def _write_ga(self, ga: str, value: PointValue) -> None:
        if self._xknx is None:
            return
        try:
            from xknx.dpt import DPTArray
            from xknx.telegram import Telegram, GroupAddress
            from xknx.telegram.apci import GroupValueWrite

            raw = int(float(value)) & 0xFF if isinstance(value, (int, float)) else 0
            await self._xknx.telegrams.put(
                Telegram(
                    destination_address=GroupAddress(ga),
                    payload=GroupValueWrite(DPTArray(raw)),
                )
            )
        except Exception as e:
            logger.debug("KNX write to %s skipped: %s", ga, e)

    # ------------------------------------------------------------------ #
    async def start_device(self, device: Device, udp_port: int) -> None:
        if not await self._ensure_xknx():
            return
        self._devices[device.device_id] = device
        for idx, point in enumerate(device.points):
            ga = _group_address(device.device_id, idx)
            self._ga[(device.device_id, point.object_name)] = ga
            await self._write_ga(ga, point.present_value)
        logger.info("KNX exposed device %s (ID=%d, %d group addresses)",
                    device.name, device.device_id, len(device.points))

    async def stop_device(self, device_id: int) -> None:
        self._devices.pop(device_id, None)
        self._ga = {k: v for k, v in self._ga.items() if k[0] != device_id}

    async def stop_all(self) -> None:
        self._devices.clear()
        self._ga.clear()
        if self._xknx is not None:
            try:
                await self._xknx.stop()
            except Exception as e:
                logger.debug("KNX stop error: %s", e)
            self._xknx = None

    async def write_point_value(
        self, device_id: int, object_type: PointType, instance: int, value: PointValue
    ) -> None:
        device = self._devices.get(device_id)
        if not device:
            return
        point = device.get_point(object_type, instance)
        if not point:
            return
        point.present_value = value
        ga = self._ga.get((device_id, point.object_name))
        if ga:
            await self._write_ga(ga, value)

    async def read_point_value(
        self, device_id: int, object_type: PointType, instance: int
    ) -> PointValue:
        device = self._devices.get(device_id)
        if not device:
            raise ValueError(f"Device {device_id} not found")
        point = device.get_point(object_type, instance)
        if not point:
            raise ValueError(f"Point {object_type}:{instance} not found on device {device_id}")
        return point.present_value
