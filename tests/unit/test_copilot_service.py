import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from bacnet_lab.copilot.service import CopilotService, CopilotResult
from bacnet_lab.domain.events import AnomalyEnriched
from bacnet_lab.infrastructure.config import LLMSettings, TimescaleSettings


@pytest.fixture
def mock_forecast_service():
    fs = MagicMock()
    # Mock return value for forecast_point
    mock_forecast = MagicMock()
    mock_forecast.p50 = [25.0]
    mock_forecast.p10 = [20.0]
    mock_forecast.p90 = [30.0]
    mock_forecast.model = "test-forecast-model"
    fs.forecast_point = AsyncMock(return_value=mock_forecast)
    return fs


@pytest.fixture
def mock_llm_settings():
    return LLMSettings(
        enabled=True,
        base_url="http://localhost:11434",
        api_key="test-key",
        model="test-llm-model",
        timeout_s=10.0,
    )


@pytest.fixture
def mock_ts_settings():
    return TimescaleSettings(dsn="sqlite:///:memory:")


@pytest.mark.asyncio
async def test_copilot_explain_llm_enabled(mock_forecast_service, mock_llm_settings, mock_ts_settings):
    service = CopilotService(mock_forecast_service, mock_llm_settings, mock_ts_settings)
    
    # Mock database resolves and queries
    service._db.resolve_device = AsyncMock(return_value=(1001, "AHU-01"))
    service._db.point_delta = AsyncMock(return_value={"units": "C", "old": 20.0, "new": 25.0, "delta": 5.0})
    service._db.driver_deltas = AsyncMock(return_value=[])
    service._db.recent_events = AsyncMock(return_value=[])
    
    # Mock LLM response with valid JSON matching our new schema
    mock_json_response = {
        "explanation": "Prediction: AHU-1.temp ≈ 25C in 6 min (range 20–30)\nReason:\n- temp rising",
        "root_cause": "cooling valve stuck closed",
        "contributing_factors": ["vibration trended up 3x", "related fan temp increased by 2C"],
        "recommended_action": "Check cooling actuator power and override status",
        "confidence": 0.85
    }
    service._llm.chat = AsyncMock(return_value=json.dumps(mock_json_response))
    
    result = await service.explain("AHU-1.temp")
    
    assert isinstance(result, CopilotResult)
    assert result.answer == mock_json_response["explanation"]
    assert result.root_cause == mock_json_response["root_cause"]
    assert result.contributing_factors == mock_json_response["contributing_factors"]
    assert result.recommended_action == mock_json_response["recommended_action"]
    assert result.confidence == 0.85
    assert result.grounded is True


@pytest.mark.asyncio
async def test_copilot_explain_llm_disabled(mock_forecast_service, mock_ts_settings):
    disabled_settings = LLMSettings(
        enabled=False,
        base_url="http://localhost:11434",
        api_key="",
        model="test-llm-model",
        timeout_s=10.0,
    )
    service = CopilotService(mock_forecast_service, disabled_settings, mock_ts_settings)
    
    # Mock database queries
    service._db.resolve_device = AsyncMock(return_value=(1001, "AHU-01"))
    service._db.point_delta = AsyncMock(return_value={"units": "C", "old": 20.0, "new": 25.0, "delta": 5.0})
    service._db.driver_deltas = AsyncMock(return_value=[])
    service._db.recent_events = AsyncMock(return_value=[])
    
    result = await service.explain("AHU-1.temp")
    
    assert isinstance(result, CopilotResult)
    # Check that answer falls back to the deterministic format
    assert "Prediction: AHU-1.temp" in result.answer
    # New fields should fall back gracefully to None
    assert result.root_cause is None
    assert result.contributing_factors is None
    assert result.recommended_action is None
    assert result.confidence is None


def test_anomaly_enriched_serialization():
    # 1. When reasoning is fully populated
    event = AnomalyEnriched(
        device_id=1001,
        point="AHU-1.temp",
        value=25.0,
        unit="C",
        severity="high",
        anomaly_score=0.9,
        anomaly_kind="temp_excursion",
        component="AHU-1",
        failure_prob=0.78,
        eta_hours=36.0,
        explanation="Test explanation",
        root_cause="cooling valve failure",
        contributing_factors=["high ambient temp"],
        recommended_action="Replace valve",
        confidence=0.85,
    )
    
    msg = event.to_message()
    assert msg["type"] == "anomaly"
    assert msg["device_id"] == 1001
    
    reasoning = msg["reasoning"]
    assert reasoning is not None
    assert reasoning["component"] == "AHU-1"
    assert reasoning["explanation"] == "Test explanation"
    assert reasoning["root_cause"] == "cooling valve failure"
    assert reasoning["contributing_factors"] == ["high ambient temp"]
    assert reasoning["recommended_action"] == "Replace valve"
    assert reasoning["confidence"] == 0.85

    # 2. When reasoning is partially populated and new fields are missing (backwards compatibility check)
    legacy_event = AnomalyEnriched(
        device_id=1001,
        point="AHU-1.temp",
        value=25.0,
        unit="C",
        severity="medium",
        anomaly_score=0.5,
        anomaly_kind="forecast_band_breach",
        component="AHU-1",
        explanation="Legacy test explanation",
    )
    
    legacy_msg = legacy_event.to_message()
    assert legacy_msg["reasoning"]["explanation"] == "Legacy test explanation"
    assert legacy_msg["reasoning"]["root_cause"] is None
    assert legacy_msg["reasoning"]["confidence"] is None
