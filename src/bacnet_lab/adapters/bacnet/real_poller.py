"""Poll REAL BACnet/IP controllers as a client and feed values into the app.

This is the INGEST counterpart to ``BAC0Engine`` (which EXPOSES simulated
devices). Here we are a BACnet *client*: we open one BAC0 stack, read each
configured controller's object list once to build a ``Device`` + ``Point`` set,
persist it (without exposing it on the network), register it with the historian,
then loop reading ``presentValue`` on a fixed interval and ingest each reading
via ``DeviceService.ingest_point_value`` so the existing event -> historian ->
API/dashboard pipeline lights up unchanged.

Field notes baked in from a live Distech ECY-S1000 (DDC1) on site:
  * Many controllers reply ONLY to a Who-Is/read sourced from UDP 47808, so the
    stack MUST bind ``bind_port`` (default 47808), not a random ephemeral port.
  * That controller rejects ReadPropertyMultiple (raises on RPM), so every
    property is read with a single ReadProperty call, never ``readMultiple``.
  * ``objectList`` yields bacpypes3 ObjectIdentifiers whose ``str(type)`` is the
    dashed form ("binary-input"); reads must use the camelCase enum value
    ("binaryInput"), so we map between them explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

import yaml

from bacnet_lab.domain.enums import DeviceStatus, PointType
from bacnet_lab.domain.models.device import Device, Point

logger = logging.getLogger(__name__)

# bacpypes3 dashed object-type names (as they appear in objectList) -> PointType.
# Only readable point objects are mapped; structural objects (device/file/
# program/schedule/trend-log/...) are skipped.
_DASHED_TO_POINTTYPE: dict[str, PointType] = {
    "analog-input": PointType.ANALOG_INPUT,
    "analog-output": PointType.ANALOG_OUTPUT,
    "analog-value": PointType.ANALOG_VALUE,
    "binary-input": PointType.BINARY_INPUT,
    "binary-output": PointType.BINARY_OUTPUT,
    "binary-value": PointType.BINARY_VALUE,
    "multi-state-input": PointType.MULTI_STATE_INPUT,
    "multi-state-output": PointType.MULTI_STATE_OUTPUT,
    "multi-state-value": PointType.MULTI_STATE_VALUE,
}
_BINARY_TYPES = (PointType.BINARY_INPUT, PointType.BINARY_OUTPUT, PointType.BINARY_VALUE)


def _detect_bind_ip(target_ip: str) -> str:
    """Local IPv4 the OS would use to reach ``target_ip``.

    A datagram ``connect`` to the DEVICE (not to 8.8.8.8) makes the kernel pick
    the interface on the device's subnet — the correct NIC to bind on a
    multi-homed host (Wi-Fi + Ethernet + VPN), where the internet-egress trick
    would bind the wrong interface and never reach the controller.
    """
    for probe in (target_ip, "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 47808))
            ip = s.getsockname()[0]
            if ip and ip != "0.0.0.0":
                return ip
        except OSError:
            continue
        finally:
            s.close()
    return "127.0.0.1"


def _resolve_point_type(raw: object) -> PointType | None:
    """Map an objectList type token (dashed or camel) to a PointType, or None."""
    s = str(raw)
    pt = _DASHED_TO_POINTTYPE.get(s)
    if pt is not None:
        return pt
    try:
        return PointType(s)  # already camelCase (e.g. "binaryInput")
    except ValueError:
        return None


def _coerce(value: object, point_type: PointType) -> object:
    """Normalise a raw BACnet read into a stored value.

    Binary present values arrive as "active"/"inactive" (or bool) -> 1/0 so the
    historian and forecaster get a number. Analog values pass through as float
    when numeric; anything else is returned unchanged.
    """
    if point_type in _BINARY_TYPES:
        if isinstance(value, bool):
            return 1 if value else 0
        s = str(value).strip().lower()
        if s in ("active", "1", "true", "on"):
            return 1
        if s in ("inactive", "0", "false", "off"):
            return 0
        try:
            return int(float(s))
        except ValueError:
            return value
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return value


class RealBACnetPoller:
    def __init__(self, device_service, tsdb, event_publisher, settings) -> None:
        self._ds = device_service
        self._tsdb = tsdb
        self._events = event_publisher
        self._settings = settings
        self._bacnet = None
        self._task: asyncio.Task | None = None
        self._running = False
        # device_id -> [(PointType, instance, object_name), ...]
        self._point_map: dict[int, list[tuple[PointType, int, str]]] = {}
        self._addr: dict[int, str] = {}   # device_id -> "ip[:port]" for reads
        self._devices: list[Device] = []
        self._reads_ok = 0
        self._reads_err = 0

    # -- lifecycle --------------------------------------------------------- #
    async def start(self) -> None:
        if not getattr(self._settings, "enabled", False):
            logger.info("Real BACnet poller disabled (BACNET_LAB_REAL_ENABLED=false)")
            return
        entries = self._load_entries()
        if not entries:
            logger.warning("Real BACnet poller enabled but no devices configured in %s",
                           self._settings.config_path)
            return
        if not await self._connect(str(entries[0]["ip"])):
            return
        for entry in entries:
            try:
                await self._onboard(entry)
            except Exception as e:  # noqa: BLE001 — one bad device never aborts the rest
                logger.error("Real device onboard failed for %s: %s", entry, e, exc_info=True)
        if not self._point_map:
            logger.warning("Real BACnet poller: no readable points on any configured device")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Real BACnet poller started: %d device(s), %d point(s), every %.1fs",
                    len(self._devices), sum(len(v) for v in self._point_map.values()),
                    float(self._settings.poll_interval_s))

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._bacnet is not None:
            try:
                disc = getattr(self._bacnet, "_disconnect", None) or getattr(self._bacnet, "disconnect", None)
                if disc:
                    res = disc()
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:  # noqa: BLE001
                logger.debug("Real poller disconnect error", exc_info=True)
            self._bacnet = None
        logger.info("Real BACnet poller stopped")

    # -- shared-client + runtime attach ------------------------------------ #
    def borrow_client(self):
        """Return the live BACnet client for discovery to reuse, or None.

        These controllers reply only to source UDP 47808, so the poller and the
        discovery scan must share ONE socket. When the poller isn't connected
        (real mode off / no devices yet) this returns None and the discovery
        adapter opens its own short-lived stack on 47808 instead.
        """
        return self._bacnet

    async def attach_device(self, device, address: str) -> None:
        """Onboard a device discovered at runtime into the live poll loop.

        Called by the discovery 'Add' flow. The device is READ as a client
        (polled), never exposed on the network. Connects the stack and starts
        the loop on first use so this works even when the poller booted idle.
        """
        addr = (address or "").strip()
        if not addr:
            raise ValueError(f"attach_device: no address for device {device.device_id}")
        if self._bacnet is None:
            # Poller booted idle (no configured devices) — bring the stack up now.
            if not getattr(self._settings, "enabled", False):
                self._settings.enabled = True  # runtime opt-in via the wizard
            if not await self._connect():
                raise RuntimeError("attach_device: BACnet client unavailable (port 47808 busy?)")
        self._addr[device.device_id] = addr
        device.status = DeviceStatus.ONLINE
        self._point_map[device.device_id] = [
            (p.object_type, p.object_instance, p.object_name) for p in device.points
        ]
        if device not in self._devices:
            self._devices.append(device)
        if self._tsdb is not None and getattr(self._tsdb, "ready", False):
            try:
                await self._tsdb.register_devices([device])
            except Exception:  # noqa: BLE001 — historian is best-effort
                logger.debug("tsdb register failed for attached device %d",
                             device.device_id, exc_info=True)
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop())
        logger.info("Attached device %d '%s' @ %s (%d points) to poll loop",
                    device.device_id, device.name, addr, len(device.points))

    # -- setup ------------------------------------------------------------- #
    def _load_entries(self) -> list[dict]:
        path = Path(self._settings.config_path)
        if not path.exists():
            logger.warning("Real device config %s not found", path)
            return []
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        entries = data.get("devices") or []
        return [e for e in entries if isinstance(e, dict) and e.get("ip") and e.get("device_id") is not None]

    async def _connect(self, device_ip: str) -> bool:
        try:
            import BAC0
        except ImportError as e:  # pragma: no cover
            logger.error("BAC0 not installed; real BACnet poller unavailable: %s", e)
            return False
        ip = self._settings.bind_ip
        if not ip or ip == "0.0.0.0":
            ip = _detect_bind_ip(device_ip)
        cidr = int(self._settings.cidr)
        port = int(self._settings.bind_port)
        try:
            self._bacnet = BAC0.lite(ip=f"{ip}/{cidr}:{port}")
        except Exception as e:  # noqa: BLE001 — port likely already bound
            logger.error(
                "Real BACnet poller could not bind %s/%d:%d (%s). If the simulator "
                "holds this port, set BACNET_LAB_REAL_BIND_PORT or free UDP %d — many "
                "controllers reply only to source port 47808.", ip, cidr, port, e, port)
            self._bacnet = None
            return False
        await asyncio.sleep(0.5)
        logger.info("Real BACnet poller bound %s/%d:%d", ip, cidr, port)
        return True

    async def _read(self, addr: str, type_token: str, instance: int, prop: str,
                    timeout: float = 5.0):
        """Single ReadProperty (never RPM — some controllers reject it)."""
        return await asyncio.wait_for(
            self._bacnet.read(f"{addr} {type_token} {instance} {prop}"), timeout=timeout)

    async def _onboard(self, entry: dict) -> None:
        device_id = int(entry["device_id"])
        ip = str(entry["ip"]).strip()
        port = int(entry.get("port", 47808))
        addr = ip if port == 47808 else f"{ip}:{port}"
        self._addr[device_id] = addr

        # Device identity (best-effort; never blocks onboarding).
        name = str(entry.get("name") or f"BACnet {device_id}")
        vendor = model = ""
        for prop, setter in (("objectName", "name"), ("vendorName", "vendor"), ("modelName", "model")):
            try:
                v = await self._read(addr, "device", device_id, prop)
                if prop == "objectName" and v and not entry.get("name"):
                    name = str(v)
                elif prop == "vendorName":
                    vendor = str(v)
                elif prop == "modelName":
                    model = str(v)
            except Exception:  # noqa: BLE001
                logger.debug("device %s %s read failed", device_id, prop, exc_info=True)

        # Resolve the point set: explicit whitelist or discovered objectList.
        targets = await self._resolve_targets(entry, addr, device_id)
        if not targets:
            logger.warning("Real device %d (%s) exposed no readable points", device_id, name)
            return

        points: list[Point] = []
        pmap: list[tuple[PointType, int, str]] = []
        for pt, inst in targets:
            oname = f"{pt.value}-{inst}"
            try:
                raw = await self._read(addr, pt.value, inst, "objectName")
                if raw:
                    oname = str(raw)
            except Exception:  # noqa: BLE001
                logger.debug("objectName read failed %s %s:%s", addr, pt.value, inst, exc_info=True)
            units = ""
            if pt in (PointType.ANALOG_INPUT, PointType.ANALOG_OUTPUT, PointType.ANALOG_VALUE):
                try:
                    u = await self._read(addr, pt.value, inst, "units")
                    units = str(u) if u is not None else ""
                except Exception:  # noqa: BLE001
                    pass
            initial = 0
            try:
                pv = await self._read(addr, pt.value, inst, "presentValue")
                initial = _coerce(pv, pt)
            except Exception:  # noqa: BLE001
                logger.debug("initial presentValue read failed %s %s:%s", addr, pt.value, inst, exc_info=True)
            points.append(Point(
                object_type=pt, object_instance=inst, object_name=oname,
                present_value=initial, units=units,
            ))
            pmap.append((pt, inst, oname))

        desc = f"Real BACnet device @ {addr}"
        extra = " ".join(x for x in (vendor, model) if x)
        if extra:
            desc = f"{desc} ({extra})"
        device = Device(device_id=device_id, name=name, description=desc,
                        protocol="bacnet", points=points)
        device.status = DeviceStatus.ONLINE
        # Persist + hold in memory WITHOUT network exposure (we are a client, so
        # we must NOT call activate_device -> BAC0Engine.start_device).
        await self._ds.save_device(device)
        self._devices.append(device)
        self._point_map[device_id] = pmap

        if self._tsdb is not None and getattr(self._tsdb, "ready", False):
            try:
                await self._tsdb.register_devices([device])
            except Exception:  # noqa: BLE001 — historian is best-effort
                logger.debug("tsdb register failed for real device %d", device_id, exc_info=True)

        logger.info("Onboarded real device %d '%s' [%s %s] with %d points",
                    device_id, name, vendor, model, len(points))

    async def _resolve_targets(self, entry: dict, addr: str,
                               device_id: int) -> list[tuple[PointType, int]]:
        """Explicit `points:` whitelist if present, else discover objectList."""
        explicit = entry.get("points")
        if isinstance(explicit, list) and explicit:
            out: list[tuple[PointType, int]] = []
            for p in explicit:
                pt = _resolve_point_type(p.get("object_type"))
                inst = p.get("object_instance")
                if pt is not None and isinstance(inst, int):
                    out.append((pt, inst))
            return out
        try:
            obj_list = await self._read(addr, "device", device_id, "objectList", timeout=10)
        except Exception as e:  # noqa: BLE001
            logger.error("objectList read failed for device %d @ %s: %s", device_id, addr, e)
            return []
        out = []
        for item in (obj_list or []):
            try:
                otype, oinst = item
            except (TypeError, ValueError):
                continue
            pt = _resolve_point_type(otype)
            if pt is not None and isinstance(oinst, int):
                out.append((pt, oinst))
        # Cap per-device points so a large supervisor (e.g. a Niagara/FIN box
        # exposing hundreds of objects) can never stall the shared poll loop.
        cap = int(getattr(self._settings, "max_points_per_device", 0) or 0)
        if cap > 0 and len(out) > cap:
            logger.warning("Device %d exposed %d points; capping to %d",
                           device_id, len(out), cap)
            out = out[:cap]
        return out

    # -- poll loop --------------------------------------------------------- #
    async def _loop(self) -> None:
        interval = max(1.0, float(self._settings.poll_interval_s))
        try:
            while self._running:
                for device_id, points in list(self._point_map.items()):
                    addr = self._addr.get(device_id, "")
                    for pt, inst, oname in points:
                        if not self._running:
                            break
                        try:
                            raw = await self._read(addr, pt.value, inst, "presentValue")
                            value = _coerce(raw, pt)
                            await self._ds.ingest_point_value(device_id, oname, value)
                            self._reads_ok += 1
                        except Exception as e:  # noqa: BLE001 — one bad point never stalls the loop
                            self._reads_err += 1
                            logger.debug("poll read failed %s %s:%s: %s", addr, pt.value, inst, e)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Real poller loop crashed: %s", e, exc_info=True)
            self._running = False

    # -- introspection ----------------------------------------------------- #
    def status(self) -> dict:
        return {
            "enabled": bool(getattr(self._settings, "enabled", False)),
            "running": self._running,
            "devices": len(self._devices),
            "points": sum(len(v) for v in self._point_map.values()),
            "poll_interval_s": float(self._settings.poll_interval_s),
            "reads_ok": self._reads_ok,
            "reads_err": self._reads_err,
        }
