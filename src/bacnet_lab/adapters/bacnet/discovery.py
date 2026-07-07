"""BACnet/IP live discovery adapter.

Discovers real controllers by **subnet sweep + wildcard device read**, not
Who-Is. Field controllers on the target site (Distech ECY series) answer
``ReadProperty`` but ignore Who-Is entirely and reply only to a client bound to
source UDP 47808 — so a Who-Is broadcast (from any port) finds nothing. Instead
we read the wildcard device instance ``4194303`` on every host in the subnet;
a device answers with its real ``objectIdentifier``/``objectName``. When
``deep_scan`` is set, each device's ``objectList`` and per-object
name/value/units are read (single ReadProperty calls — these controllers reject
ReadPropertyMultiple) so the wizard can map real points.

The scan shares the real poller's single UDP-47808 client when one is running
(the poller and discovery must not fight over that socket); otherwise it opens
its own short-lived stack and disconnects it in a ``finally`` block.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

from bacnet_lab.domain.models.discovery import DiscoveredDevice, DiscoveredPoint
from bacnet_lab.ports.discovery import DiscoveryError, ProtocolDiscoveryPort

logger = logging.getLogger(__name__)

# Hard cap on per-device object reads so a huge controller (or a KNX->BACnet
# gateway exposing hundreds of group addresses) never makes one scan run away.
_DEFAULT_MAX_OBJECTS = 300
# BACnet "unconfigured" wildcard device instance. Reading it targets whatever
# device answers at an address, which reports its real id/name back.
_WILDCARD_INSTANCE = 4194303

# bacpypes3 dashed object-type names (as they appear in objectList) -> the
# camelCase form the BAC0 read parser expects. Only readable point objects are
# mapped; structural objects (device/file/program/schedule/...) are skipped.
_DASHED_TO_CAMEL = {
    "analog-input": "analogInput",
    "analog-output": "analogOutput",
    "analog-value": "analogValue",
    "binary-input": "binaryInput",
    "binary-output": "binaryOutput",
    "binary-value": "binaryValue",
    "multi-state-input": "multiStateInput",
    "multi-state-output": "multiStateOutput",
    "multi-state-value": "multiStateValue",
}
_ANALOG_CAMEL = ("analogInput", "analogOutput", "analogValue")


def _detect_host_ip() -> str:
    """Best-effort primary non-loopback IPv4 of this host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet sent; just forces the OS to pick the egress interface.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _camel_type(raw: object) -> str | None:
    """Map an objectList type token (dashed or camel) to a readable camel name."""
    s = str(raw)
    if s in _DASHED_TO_CAMEL:
        return _DASHED_TO_CAMEL[s]
    if s in _DASHED_TO_CAMEL.values():
        return s
    return None


def _expand_range(frm: str, to: str) -> list[str]:
    """Inclusive list of IPv4 addresses between two dotted quads."""
    try:
        a = int(ipaddress.IPv4Address(frm.strip()))
        b = int(ipaddress.IPv4Address(to.strip()))
    except (ValueError, AttributeError):
        return []
    if b < a:
        a, b = b, a
    if b - a > 65535:  # safety bound
        b = a + 65535
    return [str(ipaddress.IPv4Address(i)) for i in range(a, b + 1)]


class BACnetDiscovery(ProtocolDiscoveryPort):
    protocol = "bacnet"
    label = "BACnet/IP"

    def __init__(self, local_ip: str = "0.0.0.0", poller=None) -> None:
        self._local_ip = local_ip
        self._poller = poller  # RealBACnetPoller — shares its 47808 client

    def config_schema(self) -> dict:
        return {
            "fields": [
                {"name": "scan_mode", "label": "Scan Mode", "type": "select",
                 "options": ["subnet_sweep", "ip_range"], "default": "subnet_sweep", "required": True},
                {"name": "ip_range_from", "label": "IP Range From", "type": "ip", "default": "", "required": False, "placeholder": "192.168.20.1"},
                {"name": "ip_range_to", "label": "IP Range To", "type": "ip", "default": "", "required": False, "placeholder": "192.168.21.254"},
                {"name": "cidr", "label": "Subnet Prefix (CIDR bits)", "type": "number", "default": 23, "required": True,
                 "placeholder": "23 for a /23 (192.168.20.0-192.168.21.255) network"},
                {"name": "port", "label": "Port", "type": "number", "default": 47808, "required": True},
                {"name": "timeout_s", "label": "Per-host Timeout (s)", "type": "number", "default": 2, "required": True},
                {"name": "concurrency", "label": "Concurrency", "type": "number", "default": 50, "required": False},
                {"name": "deep_scan", "label": "Deep Scan (read all objects)", "type": "bool", "default": True, "required": False},
                {"name": "max_objects", "label": "Max Objects / Device", "type": "number", "default": _DEFAULT_MAX_OBJECTS, "required": False},
            ],
            "notes": ("Sweeps every host in the subnet and reads the wildcard device instance "
                      "(4194303) to enumerate controllers — these devices answer ReadProperty but "
                      "not Who-Is. Set CIDR to the real mask (e.g. 23 for a /23) so the whole "
                      "subnet is covered. Deep scan reads each device's objectList and point values."),
        }

    # -- entry point ------------------------------------------------------- #
    async def discover(self, config: dict) -> list[DiscoveredDevice]:
        scan_mode = str(config.get("scan_mode") or "subnet_sweep")
        cidr = int(config.get("cidr") or 23)
        port = int(config.get("port") or 47808)
        timeout_s = float(config.get("timeout_s") or 2)
        deep_scan = bool(config.get("deep_scan", True))
        max_objects = int(config.get("max_objects") or _DEFAULT_MAX_OBJECTS)
        concurrency = max(1, int(config.get("concurrency") or 50))

        targets = self._targets(scan_mode, config, cidr)
        if not targets:
            raise DiscoveryError("No scan targets (check scan_mode / ip_range / cidr)")

        client, own = await self._client(port, cidr)
        if client is None:
            raise DiscoveryError(
                f"No BACnet client available on port {port} (busy?). "
                "Many controllers reply only to source port 47808.")
        try:
            logger.info("BACnet sweep: %d host(s), wildcard device read, port %d", len(targets), port)
            responders = await self._sweep(client, targets, port, timeout_s, concurrency)
            logger.info("BACnet sweep: %d controller(s) responded", len(responders))
            out: list[DiscoveredDevice] = []
            for ip, inst in responders.items():
                try:
                    out.append(await self._build(client, ip, port, inst, deep_scan, max_objects))
                except Exception:  # noqa: BLE001 — one bad device never kills the scan
                    logger.debug("build failed for %s (%s)", ip, inst, exc_info=True)
            out.sort(key=lambda d: d.device_id if d.device_id is not None else 1 << 30)
            return out
        finally:
            if own and client is not None:
                try:
                    disc = getattr(client, "_disconnect", None) or getattr(client, "disconnect", None)
                    if disc:
                        res = disc()
                        if asyncio.iscoroutine(res):
                            await res
                except Exception:  # noqa: BLE001
                    logger.debug("BACnet discovery disconnect error", exc_info=True)

    # -- scan scope -------------------------------------------------------- #
    def _bind_ip(self) -> str:
        ip = self._local_ip
        if not ip or ip == "0.0.0.0":
            return _detect_host_ip()
        return ip

    def _targets(self, scan_mode: str, config: dict, cidr: int) -> list[str]:
        if scan_mode == "ip_range":
            frm = str(config.get("ip_range_from") or "").strip()
            to = str(config.get("ip_range_to") or "").strip()
            if frm and to:
                return _expand_range(frm, to)
            # fall through to subnet sweep if the range is incomplete
        ip = self._bind_ip()
        try:
            net = ipaddress.ip_network(f"{ip}/{cidr}", strict=False)
        except ValueError:
            return []
        me = self._bind_ip()
        return [str(h) for h in net.hosts() if str(h) != me]

    # -- client ------------------------------------------------------------ #
    async def _client(self, port: int, cidr: int):
        """Reuse the poller's live client if present, else open our own stack.

        Returns ``(client, own)`` where ``own`` is True when we created the
        stack (and must disconnect it afterwards).
        """
        borrowed = self._poller.borrow_client() if self._poller is not None else None
        if borrowed is not None:
            return borrowed, False
        try:
            import BAC0
        except ImportError as e:  # pragma: no cover
            raise DiscoveryError("BAC0 not installed; BACnet discovery unavailable") from e
        ip = self._bind_ip()
        try:
            client = BAC0.lite(ip=f"{ip}/{cidr}:{port}")
        except Exception as e:
            raise DiscoveryError(f"BACnet stack init failed on {ip}/{cidr}:{port}: {e}") from e
        await asyncio.sleep(0.5)
        return client, True

    # -- sweep ------------------------------------------------------------- #
    async def _sweep(self, client, ips: list[str], port: int,
                     timeout_s: float, concurrency: int) -> dict[str, int]:
        """Read the wildcard device instance on every host; collect {ip: real_id}."""
        found: dict[str, int] = {}
        sem = asyncio.Semaphore(concurrency)

        async def probe(ip: str) -> None:
            addr = ip if port == 47808 else f"{ip}:{port}"
            async with sem:
                try:
                    oid = await asyncio.wait_for(
                        client.read(f"{addr} device {_WILDCARD_INSTANCE} objectIdentifier"),
                        timeout=timeout_s)
                except Exception:  # noqa: BLE001 — dead host / non-BACnet / timeout
                    return
            inst = None
            if isinstance(oid, (tuple, list)) and len(oid) == 2:
                inst = oid[1]
            if isinstance(inst, int):
                found[ip] = inst

        await asyncio.gather(*(probe(ip) for ip in ips))
        return found

    # -- per-device build -------------------------------------------------- #
    async def _build(self, client, ip: str, port: int, inst: int,
                     deep_scan: bool, max_objects: int) -> DiscoveredDevice:
        addr = ip if port == 47808 else f"{ip}:{port}"
        name = f"BACnet {inst}"
        vendor = model = ""
        for prop in ("objectName", "vendorName", "modelName"):
            try:
                v = await asyncio.wait_for(client.read(f"{addr} device {inst} {prop}"), timeout=4)
            except Exception:  # noqa: BLE001 — identity reads are best-effort
                continue
            if not v:
                continue
            if prop == "objectName":
                name = str(v)
            elif prop == "vendorName":
                vendor = str(v)
            else:
                model = str(v)

        objects: list[DiscoveredPoint] = []
        if deep_scan:
            objects = await self._read_objects(client, addr, inst, max_objects)

        vend = " ".join(x for x in (vendor, model) if x)
        return DiscoveredDevice(
            ref=f"{ip}-{inst}", protocol="bacnet",
            name=name, address=ip, device_id=inst,
            vendor=vend, object_count=len(objects), objects=objects,
        )

    async def _read_objects(self, client, addr: str, device_id: int,
                            max_objects: int) -> list[DiscoveredPoint]:
        """Read objectList then each point's name/value/units via single reads.

        Single ReadProperty throughout — these controllers reject RPM. Every
        read is guarded so a slow/absent point degrades to a partial point
        rather than aborting the device.
        """
        try:
            obj_list = await asyncio.wait_for(
                client.read(f"{addr} device {device_id} objectList"), timeout=10)
        except Exception:  # noqa: BLE001
            logger.debug("objectList read failed for %s", addr, exc_info=True)
            return []
        if not isinstance(obj_list, (list, tuple)):
            return []

        points: list[DiscoveredPoint] = []
        for entry in obj_list[:max_objects]:
            try:
                otype, oinst = entry  # ObjectIdentifier -> (type, instance)
            except (TypeError, ValueError):
                continue
            camel = _camel_type(otype)
            if camel is None or not isinstance(oinst, int):
                continue  # skip device/file/program/etc.
            name = f"{camel}/{oinst}"
            present_value = None
            units = ""
            try:
                nm = await asyncio.wait_for(client.read(f"{addr} {camel} {oinst} objectName"), timeout=4)
                if nm:
                    name = str(nm)
            except Exception:  # noqa: BLE001
                logger.debug("objectName read failed %s %s:%s", addr, camel, oinst, exc_info=True)
            try:
                present_value = await asyncio.wait_for(
                    client.read(f"{addr} {camel} {oinst} presentValue"), timeout=4)
            except Exception:  # noqa: BLE001
                logger.debug("presentValue read failed %s %s:%s", addr, camel, oinst, exc_info=True)
            if camel in _ANALOG_CAMEL:
                try:
                    u = await asyncio.wait_for(client.read(f"{addr} {camel} {oinst} units"), timeout=4)
                    units = str(u) if u is not None else ""
                except Exception:  # noqa: BLE001
                    pass
            points.append(DiscoveredPoint(
                object_name=name,
                object_type=camel,
                object_instance=int(oinst),
                units=units,
                present_value=present_value,
                address=f"{camel}:{oinst}",
            ))
        return points
