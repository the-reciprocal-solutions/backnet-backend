from bacnet_lab.domain.enums import AlarmSeverity, DeviceStatus, EventType, ScenarioStatus
from bacnet_lab.domain.events import (
    AlarmCleared,
    AlarmRaised,
    AnomalyEnriched,
    DeviceStatusChanged,
    PointValueChanged,
    ScenarioLifecycleChanged,
)

# Frozen wire-contract keys — must always be present (and unchanged) in the
# AnomalyEnriched.to_message() "reasoning" block.
_FROZEN_REASONING_KEYS = {"component", "failure_prob", "eta_hours", "explanation"}


def test_point_value_changed():
    event = PointValueChanged(device_id=1001, point_name="test", old_value=1.0, new_value=2.0)
    assert event.event_type == EventType.POINT_VALUE_CHANGED
    assert event.device_id == 1001


def test_device_status_changed():
    event = DeviceStatusChanged(
        device_id=1001, old_status=DeviceStatus.ONLINE, new_status=DeviceStatus.OFFLINE
    )
    assert event.event_type == EventType.DEVICE_STATUS_CHANGED


def test_alarm_raised():
    event = AlarmRaised(
        alarm_id="abc", device_id=1001, point_name="test",
        severity=AlarmSeverity.HIGH, message="too hot",
    )
    assert event.event_type == EventType.ALARM_RAISED


def test_alarm_cleared():
    event = AlarmCleared(alarm_id="abc", device_id=1001, point_name="test")
    assert event.event_type == EventType.ALARM_CLEARED


def test_scenario_lifecycle_running():
    event = ScenarioLifecycleChanged(scenario_id="test", new_status=ScenarioStatus.RUNNING)
    assert event.event_type == EventType.SCENARIO_STARTED


def test_scenario_lifecycle_stopped():
    event = ScenarioLifecycleChanged(scenario_id="test", new_status=ScenarioStatus.STOPPED)
    assert event.event_type == EventType.SCENARIO_STOPPED


def test_anomaly_enriched_reasoning_additive_fields():
    """The deepened reasoning fields appear ALONGSIDE the frozen keys."""
    event = AnomalyEnriched(
        device_id=1001,
        point="AHU-1/temp",
        value=42.0,
        explanation="value rising",
        component="AHU-1",
        failure_prob=0.7,
        eta_hours=3.5,
        root_cause="AHU-1/temp is rising",
        contributing_factors=["AHU-1/fan changed 10→20 (+10rpm)"],
        recommended_action="Check AHU-1 cooling/airflow and setpoints",
        confidence="high",
    )
    reasoning = event.to_message()["reasoning"]
    # Frozen keys present and unchanged.
    assert _FROZEN_REASONING_KEYS <= set(reasoning)
    assert reasoning["component"] == "AHU-1"
    assert reasoning["failure_prob"] == 0.7
    assert reasoning["eta_hours"] == 3.5
    assert reasoning["explanation"] == "value rising"
    # Additive deepened fields present.
    assert reasoning["root_cause"] == "AHU-1/temp is rising"
    assert reasoning["contributing_factors"] == ["AHU-1/fan changed 10→20 (+10rpm)"]
    assert reasoning["recommended_action"] == "Check AHU-1 cooling/airflow and setpoints"
    assert reasoning["confidence"] == "high"


def test_anomaly_enriched_reasoning_null_when_all_inputs_none():
    """Reasoning stays null only when ALL reasoning inputs are None/empty."""
    event = AnomalyEnriched(device_id=1001, point="AHU-1/temp", value=42.0)
    assert event.to_message()["reasoning"] is None


def test_anomaly_enriched_frozen_keys_with_empty_deepened_fields():
    """Frozen keys intact even when the deepened fields are at safe defaults."""
    event = AnomalyEnriched(
        device_id=1001, point="AHU-1/temp", explanation="only narration",
    )
    reasoning = event.to_message()["reasoning"]
    assert _FROZEN_REASONING_KEYS <= set(reasoning)
    assert reasoning["explanation"] == "only narration"
    # Deepened fields default to null / empty, not absent garbage.
    assert reasoning["root_cause"] is None
    assert reasoning["contributing_factors"] == []
    assert reasoning["recommended_action"] is None
    assert reasoning["confidence"] is None
