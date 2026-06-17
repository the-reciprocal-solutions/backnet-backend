from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from bacnet_lab.domain.models.device import Device
from bacnet_lab.domain.value_objects import PointValue


class TimeSeriesPort(ABC):
    """Contract for time-series persistence of readings + events.

    Implementations must degrade gracefully: a backend outage must never
    block or crash the simulation loop (dual-write safety).
    """

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @property
    @abstractmethod
    def ready(self) -> bool: ...

    @abstractmethod
    async def register_devices(self, devices: list[Device]) -> None:
        """Upsert device + point metadata rows (no schema change per device)."""
        ...

    @abstractmethod
    async def write_readings(
        self, rows: list[tuple[datetime, int, str, PointValue]]
    ) -> None:
        """Append point readings. Each row: (time, device_id, object_name, value)."""
        ...

    @abstractmethod
    async def write_event(
        self,
        time: datetime,
        event_type: str,
        device_id: int | None,
        point_name: str | None,
        severity: str | None,
        payload: dict,
    ) -> None: ...
