from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from bacnet_lab.domain.enums import AlarmSeverity, DeviceStatus, EventType, ScenarioStatus
from bacnet_lab.domain.value_objects import PointValue


@dataclass
class DomainEvent:
    event_type: EventType
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PointValueChanged(DomainEvent):
    device_id: int = 0
    point_name: str = ""
    old_value: PointValue = 0.0
    new_value: PointValue = 0.0
    event_type: EventType = field(default=EventType.POINT_VALUE_CHANGED, init=False)


@dataclass
class DeviceStatusChanged(DomainEvent):
    device_id: int = 0
    old_status: DeviceStatus = DeviceStatus.ONLINE
    new_status: DeviceStatus = DeviceStatus.ONLINE
    event_type: EventType = field(default=EventType.DEVICE_STATUS_CHANGED, init=False)


@dataclass
class AlarmRaised(DomainEvent):
    alarm_id: str = ""
    device_id: int = 0
    point_name: str = ""
    severity: AlarmSeverity = AlarmSeverity.MEDIUM
    message: str = ""
    event_type: EventType = field(default=EventType.ALARM_RAISED, init=False)


@dataclass
class AlarmCleared(DomainEvent):
    alarm_id: str = ""
    device_id: int = 0
    point_name: str = ""
    event_type: EventType = field(default=EventType.ALARM_CLEARED, init=False)


@dataclass
class TelemetrySnapshotTaken(DomainEvent):
    device_id: int = 0
    points: dict = field(default_factory=dict)
    event_type: EventType = field(default=EventType.TELEMETRY_SNAPSHOT, init=False)


@dataclass
class AnomalyEnriched(DomainEvent):
    """An anomaly enriched with reasoning, ready for WebSocket / webhook fan-out.

    ``to_message()`` is the FROZEN wire contract shared with the frontend
    (see INTERN-TASKS.md). Do not change field names/shape without updating
    both the frontend WS client and the intern contract doc.
    """

    device_id: int = 0
    point: str = ""
    value: float | None = None
    unit: str = ""
    severity: str = "medium"          # "low" | "medium" | "high"
    anomaly_score: float | None = None
    anomaly_kind: str = "forecast_band_breach"
    # Reasoning (any field may be None until the reasoning layer fills it).
    component: str | None = None
    failure_prob: float | None = None
    eta_hours: float | None = None
    explanation: str | None = None
    # Deepened reasoning (additive — optional; null/empty when LLM disabled).
    root_cause: str | None = None
    contributing_factors: list[str] = field(default_factory=list)
    recommended_action: str | None = None
    confidence: str | None = None
    event_type: EventType = field(default=EventType.ANOMALY_ENRICHED, init=False)

    def to_message(self) -> dict:
        """Serialize to the exact JSON contract pushed over the WebSocket."""
        reasoning = None
        if any(v is not None for v in (self.component, self.failure_prob,
                                       self.eta_hours, self.explanation)):
            # FROZEN keys — must stay exactly as-is for the live frontend grid
            # and webhook subscriber.
            reasoning = {
                "component": self.component,
                "failure_prob": self.failure_prob,
                "eta_hours": self.eta_hours,
                "explanation": self.explanation,
            }
            # Additive deepened reasoning fields (alongside the frozen keys).
            reasoning["root_cause"] = self.root_cause
            reasoning["contributing_factors"] = self.contributing_factors
            reasoning["recommended_action"] = self.recommended_action
            reasoning["confidence"] = self.confidence
        return {
            "type": "anomaly",
            "device_id": self.device_id,
            "point": self.point,
            "value": self.value,
            "unit": self.unit,
            "severity": self.severity,
            "anomaly": {"score": self.anomaly_score, "kind": self.anomaly_kind},
            "reasoning": reasoning,
            "ts": self.timestamp.isoformat(),
        }


@dataclass
class WorkOrderAssigned(DomainEvent):
    """Auto-generated maintenance work order from a predicted future failure.

    Emitted by the pipeline when the predictor projects a point to breach an
    operating limit (finite ETA). Subscribers (WebSocket, webhook) deliver it
    so a failure scenario is actioned BEFORE it happens.
    """

    work_order_id: str = ""
    device_id: int = 0
    point: str = ""
    component: str | None = None
    action: str = ""
    severity: str = "medium"
    eta_hours: float | None = None
    failure_prob: float | None = None
    reason: str = ""
    event_type: EventType = field(default=EventType.WORK_ORDER_ASSIGNED, init=False)

    def to_message(self) -> dict:
        return {
            "type": "work_order",
            "work_order_id": self.work_order_id,
            "device_id": self.device_id,
            "point": self.point,
            "component": self.component,
            "action": self.action,
            "severity": self.severity,
            "eta_hours": self.eta_hours,
            "failure_prob": self.failure_prob,
            "reason": self.reason,
            "ts": self.timestamp.isoformat(),
        }


@dataclass
class ScenarioLifecycleChanged(DomainEvent):
    scenario_id: str = ""
    new_status: ScenarioStatus = ScenarioStatus.IDLE
    event_type: EventType = field(default=EventType.SCENARIO_STARTED, init=False)

    def __post_init__(self) -> None:
        if self.new_status == ScenarioStatus.RUNNING:
            self.event_type = EventType.SCENARIO_STARTED
        elif self.new_status in (ScenarioStatus.STOPPED, ScenarioStatus.IDLE):
            self.event_type = EventType.SCENARIO_STOPPED
