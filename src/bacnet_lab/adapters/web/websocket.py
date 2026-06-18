"""WebSocket fan-out: push live enriched anomalies + work orders to clients.

``ConnectionManager`` holds the connected sockets and broadcasts JSON to all of
them (dead sockets are dropped, never blocking the loop). ``WsBroadcaster``
subscribes to the event bus and forwards ``AnomalyEnriched`` and
``WorkOrderAssigned`` events to the manager using their frozen ``to_message()``
wire contract — the same shape the REST ``/api/anomaly-feed`` serves and the
frontend ws.js client expects (message.type = "anomaly" | "work_order").
"""

from __future__ import annotations

import logging

from bacnet_lab.domain.events import AnomalyEnriched, DomainEvent, WorkOrderAssigned

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set = set()

    async def connect(self, ws) -> None:
        await ws.accept()
        self._clients.add(ws)
        logger.info("WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws) -> None:
        self._clients.discard(ws)
        logger.info("WS client disconnected (%d total)", len(self._clients))

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in self._clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)


class WsBroadcaster:
    """Bus subscriber that forwards enriched anomalies + work orders to WS."""

    def __init__(self, event_publisher, manager: ConnectionManager) -> None:
        self._manager = manager
        event_publisher.subscribe(self._on_event)

    async def _on_event(self, event: DomainEvent) -> None:
        if isinstance(event, (AnomalyEnriched, WorkOrderAssigned)):
            await self._manager.broadcast(event.to_message())
