"""Periodic forecast scheduler.

The anomaly detector and the frontend's predictive views only work while the
`forecast` table holds fresh rows (the detector ignores forecasts older than
~15 min). Nothing kept it fresh — so detection went blind shortly after boot.

This service re-forecasts every analog point on a fixed interval and stores the
result, keeping predictions and anomaly detection continuously alive.
"""

from __future__ import annotations

import asyncio
import logging

from bacnet_lab.application.device_service import DeviceService
from bacnet_lab.forecasting.service import ForecastService
from bacnet_lab.infrastructure.config import ForecastSettings

logger = logging.getLogger(__name__)


class ForecastScheduler:
    def __init__(
        self,
        forecast_service: ForecastService,
        device_service: DeviceService,
        settings: ForecastSettings,
    ) -> None:
        self._fc = forecast_service
        self._ds = device_service
        self._cfg = settings
        self._running = False
        self._task: asyncio.Task | None = None

    # Build the forecastable object names (device-prefixed, e.g. AHU-01/SupplyAirTemp).
    def _target_points(self) -> list[str]:
        names: list[str] = []
        for d in self._ds.get_all_in_memory_devices():
            for p in d.points:
                ot = getattr(p.object_type, "value", str(p.object_type)).lower()
                if self._cfg.analog_only and "analog" not in ot:
                    continue
                # object_name already carries the device prefix (AHU-01/SupplyAirTemp)
                names.append(p.object_name)
        if self._cfg.max_points > 0:
            names = names[: self._cfg.max_points]
        return names

    async def start(self) -> None:
        if not self._cfg.enabled:
            logger.info("Forecast scheduler disabled (BACNET_LAB_FORECAST_ENABLED=false)")
            return
        if not self._fc.db.ready:
            logger.warning("Forecast scheduler: forecast DB not ready — skipping")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Forecast scheduler started (interval=%ss, horizon=%d, res=%s, concurrency=%d)",
            self._cfg.interval_s, self._cfg.horizon, self._cfg.resolution, self._cfg.concurrency,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Forecast scheduler stopped")

    async def _loop(self) -> None:
        # Bound concurrent Chronos calls so we don't saturate CPU/GPU.
        sem = asyncio.Semaphore(max(1, self._cfg.concurrency))

        async def one(name: str) -> bool:
            async with sem:
                try:
                    await self._fc.forecast_point(
                        name,
                        lookback_s=self._cfg.lookback_s,
                        resolution=self._cfg.resolution,
                        horizon=self._cfg.horizon,
                        store=True,
                    )
                    return True
                except Exception as e:  # best-effort per point
                    logger.debug("Forecast scheduler: %r failed: %s", name, e)
                    return False

        while self._running:
            points = self._target_points()
            if points:
                results = await asyncio.gather(*(one(n) for n in points))
                ok = sum(1 for r in results if r)
                logger.info("Forecast scheduler: refreshed %d/%d points", ok, len(points))
            else:
                logger.warning("Forecast scheduler: no target points found")
            # Sleep in short slices so stop() is responsive.
            slept = 0.0
            while self._running and slept < self._cfg.interval_s:
                await asyncio.sleep(min(2.0, self._cfg.interval_s - slept))
                slept += 2.0
