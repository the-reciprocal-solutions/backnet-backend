from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from bacnet_lab.application.device_service import DeviceService
from bacnet_lab.domain.events import (
    AlarmCleared,
    AlarmRaised,
    DeviceStatusChanged,
    DomainEvent,
    PointValueChanged,
    TelemetrySnapshotTaken,
)
from bacnet_lab.infrastructure.config import TimescaleSettings
from bacnet_lab.ports.event_publisher import EventPublisherPort
from bacnet_lab.ports.timeseries import TimeSeriesPort

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class HistorianService:
    """Writes a REGULAR-interval grid of every point's value into the
    time-series store (ideal input for Chronos/Moirai), plus an event_log
    stream of domain events. Dual-write: never blocks the sim; degrades to
    a no-op when the backend is unavailable.
    """

    def __init__(
        self,
        tsdb: TimeSeriesPort,
        device_service: DeviceService,
        event_publisher: EventPublisherPort,
        settings: TimescaleSettings,
    ) -> None:
        self._tsdb = tsdb
        self._ds = device_service
        self._events = event_publisher
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._running = False
        self._samples_total = 0

    async def start(self) -> None:
        if not self._settings.enabled:
            logger.info("Historian disabled (BACNET_LAB_TSDB_ENABLED=false)")
            return
        await self._tsdb.connect()
        if not self._tsdb.ready:
            logger.warning("Historian: TSDB not ready, sampling disabled")
            return
        # Register current devices/points, then sample on a fixed grid.
        await self._tsdb.register_devices(self._ds.get_all_in_memory_devices())
        self._events.subscribe(self._on_event)
        self._running = True
        self._task = asyncio.create_task(self._sample_loop())
        logger.info(
            "Historian started (interval=%ss) — regular-grid sampling to TimescaleDB",
            self._settings.sample_interval_s,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._tsdb.close()
        logger.info("Historian stopped")

    async def _sample_loop(self) -> None:
        interval = max(0.5, float(self._settings.sample_interval_s))
        try:
            while self._running:
                await asyncio.sleep(interval)
                ts = _now()
                rows = []
                for device in self._ds.get_all_in_memory_devices():
                    for point in device.points:
                        rows.append((ts, device.device_id, point.object_name, point.present_value))
                if rows:
                    await self._tsdb.write_readings(rows)
                    self._samples_total += len(rows)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Historian sample loop error: %s", e, exc_info=True)
            self._running = False

    async def _on_event(self, event: DomainEvent) -> None:
        """Persist domain events to event_log. Best-effort, never raises."""
        try:
            device_id: int | None = getattr(event, "device_id", None)
            point_name: str | None = getattr(event, "point_name", None)
            severity: str | None = None
            payload: dict = {}
            if isinstance(event, PointValueChanged):
                payload = {"old_value": event.old_value, "new_value": event.new_value}
            elif isinstance(event, DeviceStatusChanged):
                payload = {"old_status": event.old_status.value, "new_status": event.new_status.value}
            elif isinstance(event, AlarmRaised):
                severity = event.severity.value
                payload = {"alarm_id": event.alarm_id, "message": event.message}
            elif isinstance(event, AlarmCleared):
                payload = {"alarm_id": event.alarm_id}
            elif isinstance(event, TelemetrySnapshotTaken):
                payload = {"point_count": len(event.points)}
            else:
                payload = {"info": str(getattr(event, "__dict__", {}))}
            await self._tsdb.write_event(
                time=event.timestamp,
                event_type=event.event_type.value,
                device_id=device_id,
                point_name=point_name,
                severity=severity,
                payload=payload,
            )
        except Exception as e:
            logger.debug("Historian event write skipped: %s", e)

    def status(self) -> dict:
        return {
            "enabled": self._settings.enabled,
            "running": self._running,
            "ready": self._tsdb.ready,
            "sample_interval_s": self._settings.sample_interval_s,
            "samples_total": self._samples_total,
        }
