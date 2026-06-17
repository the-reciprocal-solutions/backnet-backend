import pytest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from bacnet_lab.application.prediction_service import PredictionService


def _point(name="MotorTemp", units="degreesCelsius", value=25.0):
    return SimpleNamespace(
        object_name=name,
        object_type="analogInput",
        units=units,
        present_value=value,
    )


def _device(device_id=1001, name="AHU-01", points=None):
    return SimpleNamespace(
        device_id=device_id,
        name=name,
        points=points or [_point()],
    )


def _forecast_rows(made_at, p50_seq, p10_seq=None, p90_seq=None, step_s=60):
    rows = []
    for i, p50 in enumerate(p50_seq):
        rows.append(
            {
                "made_at": made_at,
                "horizon_ts": made_at + timedelta(seconds=step_s * (i + 1)),
                "p10": (p10_seq or p50_seq)[i],
                "p50": p50,
                "p90": (p90_seq or p50_seq)[i],
                "model": "test",
            }
        )
    return rows


def _build_service(fetch_series_ret, latest_forecast_ret, device, alarms=None):
    db = MagicMock()
    db.fetch_series = AsyncMock(return_value=fetch_series_ret)
    db.latest_forecast = AsyncMock(return_value=latest_forecast_ret)

    forecast_service = MagicMock()
    forecast_service.db = db

    device_service = MagicMock()
    device_service.get_all_in_memory_devices = MagicMock(return_value=[device])

    asset_service = MagicMock()
    alarm_service = MagicMock()
    alarm_service.get_active_alarms = AsyncMock(return_value=alarms or [])

    return PredictionService(
        forecast_service, device_service, asset_service, alarm_service
    )


@pytest.mark.asyncio
async def test_scan_predicts_breach():
    # Stable history around 25C (mu~25, small sigma). Forecast climbs past the
    # 45C hard limit -> a predicted critical failure.
    hist_vals = [24.0, 25.0, 26.0, 25.0, 24.0, 25.0, 26.0, 25.0, 24.0, 26.0]
    times = [datetime.now(timezone.utc)] * len(hist_vals)
    made_at = datetime.now(timezone.utc)
    rows = _forecast_rows(made_at, [30.0, 40.0, 48.0, 50.0], p90_seq=[31.0, 42.0, 50.0, 52.0])

    svc = _build_service((times, hist_vals), rows, _device())
    out = await svc.scan_predictions()

    assert len(out) == 1
    pred = out[0]
    assert pred["direction"] == "rising"
    assert pred["level"] in ("critical", "high", "elevated")
    assert pred["eta_minutes"] is not None
    assert pred["confidence"] == "high"  # p90 also crosses


@pytest.mark.asyncio
async def test_scan_in_envelope_yields_none():
    # History and forecast both hover around 25C -> no breach, no watch.
    hist_vals = [24.0, 25.0, 26.0, 25.0, 24.0, 25.0, 26.0, 25.0, 24.0, 26.0]
    times = [datetime.now(timezone.utc)] * len(hist_vals)
    made_at = datetime.now(timezone.utc)
    rows = _forecast_rows(made_at, [25.0, 25.0, 25.0, 25.0])

    svc = _build_service((times, hist_vals), rows, _device())
    out = await svc.scan_predictions()
    assert out == []


@pytest.mark.asyncio
async def test_scan_skips_insufficient_history():
    times = [datetime.now(timezone.utc)] * 3
    rows = _forecast_rows(datetime.now(timezone.utc), [50.0, 60.0])
    svc = _build_service((times, [24.0, 25.0, 26.0]), rows, _device())
    out = await svc.scan_predictions()
    assert out == []


@pytest.mark.asyncio
async def test_asset_health_score_math():
    # Asset with one HIGH alarm (-25) and one predicted failure (-20) -> 55.
    asset = SimpleNamespace(id="a1", name="AHU-01", device_id=1001)

    hist_vals = [24.0, 25.0, 26.0, 25.0, 24.0, 25.0, 26.0, 25.0, 24.0, 26.0]
    times = [datetime.now(timezone.utc)] * len(hist_vals)
    rows = _forecast_rows(
        datetime.now(timezone.utc), [30.0, 40.0, 48.0, 50.0], p90_seq=[31.0, 42.0, 50.0, 52.0]
    )
    alarm = SimpleNamespace(device_id=1001, severity="high")

    svc = _build_service((times, hist_vals), rows, _device(), alarms=[alarm])
    svc._asset_service.get_asset = AsyncMock(return_value=asset)

    health = await svc.asset_health("a1")
    assert health["score"] == 55  # 100 - 25 - 20
    assert health["status"] == "Watch"
    assert health["active_alarms"] == 1
    assert len(health["predictions"]) == 1
    assert health["rul_minutes"] is not None


@pytest.mark.asyncio
async def test_asset_health_missing_returns_none():
    svc = _build_service(([], []), [], _device())
    svc._asset_service.get_asset = AsyncMock(return_value=None)
    assert await svc.asset_health("nope") is None


@pytest.mark.asyncio
async def test_fleet_kpi():
    asset = SimpleNamespace(id="a1", name="AHU-01", device_id=1001)
    hist_vals = [24.0, 25.0, 26.0, 25.0, 24.0, 25.0, 26.0, 25.0, 24.0, 26.0]
    times = [datetime.now(timezone.utc)] * len(hist_vals)
    rows = _forecast_rows(
        datetime.now(timezone.utc), [30.0, 40.0, 48.0, 50.0], p90_seq=[31.0, 42.0, 50.0, 52.0]
    )

    svc = _build_service((times, hist_vals), rows, _device())
    svc._asset_service.list_assets = AsyncMock(return_value=[asset])

    kpi = await svc.fleet_kpi()
    assert kpi["assets_total"] == 1
    assert kpi["predicted_failures"] == 1
    assert kpi["avg_health"] == 80  # 100 - 20 (one failure)
    assert kpi["assets_at_risk"] == 0
