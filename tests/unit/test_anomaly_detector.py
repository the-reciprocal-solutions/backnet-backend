import pytest
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

from bacnet_lab.domain.events import PointValueChanged, AlarmRaised
from bacnet_lab.domain.enums import EventType, AlarmSeverity
from bacnet_lab.forecasting.anomaly_detector import AnomalyDetector


@pytest.mark.asyncio
async def test_anomaly_detector_raises_alarm_below_p10():
    # Setup
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    
    db = MagicMock()
    db.latest_forecast = AsyncMock()
    
    # Mock a forecast that predicts [20, 25] (P10, P90)
    now = datetime.now(timezone.utc)
    db.latest_forecast.return_value = [
        {
            "horizon_ts": now,
            "p10": 20.0,
            "p90": 25.0,
            "model": "test-model"
        }
    ]
    
    detector = AnomalyDetector(publisher, db)
    
    # Trigger an event with value 15 (below P10=20)
    event = PointValueChanged(
        device_id=1001,
        point_name="TestPoint",
        old_value=22.0,
        new_value=15.0,
        timestamp=now
    )
    
    await detector.check_anomaly(event)
    
    # Verify alarm raised (published as event)
    publisher.publish.assert_called_once()
    alarm = publisher.publish.call_args[0][0]
    assert isinstance(alarm, AlarmRaised)
    assert alarm.point_name == "TestPoint"
    assert "below forecast threshold 20.00" in alarm.message
    assert alarm.severity == AlarmSeverity.MEDIUM


@pytest.mark.asyncio
async def test_anomaly_detector_raises_alarm_above_p90():
    # Setup
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    
    db = MagicMock()
    db.latest_forecast = AsyncMock()
    
    # Mock a forecast that predicts [20, 25] (P10, P90)
    now = datetime.now(timezone.utc)
    db.latest_forecast.return_value = [
        {
            "horizon_ts": now,
            "p10": 20.0,
            "p90": 25.0,
            "model": "test-model"
        }
    ]
    
    detector = AnomalyDetector(publisher, db)
    
    # Trigger an event with value 30 (above P90=25)
    event = PointValueChanged(
        device_id=1001,
        point_name="TestPoint",
        old_value=22.0,
        new_value=30.0,
        timestamp=now
    )
    
    await detector.check_anomaly(event)
    
    # Verify alarm raised
    publisher.publish.assert_called_once()
    alarm = publisher.publish.call_args[0][0]
    assert isinstance(alarm, AlarmRaised)
    assert alarm.point_name == "TestPoint"
    assert "above forecast threshold 25.00" in alarm.message


@pytest.mark.asyncio
async def test_anomaly_detector_no_alarm_within_range():
    # Setup
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    
    db = MagicMock()
    db.latest_forecast = AsyncMock()
    
    # Mock a forecast that predicts [20, 25]
    now = datetime.now(timezone.utc)
    db.latest_forecast.return_value = [
        {"horizon_ts": now, "p10": 20.0, "p90": 25.0, "model": "test"}
    ]
    
    detector = AnomalyDetector(publisher, db)
    
    # Trigger an event with value 22 (within [20, 25])
    event = PointValueChanged(
        device_id=1001,
        point_name="TestPoint",
        old_value=21.0,
        new_value=22.0,
        timestamp=now
    )
    
    await detector.check_anomaly(event)
    
    # Verify NO alarm raised
    publisher.publish.assert_not_called()


@pytest.mark.asyncio
async def test_anomaly_detector_skips_if_no_forecast():
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    db = MagicMock()
    db.latest_forecast = AsyncMock(return_value=[])
    
    detector = AnomalyDetector(publisher, db)
    event = PointValueChanged(device_id=1001, point_name="TestPoint", new_value=30.0)
    
    await detector.check_anomaly(event)
    publisher.publish.assert_not_called()


@pytest.mark.asyncio
async def test_anomaly_detector_skips_if_forecast_too_old():
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    db = MagicMock()
    
    now = datetime.now(timezone.utc)
    # Forecast for 1 hour ago
    db.latest_forecast = AsyncMock(return_value=[
        {"horizon_ts": now - timedelta(hours=1), "p10": 20.0, "p90": 25.0}
    ])
    
    detector = AnomalyDetector(publisher, db)
    event = PointValueChanged(device_id=1001, point_name="TestPoint", new_value=30.0, timestamp=now)
    
    await detector.check_anomaly(event)
    publisher.publish.assert_not_called()
