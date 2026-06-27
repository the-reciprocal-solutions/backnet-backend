"""Pipeline orchestrator: Anomaly -> Reasoning -> enriched fan-out.

Flow (all over the in-process event bus, async, single process):

    PointValueChanged
        -> AnomalyDetector  (existing)  -> AlarmRaised   [the anomaly]
        -> PipelineService  (this)      -> AnomalyEnriched [+ reasoning]
            -> WebSocket broadcaster  (subscriber, intern B1)
            -> Webhook delivery       (subscriber, intern B3)

PipelineService listens for anomaly alarms, asks the reasoning layer
(CopilotService) to explain the point, and republishes an ``AnomalyEnriched``
event carrying the frozen wire contract (see ``AnomalyEnriched.to_message``).

The LLM/reasoning call is slow, so it must NOT run inside the bus publish
loop (that would stall the detector). Alarms are pushed onto an internal
queue and drained by a background worker. Each enrichment is independent —
this is the "each model runs independently" requirement realized as async
tasks rather than processes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from bacnet_lab.application.device_service import DeviceService
from bacnet_lab.domain.enums import AlarmSeverity, EventType
from bacnet_lab.domain.events import (
    AlarmRaised,
    AnomalyEnriched,
    DomainEvent,
    WorkOrderAssigned,
)
from bacnet_lab.ports.event_publisher import EventPublisherPort

logger = logging.getLogger(__name__)

# AlarmSeverity (4 levels) -> contract severity (3 levels).
_SEVERITY_MAP = {
    AlarmSeverity.LOW: "low",
    AlarmSeverity.MEDIUM: "medium",
    AlarmSeverity.HIGH: "high",
    AlarmSeverity.CRITICAL: "high",
}
# Coarse score so the UI has a magnitude to render. Real per-event scores
# (band-widths over) are a follow-up once the detector exposes them.
_SEVERITY_SCORE = {"low": 0.4, "medium": 0.6, "high": 0.85}

# Predictor failure-level -> base failure probability. Nudged by confidence.
_LEVEL_PROB = {"critical": 0.9, "high": 0.75, "elevated": 0.55, "watch": 0.3}
_CONF_NUDGE = {"high": 0.05, "medium": 0.0, "low": -0.1}
# Predictor levels that justify auto-assigning a work order.
_ACTIONABLE_LEVELS = {"critical", "high", "elevated"}

_MAX_QUEUE = 256


def _action_for(kind: str, component: str | None) -> str:
    where = component or "asset"
    if kind == "vibration_spike":
        return f"Inspect {where} bearing/mounts for imbalance or wear"
    if kind == "temp_excursion":
        return f"Check {where} cooling/airflow and setpoints"
    if kind == "pressure_excursion":
        return f"Check {where} filters/dampers for restriction"
    return f"Inspect {where} — value trending out of operating band"


def _kind_for(point_name: str) -> str:
    n = (point_name or "").lower()
    if "vibrat" in n:
        return "vibration_spike"
    if "temp" in n:
        return "temp_excursion"
    if "press" in n:
        return "pressure_excursion"
    return "forecast_band_breach"


class PipelineService:
    def __init__(
        self,
        event_publisher: EventPublisherPort,
        device_service: DeviceService,
        copilot_service,
        prediction_service=None,
        *,
        reasoning_enabled: bool = True,
        auto_work_orders: bool = True,
    ) -> None:
        self._events = event_publisher
        self._ds = device_service
        self._copilot = copilot_service
        self._prediction = prediction_service
        self._reasoning_enabled = reasoning_enabled
        self._auto_work_orders = auto_work_orders
        self._work_orders_total = 0
        self._queue: asyncio.Queue[AlarmRaised] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._task: asyncio.Task | None = None
        self._running = False
        self._enriched_total = 0
        # Subscribe immediately so no anomaly is missed between create and start.
        self._events.subscribe(self._on_event)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._worker())
        logger.info("Pipeline service started (Anomaly -> Reasoning -> AnomalyEnriched)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Pipeline service stopped")

    def status(self) -> dict:
        return {
            "running": self._running,
            "queued": self._queue.qsize(),
            "enriched_total": self._enriched_total,
            "work_orders_total": self._work_orders_total,
            "reasoning_enabled": self._reasoning_enabled,
            "auto_work_orders": self._auto_work_orders,
        }

    # ------------------------------------------------------------------ #
    # Bus handler — fast, non-blocking. Only enqueues.
    # ------------------------------------------------------------------ #
    async def _on_event(self, event: DomainEvent) -> None:
        if not isinstance(event, AlarmRaised):
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Pipeline queue full; dropping anomaly on %s", event.point_name)

    # ------------------------------------------------------------------ #
    # Worker — slow enrichment runs here, off the bus publish loop.
    # ------------------------------------------------------------------ #
    async def _worker(self) -> None:
        try:
            while self._running:
                alarm = await self._queue.get()
                try:
                    await self._process(alarm)
                except Exception as e:  # one bad enrichment must not kill the worker
                    logger.error("Pipeline enrichment failed for %s: %s",
                                 alarm.point_name, e, exc_info=True)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise

    async def _process(self, alarm: AlarmRaised) -> None:
        """Enrich one anomaly, publish AnomalyEnriched, and — when the predictor
        projects a future failure — auto-assign a WorkOrderAssigned event."""
        point_name = alarm.point_name
        severity = _SEVERITY_MAP.get(alarm.severity, "medium")
        kind = _kind_for(point_name)
        value, unit = self._live_value(alarm.device_id, point_name)
        # Equipment that owns the point (device prefix, e.g. "AHU-1").
        component = point_name.split("/")[0] if "/" in point_name else None

        # --- Predict the future failure (WHEN + how likely). ---
        prediction = None
        if self._prediction is not None:
            try:
                prediction = await self._prediction.predict_point(alarm.device_id, point_name)
            except Exception as e:
                logger.warning("Prediction failed for %s: %s", point_name, e)

        eta_hours = failure_prob = None
        if prediction:
            eta_min = prediction.get("eta_minutes")
            eta_hours = round(eta_min / 60.0, 2) if eta_min is not None else None
            base = _LEVEL_PROB.get(prediction.get("level", ""), None)
            if base is not None:
                failure_prob = round(
                    min(1.0, max(0.0, base + _CONF_NUDGE.get(prediction.get("confidence"), 0.0))),
                    2,
                )

        # --- Narrate (WHY) via the grounded reasoning layer. ---
        explanation = None
        root_cause = None
        contributing_factors = None
        recommended_action = None
        confidence = None

        if self._reasoning_enabled and self._copilot is not None:
            try:
                result = await self._copilot.explain(point_name)
                explanation = result.answer
                root_cause = getattr(result, "root_cause", None)
                contributing_factors = getattr(result, "contributing_factors", None)
                recommended_action = getattr(result, "recommended_action", None)
                confidence = getattr(result, "confidence", None)
            except Exception as e:
                logger.warning("Reasoning failed for %s: %s", point_name, e)
        if explanation is None and prediction:
            explanation = prediction.get("reason")

        await self._events.publish(AnomalyEnriched(
            device_id=alarm.device_id,
            point=point_name,
            value=value,
            unit=unit,
            severity=severity,
            anomaly_score=_SEVERITY_SCORE.get(severity),
            anomaly_kind=kind,
            component=component,
            failure_prob=failure_prob,
            eta_hours=eta_hours,
            explanation=explanation,
            root_cause=root_cause,
            contributing_factors=contributing_factors,
            recommended_action=recommended_action,
            confidence=confidence,
        ))
        self._enriched_total += 1

        # --- Auto-assign a work order on a projected (finite-ETA) failure. ---
        if (
            self._auto_work_orders
            and prediction
            and prediction.get("level") in _ACTIONABLE_LEVELS
            and prediction.get("eta_minutes") is not None
        ):
            await self._events.publish(WorkOrderAssigned(
                work_order_id=str(uuid.uuid4()),
                device_id=alarm.device_id,
                point=point_name,
                component=component,
                action=_action_for(kind, component),
                severity=severity,
                eta_hours=eta_hours,
                failure_prob=failure_prob,
                reason=prediction.get("reason", ""),
            ))
            self._work_orders_total += 1
            logger.info("Work order auto-assigned for %s (ETA ~%sh)", point_name, eta_hours)

    def _live_value(self, device_id: int, point_name: str) -> tuple[float | None, str]:
        device = self._ds.get_in_memory_device(device_id)
        if not device:
            return None, ""
        for p in getattr(device, "points", []):
            if p.object_name == point_name:
                v = p.present_value
                v = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
                return v, getattr(p, "units", "") or ""
        return None, ""
