"""KNX device-network adapter.

Exposes simulated points over KNX/IP via ``xknx``. KNX addresses are
group addresses (e.g. ``1/2/3``). Each point carries its own ``group_address``
and ``dpt`` (KNX datapoint type) taken from config / ETS import; points
without a group address are simply not KNX-exposed. The mapping is kept in
memory so it can be exported to an ETS-style table later.

Values are encoded per DPT (boolean 1.x, 8-bit/percent 5.x, 2-byte float 9.x;
other types fall back to a raw byte). The engine also INGESTS live values from
the bus: inbound ``GroupValueWrite``/``GroupValueResponse`` telegrams for known
group addresses are decoded by DPT and update the in-memory point state (which
the historian samples). ``read_point_value`` triggers a real ``GroupValueRead``
so devices respond, then returns the latest cached value (eventually
consistent). Implements ``DeviceNetworkPort`` for the ``CompositeDeviceNetwork``.
``xknx`` is imported lazily; if missing or the KNX gateway is unreachable, the
engine logs and degrades to a no-op (boot never breaks).
"""

from __future__ import annotations

import logging

from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Device
from bacnet_lab.domain.value_objects import PointValue
from bacnet_lab.ports.device_network import DeviceNetworkPort

logger = logging.getLogger(__name__)


def _dpt_main(dpt: str) -> int | None:
    """Parse the DPT main number, e.g. "5.001" -> 5, "1" -> 1, "" -> None."""
    if not dpt:
        return None
    try:
        return int(str(dpt).split(".", 1)[0])
    except (ValueError, TypeError):
        return None


def _knx_float16(v) -> list[int]:
    """Manual KNX 2-byte float (DPT 9.x) encoding fallback."""
    v = float(v)
    data = round(v * 100)
    e = 0
    while data < -2048 or data > 2047:
        data >>= 1
        e += 1
    m = data & 0x7FF
    s = 0x8000 if data < 0 else 0
    hi = s | (e << 11) | m
    return [(hi >> 8) & 0xFF, hi & 0xFF]


def _knx_float16_decode(raw) -> float:
    """Manual KNX 2-byte float (DPT 9.x) decoding — inverse of ``_knx_float16``."""
    if len(raw) < 2:
        return 0.0
    hi, lo = raw[0] & 0xFF, raw[1] & 0xFF
    val = (hi << 8) | lo
    sign = -1 if (val & 0x8000) else 1
    exp = (val >> 11) & 0x0F
    mant = val & 0x07FF
    if sign < 0:
        mant -= 2048  # two's-complement-style mantissa used by KNX 9.x
    return round((mant << exp) / 100.0, 3)


class KnxEngine(DeviceNetworkPort):
    def __init__(self, gateway_ip: str = "", gateway_port: int = 3671) -> None:
        self._gateway_ip = gateway_ip
        self._gateway_port = gateway_port
        self._xknx = None
        self._devices: dict[int, Device] = {}
        # (device_id, object_name) -> group address
        self._ga: dict[tuple[int, str], str] = {}
        # group_address -> (device_id, object_name, dpt)
        self._rev: dict[str, tuple[int, str, str]] = {}
        # ga -> last decoded value (for status/debug)
        self._last: dict[str, object] = {}
        # optional async callback(device_id:int, object_name:str, value) -> awaitable
        self.on_bus_value = None

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
            try:
                xknx.telegram_queue.register_telegram_received_cb(self._on_telegram)
            except Exception as e:
                logger.debug("KNX inbound callback registration failed: %s "
                             "(sending still works)", e)
            logger.info("KNX engine started (gateway=%s)", self._gateway_ip or "multicast-routing")
            return True
        except Exception as e:
            logger.warning("KNX start failed (gateway=%s): %s — engine disabled",
                           self._gateway_ip or "routing", e)
            return False

    def _encode(self, value: PointValue, dpt: str = ""):
        """Encode ``value`` into an xknx payload object per its DPT.

        Returns a ``DPTArray`` or ``DPTBinary``. Unknown/empty DPTs fall back
        to the legacy raw-byte behaviour.
        """
        from xknx.dpt import DPTArray, DPTBinary

        main = _dpt_main(dpt)

        if main == 1:  # boolean
            return DPTBinary(1 if value else 0)

        if main == 5:  # 8-bit unsigned / percent
            if str(dpt) == "5.001":  # percent 0..100 -> 0..255
                raw = round(float(value) / 100 * 255)
            else:
                raw = int(float(value))
            raw = max(0, min(255, raw))
            return DPTArray(raw & 0xFF)

        if main == 9:  # 2-byte float
            try:
                from xknx.dpt import DPT2ByteFloat

                return DPTArray(DPT2ByteFloat.to_knx(float(value)))
            except Exception:
                return DPTArray(_knx_float16(value))

        # default / unknown DPT: legacy raw byte
        raw = int(float(value)) & 0xFF if isinstance(value, (int, float)) else 0
        return DPTArray(raw)

    def _decode(self, payload, dpt: str = ""):
        """Decode an xknx payload object into a Python value — inverse of ``_encode``."""
        from xknx.dpt import DPTArray, DPTBinary

        main = _dpt_main(dpt)

        if isinstance(payload, DPTBinary):
            return bool(payload.value)

        # DPTArray: payload.value is a tuple of ints
        raw = list(payload.value)
        if not raw:
            return 0

        if main == 1:
            return bool(raw[0]) if raw else False

        if main == 5:
            b = raw[0] if raw else 0
            if str(dpt) == "5.001":  # percent 0..255 -> 0..100
                return round(b / 255 * 100, 1)
            return b

        if main == 9:
            try:
                from xknx.dpt import DPT2ByteFloat

                return round(float(DPT2ByteFloat.from_knx(DPTArray(tuple(raw)))), 3)
            except Exception:
                return _knx_float16_decode(raw)

        return raw[0] if raw else 0

    async def _on_telegram(self, telegram) -> None:
        """Ingest an inbound telegram: decode by DPT and update in-memory point state."""
        try:
            from xknx.telegram.apci import GroupValueWrite, GroupValueResponse

            if not isinstance(telegram.payload, (GroupValueWrite, GroupValueResponse)):
                return  # ignore reads / other APCI services
            ga = str(telegram.destination_address)  # "1/2/3"
            ref = self._rev.get(ga)
            if ref is None:
                return  # not one of our points
            device_id, object_name, dpt = ref
            value = self._decode(telegram.payload.value, dpt)
            device = self._devices.get(device_id)
            if device:
                for point in device.points:
                    if point.object_name == object_name:
                        point.present_value = value
                        break
            self._last[ga] = value
            if self.on_bus_value is not None:
                await self.on_bus_value(device_id, object_name, value)
        except Exception as e:
            logger.debug("KNX inbound telegram ignored: %s", e)

    async def _write_ga(self, ga: str, value: PointValue, dpt: str = "") -> None:
        if self._xknx is None:
            return
        try:
            from xknx.telegram import Telegram, GroupAddress
            from xknx.telegram.apci import GroupValueWrite

            try:
                encoded = self._encode(value, dpt)
            except Exception as e:
                logger.debug("KNX encode for %s (dpt=%s) failed: %s", ga, dpt, e)
                return
            await self._xknx.telegrams.put(
                Telegram(
                    destination_address=GroupAddress(ga),
                    payload=GroupValueWrite(encoded),
                )
            )
        except Exception as e:
            logger.debug("KNX write to %s skipped: %s", ga, e)

    # ------------------------------------------------------------------ #
    async def start_device(self, device: Device, udp_port: int) -> None:
        if not await self._ensure_xknx():
            return
        self._devices[device.device_id] = device
        exposed = 0
        for point in device.points:
            ga = point.group_address
            if not ga:  # not KNX-exposed
                continue
            self._ga[(device.device_id, point.object_name)] = ga
            self._rev[ga] = (device.device_id, point.object_name, point.dpt)
            await self._write_ga(ga, point.present_value, point.dpt)
            exposed += 1
        logger.info("KNX exposed device %s (ID=%d, %d group addresses)",
                    device.name, device.device_id, exposed)

    async def stop_device(self, device_id: int) -> None:
        self._devices.pop(device_id, None)
        self._ga = {k: v for k, v in self._ga.items() if k[0] != device_id}
        self._rev = {k: v for k, v in self._rev.items() if v[0] != device_id}

    async def stop_all(self) -> None:
        self._devices.clear()
        self._ga.clear()
        self._rev.clear()
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
            await self._write_ga(ga, value, point.dpt)

    async def read_point_value(
        self, device_id: int, object_type: PointType, instance: int
    ) -> PointValue:
        device = self._devices.get(device_id)
        if not device:
            raise ValueError(f"Device {device_id} not found")
        point = device.get_point(object_type, instance)
        if not point:
            raise ValueError(f"Point {object_type}:{instance} not found on device {device_id}")
        # Best-effort: trigger a real KNX GroupValueRead so the device responds.
        # The response arrives via _on_telegram and updates the cache asynchronously.
        ga = self._ga.get((device_id, point.object_name))
        if ga and self._xknx is not None:
            try:
                from xknx.telegram import Telegram, GroupAddress
                from xknx.telegram.apci import GroupValueRead

                await self._xknx.telegrams.put(
                    Telegram(
                        destination_address=GroupAddress(ga),
                        payload=GroupValueRead(),
                    )
                )
            except Exception as e:
                logger.debug("KNX GroupValueRead to %s skipped: %s", ga, e)
        # Return the latest cached value (eventually consistent: the
        # GroupValueRead response updates it shortly after).
        return point.present_value

    def status(self) -> dict:
        return {
            "connected": self._xknx is not None,
            "gateway": self._gateway_ip or "multicast-routing",
            "exposed": len(self._ga),
            "subscribed": len(self._rev),
        }
