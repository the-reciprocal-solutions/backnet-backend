"""Anomaly detector: monitors point changes against forecasts.

Previously this raised a MEDIUM alarm on EVERY single-tick breach of the
P10-P90 forecast band — producing hundreds of noise alarms on sub-degree
wiggles. That is threshold alerting, not condition monitoring.

This version de-noises, so only meaningful excursions surface:

* **Magnitude gate** — the breach must exceed a fraction of the band width
  (``min_band_ratio``). Tiny in-the-margin wiggles are ignored.
* **Severity scaling** — how far past the band (in band-widths) sets
  LOW/MEDIUM/HIGH/CRITICAL instead of a flat MEDIUM.
* **De-duplication** — one alarm per ongoing excursion per point; it is not
  re-raised every tick while the point stays out of band.
* **Auto-clear** — when the value returns inside the band, the open alarm is
  cleared (ALARM_CLEARED), so the active-alarm list reflects reality.

All behaviour is tunable via constructor args (defaults keep the unit-test
contract: a 1.0-band breach raises one MEDIUM alarm).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from bacnet_lab.domain.enums import AlarmSeverity, EventType
from bacnet_lab.domain.events import AlarmCleared, AlarmRaised, DomainEvent, PointValueChanged
from bacnet_lab.forecasting.db import ForecastDB
from bacnet_lab.ports.event_publisher import EventPublisherPort

logger = logging.getLogger(__name__)


class AnomalyDetector:
    """Subscriber that checks every point change against its latest forecast."""

    def __init__(
        self,
        event_publisher: EventPublisherPort,
        db: ForecastDB,
        *,
        min_band_ratio: float = 0.5,
        max_forecast_age_s: float = 900.0,
        auto_clear: bool = True,
    ) -> None:
        self._event_publisher = event_publisher
        self._db = db
        # Breach must exceed this fraction of the band width to count.
        self._min_band_ratio = min_band_ratio
        self._max_forecast_age_s = max_forecast_age_s
        self._auto_clear = auto_clear
        # point_name -> {"alarm_id", "device_id", "severity"} for open excursions
        self._active: dict[str, dict] = {}
        self._event_publisher.subscribe(self._handle_event)

    async def _handle_event(self, event: DomainEvent) -> None:
        if event.event_type != EventType.POINT_VALUE_CHANGED:
            return
        if not isinstance(event, PointValueChanged):
            return
        await self.check_anomaly(event)

    async def check_anomaly(self, event: PointValueChanged) -> None:
        """Compare new_value against the latest P10/P90 forecast for the point."""
        object_name = event.point_name
        if not object_name:
            return

        value = event.new_value
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return

        forecasts = await self._db.latest_forecast(object_name, limit=288)
        if not forecasts:
            return

        ts = event.timestamp or datetime.now(timezone.utc)
        closest = None
        min_diff = None
        for f in forecasts:
            diff = abs((f["horizon_ts"] - ts).total_seconds())
            if min_diff is None or diff < min_diff:
                min_diff = diff
                closest = f

        # No usable prediction near this timestamp.
        if min_diff is None or min_diff > self._max_forecast_age_s:
            return

        p10 = closest["p10"]
        p90 = closest["p90"]
        val_f = float(value)

        # Band width drives both the magnitude gate and severity scaling.
        band = None
        if p10 is not None and p90 is not None and p90 > p10:
            band = p90 - p10

        # How far outside the band, in band-widths (0 if inside).
        over = 0.0
        direction = None
        threshold = None
        if p90 is not None and val_f > p90:
            direction, threshold = "above", p90
            over = (val_f - p90) / band if band else self._min_band_ratio
        elif p10 is not None and val_f < p10:
            direction, threshold = "below", p10
            over = (p10 - val_f) / band if band else self._min_band_ratio

        if direction is None:
            # Inside band → clear any open excursion for this point.
            await self._maybe_clear(event)
            return

        # Magnitude gate: ignore wiggles within the margin (the noise).
        if over < self._min_band_ratio:
            return

        # Already alarmed and still out of band → don't spam; keep the open one.
        if object_name in self._active:
            return

        severity = self._severity_for(over)
        await self._raise_anomaly_alarm(event, val_f, threshold, direction, severity, over)

    def _severity_for(self, over: float) -> AlarmSeverity:
        """Map breach size (in band-widths) to severity."""
        if over >= 3.0:
            return AlarmSeverity.CRITICAL
        if over >= 2.0:
            return AlarmSeverity.HIGH
        return AlarmSeverity.MEDIUM

    async def _maybe_clear(self, event: PointValueChanged) -> None:
        if not self._auto_clear:
            return
        open_alarm = self._active.pop(event.point_name, None)
        if not open_alarm:
            return
        await self._event_publisher.publish(
            AlarmCleared(
                alarm_id=open_alarm["alarm_id"],
                device_id=open_alarm["device_id"],
                point_name=event.point_name,
            )
        )
        logger.info("Anomaly cleared on %s (back inside forecast band)", event.point_name)

    async def _raise_anomaly_alarm(
        self,
        event: PointValueChanged,
        value: float,
        threshold: float,
        direction: str,
        severity: AlarmSeverity,
        over: float,
    ) -> None:
        alarm_id = str(uuid.uuid4())
        message = (
            f"Anomaly detected on {event.point_name}: "
            f"Value {value:.2f} is {direction} forecast threshold {threshold:.2f}."
        )
        logger.warning("%s (%.1f band-widths, %s)", message, over, severity.value)

        self._active[event.point_name] = {
            "alarm_id": alarm_id,
            "device_id": event.device_id,
            "severity": severity,
        }

        await self._event_publisher.publish(
            AlarmRaised(
                alarm_id=alarm_id,
                device_id=event.device_id,
                point_name=event.point_name,
                severity=severity,
                message=message,
                timestamp=datetime.now(timezone.utc),
            )
        )
