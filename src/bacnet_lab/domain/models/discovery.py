"""Domain models for live protocol discovery.

A discovery scan probes a network (BACnet Who-Is, Modbus unit scan, MQTT topic
sweep, KNX gateway/ETS) and returns candidate devices the operator can select
and attach to an asset. These are transient results — they become real
``Device`` records only when added.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DiscoveredPoint:
    """One readable/writable object found on a discovered device."""

    object_name: str
    object_type: str = ""          # BACnet object type / KNX dpt class / register kind
    object_instance: int = 0
    units: str = ""
    present_value: float | int | bool | str | None = None
    group_address: str = ""        # KNX only
    dpt: str = ""                  # KNX only
    address: str = ""              # Modbus register / BACnet object id, free-form


@dataclass
class DiscoveredDevice:
    """A candidate device surfaced by a scan (not yet persisted).

    ``ref`` is a scan-stable handle the client returns when selecting devices to
    add; it is unique within a single scan result set.
    """

    ref: str
    protocol: str
    name: str
    address: str = ""              # IP / gateway / unit id / broker topic root
    device_id: int | None = None   # protocol device id when known
    vendor: str = ""
    model: str = ""
    object_count: int = 0
    objects: list[DiscoveredPoint] = field(default_factory=list)
    raw: dict = field(default_factory=dict)  # protocol-specific extra metadata

    def summary(self) -> dict:
        """List-view shape (no per-object detail)."""
        return {
            "ref": self.ref,
            "protocol": self.protocol,
            "name": self.name,
            "address": self.address,
            "device_id": self.device_id,
            "vendor": self.vendor,
            "model": self.model,
            "object_count": self.object_count,
        }
