"""Application service driving live, per-protocol device discovery.

Holds one ``ProtocolDiscoveryPort`` per protocol and runs scans as background
async jobs so slow network probes (Who-Is, unit sweeps, broker waits) never
block the request. Selected results are turned into real ``Device`` records,
brought online on the network engine, and registered with the historian.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Device, Point
from bacnet_lab.domain.models.discovery import DiscoveredDevice
from bacnet_lab.ports.discovery import DiscoveryError, ProtocolDiscoveryPort

logger = logging.getLogger(__name__)


@dataclass
class ScanJob:
    scan_id: str
    protocol: str
    status: str = "running"           # running | done | error
    error: str | None = None
    devices: list[DiscoveredDevice] = field(default_factory=list)

    def to_status(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "protocol": self.protocol,
            "status": self.status,
            "error": self.error,
            "count": len(self.devices),
            "devices": [d.summary() for d in self.devices],
        }


def _point_type(raw: str) -> PointType:
    """Best-effort map a discovered object type string to a PointType."""
    try:
        return PointType(raw)
    except (ValueError, TypeError):
        # KNX/Modbus use dpt/register kinds — fall back to analog vs binary.
        low = str(raw).lower()
        if low.startswith("1") or "binary" in low or "bool" in low:
            return PointType.BINARY_VALUE
        return PointType.ANALOG_VALUE


class DiscoveryService:
    def __init__(self, device_service, tsdb=None) -> None:
        self._adapters: dict[str, ProtocolDiscoveryPort] = {}
        self._jobs: dict[str, ScanJob] = {}
        self._ds = device_service
        self._tsdb = tsdb

    # -- registry ---------------------------------------------------------- #
    def register(self, adapter: ProtocolDiscoveryPort) -> None:
        self._adapters[adapter.protocol] = adapter
        logger.info("Discovery adapter registered: %s", adapter.protocol)

    def protocols(self) -> list[dict]:
        """Protocol cards: id, label, live device count, availability."""
        counts: dict[str, int] = {}
        for d in self._ds.get_all_in_memory_devices():
            counts[d.protocol] = counts.get(d.protocol, 0) + 1
        return [
            {
                "protocol": a.protocol,
                "label": a.label or a.protocol.upper(),
                "device_count": counts.get(a.protocol, 0),
                "available": True,
            }
            for a in self._adapters.values()
        ]

    def config_schema(self, protocol: str) -> dict:
        adapter = self._require(protocol)
        return adapter.config_schema()

    # -- scans ------------------------------------------------------------- #
    def start_scan(self, protocol: str, config: dict) -> str:
        adapter = self._require(protocol)
        scan_id = uuid.uuid4().hex[:12]
        job = ScanJob(scan_id=scan_id, protocol=protocol)
        self._jobs[scan_id] = job
        asyncio.create_task(self._run_scan(adapter, job, config or {}))
        return scan_id

    async def _run_scan(self, adapter, job: ScanJob, config: dict) -> None:
        try:
            job.devices = await adapter.discover(config)
            job.status = "done"
            logger.info("Scan %s (%s) found %d devices",
                        job.scan_id, job.protocol, len(job.devices))
        except DiscoveryError as e:
            job.status, job.error = "error", str(e)
            logger.warning("Scan %s (%s) failed: %s", job.scan_id, job.protocol, e)
        except Exception as e:  # noqa: BLE001
            job.status, job.error = "error", f"unexpected: {e}"
            logger.error("Scan %s (%s) crashed", job.scan_id, job.protocol, exc_info=True)

    def get_scan(self, scan_id: str) -> ScanJob | None:
        return self._jobs.get(scan_id)

    def device_objects(self, scan_id: str, ref: str) -> list[dict] | None:
        job = self._jobs.get(scan_id)
        if not job:
            return None
        for d in job.devices:
            if d.ref == ref:
                return [vars(o) for o in d.objects]
        return None

    # -- add selected ------------------------------------------------------ #
    async def add_devices(self, scan_id: str, refs: list[str]) -> list[dict]:
        """Persist selected discovered devices, bring them online, register them
        with the historian. Returns one result row per requested ref."""
        job = self._jobs.get(scan_id)
        if not job:
            raise DiscoveryError(f"Unknown scan_id: {scan_id}")
        by_ref = {d.ref: d for d in job.devices}
        results: list[dict] = []
        for ref in refs:
            disc = by_ref.get(ref)
            if not disc:
                results.append({"ref": ref, "added": False, "error": "ref not found"})
                continue
            device = await self._materialize(disc)
            results.append({
                "ref": ref, "added": True,
                "device_id": device.device_id, "name": device.name,
                "points": len(device.points),
            })
        return results

    async def _materialize(self, disc: DiscoveredDevice) -> Device:
        device_id = disc.device_id
        if device_id is None or self._ds.get_in_memory_device(device_id):
            device_id = await self._ds.next_device_id()
        points = [
            Point(
                object_type=_point_type(o.object_type),
                object_instance=o.object_instance or (i + 1),
                object_name=o.object_name or f"{disc.protocol}/{i + 1}",
                present_value=o.present_value if o.present_value is not None else 0,
                units=o.units,
                group_address=o.group_address,
                dpt=o.dpt,
            )
            for i, o in enumerate(disc.objects)
        ]
        device = Device(
            device_id=device_id,
            name=disc.name or f"{disc.protocol}-{device_id}",
            description=f"Discovered via {disc.protocol} ({disc.vendor or disc.address})".strip(),
            protocol=disc.protocol,
            points=points,
        )
        await self._ds.save_device(device)
        await self._ds.activate_device(device)
        if self._tsdb is not None and getattr(self._tsdb, "ready", False):
            try:
                await self._tsdb.register_devices([device])
            except Exception:  # noqa: BLE001 — historian is best-effort
                logger.debug("tsdb register failed for %s", device.device_id, exc_info=True)
        return device

    # -- internal ---------------------------------------------------------- #
    def _require(self, protocol: str) -> ProtocolDiscoveryPort:
        adapter = self._adapters.get(protocol)
        if adapter is None:
            raise DiscoveryError(f"Unsupported protocol: {protocol}")
        return adapter
