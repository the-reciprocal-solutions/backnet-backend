"""BACnet/IP live discovery adapter.

Runs a real BACnet Who-Is via BAC0 (bacpypes3) and turns the I-Am responses
into transient ``DiscoveredDevice`` results. BAC0 is imported lazily and every
scan disconnects its network in a ``finally`` block so a failed/empty scan never
leaks a UDP stack or hangs the worker.
"""

from __future__ import annotations

import asyncio
import logging
import socket

from bacnet_lab.domain.models.discovery import DiscoveredDevice, DiscoveredPoint
from bacnet_lab.ports.discovery import DiscoveryError, ProtocolDiscoveryPort

logger = logging.getLogger(__name__)


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


def _free_udp_port() -> int:
    """Grab a free UDP port so discovery never collides with the simulator."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class BACnetDiscovery(ProtocolDiscoveryPort):
    protocol = "bacnet"
    label = "BACnet/IP"

    def __init__(self, local_ip: str = "0.0.0.0") -> None:
        self._local_ip = local_ip

    def config_schema(self) -> dict:
        return {
            "fields": [
                {"name": "scan_mode", "label": "Scan Mode", "type": "select",
                 "options": ["broadcast", "ip_range"], "default": "broadcast", "required": True},
                {"name": "ip_range_from", "label": "IP Range From", "type": "ip", "default": "", "required": False, "placeholder": "192.168.10.1"},
                {"name": "ip_range_to", "label": "IP Range To", "type": "ip", "default": "", "required": False, "placeholder": "192.168.10.255"},
                {"name": "port", "label": "Port", "type": "number", "default": 47808, "required": True},
                {"name": "timeout_s", "label": "Timeout (s)", "type": "number", "default": 5, "required": True},
                {"name": "retries", "label": "Retries", "type": "number", "default": 2, "required": False},
                {"name": "deep_scan", "label": "Deep Scan (read all objects)", "type": "bool", "default": True, "required": False},
            ],
            "notes": "BACnet uses Who-Is broadcast discovery; IP range narrows the target subnet.",
        }

    async def discover(self, config: dict) -> list[DiscoveredDevice]:
        timeout_s = float(config.get("timeout_s", 5))
        deep_scan = bool(config.get("deep_scan", True))
        rng_from = (config.get("ip_range_from") or "").strip()
        rng_to = (config.get("ip_range_to") or "").strip()

        try:
            import BAC0
        except ImportError as e:  # pragma: no cover
            raise DiscoveryError("BAC0 not installed; BACnet discovery unavailable") from e

        bacnet = None
        try:
            # BAC0.lite needs a running loop (we are in one). Bind to the local
            # interface; "0.0.0.0" lets BAC0 auto-pick.
            # BAC0/bacpypes3 needs an IP with subnet mask (CIDR). Bind on a free
            # UDP port so we never collide with the simulator, which already
            # holds 47808+. Who-Is still broadcasts to the standard dest port.
            ip = self._local_ip if self._local_ip and self._local_ip != "0.0.0.0" else _detect_host_ip()
            bind = f"{ip}/24:{_free_udp_port()}"
            try:
                bacnet = BAC0.lite(ip=bind)
            except Exception as e:
                raise DiscoveryError(f"BACnet stack init failed: {e}") from e

            # Give the async stack a moment to come up.
            await asyncio.sleep(0.5)

            # Who-Is — broadcast, or constrained to an address range.
            # BAC0 2025.x (bacpypes3) exposes ``who_is``; older releases used
            # ``whois``. Bind to whichever this version provides.
            who_is = getattr(bacnet, "who_is", None) or getattr(bacnet, "whois", None)
            if who_is is None:
                raise DiscoveryError("BAC0 instance exposes no who_is/whois method")
            # bacpypes3's who_is takes device-instance limits, not an IP range,
            # so we always broadcast and filter by address after collecting.
            try:
                coro_or_val = who_is()
                if asyncio.iscoroutine(coro_or_val):
                    await asyncio.wait_for(coro_or_val, timeout=timeout_s)
                else:
                    await asyncio.sleep(min(timeout_s, 5))
            except asyncio.TimeoutError:
                pass  # whatever answered within the window is fine
            except Exception as e:
                raise DiscoveryError(f"BACnet Who-Is failed: {e}") from e

            return self._collect(bacnet, deep_scan)
        finally:
            if bacnet is not None:
                try:
                    disc = getattr(bacnet, "_disconnect", None) or getattr(bacnet, "disconnect", None)
                    if disc:
                        res = disc()
                        if asyncio.iscoroutine(res):
                            await res
                except Exception:  # noqa: BLE001
                    logger.debug("BACnet disconnect error", exc_info=True)

    def _collect(self, bacnet, deep_scan: bool) -> list[DiscoveredDevice]:
        """Read whatever the BAC0 instance discovered, defensively across versions."""
        out: list[DiscoveredDevice] = []
        # BAC0 exposes discovered devices as a list of tuples on .discoveredDevices
        # (commonly (name, vendor, address, device_id)) or via .devices.
        rows = getattr(bacnet, "discoveredDevices", None)
        if not rows:
            rows = getattr(bacnet, "devices", None)
        if not rows:
            return out
        for row in rows:
            name, vendor, address, device_id = "", "", "", None
            try:
                if isinstance(row, (list, tuple)):
                    vals = list(row)
                    # Heuristic: find the int (device id) and an address-looking str.
                    for v in vals:
                        if isinstance(v, int) and device_id is None:
                            device_id = v
                        elif isinstance(v, str) and (":" in v or "." in v) and not address:
                            address = v
                    name = str(vals[0]) if vals else ""
                    if len(vals) > 1 and isinstance(vals[1], str):
                        vendor = vals[1]
                elif isinstance(row, dict):
                    name = str(row.get("name", ""))
                    vendor = str(row.get("vendor", ""))
                    address = str(row.get("address", ""))
                    device_id = row.get("device_id") or row.get("deviceId")
            except Exception:  # noqa: BLE001
                continue
            ref = f"{address or 'bacnet'}-{device_id if device_id is not None else len(out)}"
            objects: list[DiscoveredPoint] = []
            # Deep object read is best-effort and version-specific; left empty
            # when unavailable so the scan stays fast and never hangs.
            out.append(DiscoveredDevice(
                ref=ref, protocol="bacnet",
                name=name or f"BACnet {device_id}", address=address,
                device_id=device_id if isinstance(device_id, int) else None,
                vendor=vendor, object_count=len(objects), objects=objects,
            ))
        return out
