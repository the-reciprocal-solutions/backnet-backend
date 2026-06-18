"""Live anomaly feed: in-memory buffer of enriched anomalies + work orders.

Subscribes to the event bus and keeps the latest ``AnomalyEnriched`` and
``WorkOrderAssigned`` per (device, point), so the frontend grid can poll a
single endpoint (``GET /api/anomaly-feed``) for "what may fail, when, why"
without depending on the WebSocket layer.

Keyed + de-duplicated: a fresh event for the same point replaces the old one
and re-surfaces it (clears any ack). Bounded so it never grows unbounded.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from bacnet_lab.domain.events import AnomalyEnriched, DomainEvent, WorkOrderAssigned

logger = logging.getLogger(__name__)


class AnomalyFeed:
    def __init__(self, event_publisher, max_items: int = 200) -> None:
        self._max = max_items
        # feed_id -> {"feed_id", "acked", **message}
        self._items: "OrderedDict[str, dict]" = OrderedDict()
        event_publisher.subscribe(self._on_event)

    async def _on_event(self, event: DomainEvent) -> None:
        if isinstance(event, AnomalyEnriched):
            kind = "anomaly"
            msg = event.to_message()
        elif isinstance(event, WorkOrderAssigned):
            kind = "work_order"
            msg = event.to_message()
        else:
            return

        feed_id = f"{kind}:{msg['device_id']}:{msg['point']}"
        item = {"feed_id": feed_id, "acked": False, **msg}
        # Re-insert at end (newest) and re-surface (acked reset via fresh item).
        self._items.pop(feed_id, None)
        self._items[feed_id] = item
        while len(self._items) > self._max:
            self._items.popitem(last=False)  # drop oldest

    def list_active(self) -> list[dict]:
        """Unacked items, newest first."""
        return [i for i in reversed(self._items.values()) if not i["acked"]]

    def list_all(self) -> list[dict]:
        return list(reversed(self._items.values()))

    def ack(self, feed_id: str) -> bool:
        item = self._items.get(feed_id)
        if not item:
            return False
        item["acked"] = True
        return True

    def clear(self) -> None:
        self._items.clear()

    def status(self) -> dict:
        active = sum(1 for i in self._items.values() if not i["acked"])
        return {"total": len(self._items), "active": active}
