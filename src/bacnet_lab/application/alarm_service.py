"""Alarm service: persists Raised/Cleared alarm events to the repository.

This ensures that alarms raised by any service (Simulation, Scenarios, 
Anomaly Detector) are stored in SQLite and visible via the API/UI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bacnet_lab.domain.enums import EventType
from bacnet_lab.domain.events import AlarmCleared, AlarmRaised, DomainEvent
from bacnet_lab.domain.models.event import Alarm
from bacnet_lab.ports.event_publisher import EventPublisherPort
from bacnet_lab.ports.repositories import AlarmRepositoryPort

logger = logging.getLogger(__name__)


class AlarmService:
    """Service that manages the persistent state of alarms."""

    def __init__(
        self,
        event_publisher: EventPublisherPort,
        alarm_repo: AlarmRepositoryPort,
    ) -> None:
        self._event_publisher = event_publisher
        self._alarm_repo = alarm_repo
        self._event_publisher.subscribe(self._handle_event)

    async def _handle_event(self, event: DomainEvent) -> None:
        if event.event_type == EventType.ALARM_RAISED and isinstance(event, AlarmRaised):
            await self._handle_alarm_raised(event)
        elif event.event_type == EventType.ALARM_CLEARED and isinstance(event, AlarmCleared):
            await self._handle_alarm_cleared(event)

    async def _handle_alarm_raised(self, event: AlarmRaised) -> None:
        alarm = Alarm(
            id=event.alarm_id,
            device_id=event.device_id,
            point_name=event.point_name,
            severity=event.severity,
            message=event.message,
            raised_at=event.timestamp or datetime.now(timezone.utc),
        )
        await self._alarm_repo.save(alarm)
        logger.info("Alarm saved to repository: %s", event.alarm_id)

    async def _handle_alarm_cleared(self, event: AlarmCleared) -> None:
        await self._alarm_repo.clear(event.alarm_id)
        logger.info("Alarm cleared in repository: %s", event.alarm_id)

    async def get_active_alarms(self) -> list[Alarm]:
        return await self._alarm_repo.get_active()

    async def list_recent_alarms(self, limit: int = 50) -> list[Alarm]:
        return await self._alarm_repo.list_recent(limit)
