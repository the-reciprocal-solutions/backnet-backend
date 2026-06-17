"""Forecast service: ties fetch -> forecast -> store.

Self-contained and runnable standalone. Owns its own :class:`ForecastDB` and
:class:`ChronosForecaster`. Heavy ML deps are optional; see
:mod:`bacnet_lab.forecasting.chronos_model`. Enable real Chronos with:

    pip install chronos-forecasting torch
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from bacnet_lab.forecasting.chronos_model import ChronosForecaster
from bacnet_lab.forecasting.db import ForecastDB

logger = logging.getLogger(__name__)

# resolution -> step between successive samples / forecast horizons (seconds).
_RESOLUTION_STEP_S: dict[str, int] = {
    "1m": 60,
    "15m": 900,
    "1h": 3600,
    "raw": 5,
}


@dataclass
class ForecastResult:
    object_name: str
    made_at: datetime
    horizon_ts: list[datetime]
    p10: list[float]
    p50: list[float]
    p90: list[float]
    model: str


class ForecastService:
    """Orchestrates fetching a series, forecasting it, and storing the result."""

    def __init__(
        self,
        dsn: str,
        model_name: str = "amazon/chronos-bolt-small",
    ) -> None:
        self.model_name = model_name
        self.db = ForecastDB(dsn)
        self._model = ChronosForecaster(model_name=model_name)

    async def start(self) -> None:
        await self.db.connect()
        await self.db.ensure_forecast_table()

    async def stop(self) -> None:
        await self.db.close()

    async def forecast_point(
        self,
        object_name: str,
        lookback_s: int = 3600,
        resolution: str = "1m",
        horizon: int = 12,
        store: bool = True,
    ) -> ForecastResult:
        """Fetch recent series, forecast ``horizon`` steps, optionally store."""
        made_at = datetime.now().astimezone()
        step_s = _RESOLUTION_STEP_S.get(resolution, 60)
        horizon = max(int(horizon), 1)

        times, values = await self.db.fetch_series(
            object_name, timedelta(seconds=lookback_s), resolution
        )

        forecast = self._model.forecast(values, horizon)

        # Future timestamps spaced by the resolution step, anchored to the last
        # observed sample when available, else to now().
        anchor = times[-1] if times else made_at
        horizon_ts = [
            anchor + timedelta(seconds=step_s * (i + 1)) for i in range(horizon)
        ]

        result = ForecastResult(
            object_name=object_name,
            made_at=made_at,
            horizon_ts=horizon_ts,
            p10=list(forecast.get("p10", [])),
            p50=list(forecast.get("p50", [])),
            p90=list(forecast.get("p90", [])),
            model=str(forecast.get("model", "naive")),
        )

        if store:
            try:
                await self.db.store_forecast(
                    object_name,
                    made_at,
                    horizon_ts,
                    result.p10,
                    result.p50,
                    result.p90,
                    result.model,
                )
            except Exception as e:  # pragma: no cover - store is best-effort
                logger.error("ForecastService store failed for %r: %s", object_name, e)

        return result

    def info(self) -> dict:
        return {
            "model_name": self.model_name,
            "chronos_available": self._model.available(),
            "db_ready": self.db.ready,
            "resolutions": list(_RESOLUTION_STEP_S.keys()),
        }
