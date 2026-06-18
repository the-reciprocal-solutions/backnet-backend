"""Predictive-layer validation scenarios.

Each scenario injects a KNOWN fault pattern so the anomaly + prediction +
reasoning pipeline can be confirmed end-to-end:

    AHU vibration spike        -> bearing/imbalance failure signature
    Cooling inefficiency       -> running but not cooling (ΔT collapse)
    Sensor stuck (flatline)    -> dead sensor while forecast expects motion
    Compressor short-cycling   -> rapid oscillation / instability

The simulation engine drives every point continuously, so each scenario
RE-ASSERTS its injected value on a fixed interval; otherwise the sim washes
the fault out within a tick. Stopping the scenario lets the sim recover the
point naturally.
"""

from __future__ import annotations

import asyncio
import logging

from bacnet_lab.adapters.scenarios.base import BaseScenario
from bacnet_lab.domain.models.scenario import ScenarioParameter

logger = logging.getLogger(__name__)


class AhuVibrationScenario(BaseScenario):
    id = "ahu_vibration"
    name = "AHU Vibration Spike"
    description = "Ramps AHU fan vibration from baseline to a high level to trigger a bearing/imbalance prediction."

    def default_parameters(self) -> list[ScenarioParameter]:
        return [
            ScenarioParameter(name="device_id", description="Target device ID", default=1001),
            ScenarioParameter(name="point_name", description="Vibration point", default="AHU-01/Vibration"),
            ScenarioParameter(name="baseline", description="Baseline vibration (mm/s)", default=2.0),
            ScenarioParameter(name="peak", description="Peak vibration (mm/s)", default=14.0),
            ScenarioParameter(name="ramp_s", description="Ramp duration (s)", default=120.0),
            ScenarioParameter(name="interval_s", description="Re-assert interval (s)", default=2.0),
        ]

    async def run(self) -> None:
        device_id = int(self._parameters[0].value)
        point = str(self._parameters[1].value)
        baseline = float(self._parameters[2].value)
        peak = float(self._parameters[3].value)
        ramp_s = float(self._parameters[4].value)
        interval = float(self._parameters[5].value)

        elapsed = 0.0
        while self.is_running:
            frac = min(1.0, elapsed / ramp_s) if ramp_s > 0 else 1.0
            value = baseline + (peak - baseline) * frac
            await self._write(device_id, point, round(value, 2))
            await asyncio.sleep(interval)
            elapsed += interval

    async def _write(self, device_id: int, point: str, value: float) -> None:
        try:
            await self._device_service.write_point_by_name(device_id, point, value)
        except Exception as e:
            logger.error("%s write failed for %s: %s", self.id, point, e)


class CoolingInefficiencyScenario(BaseScenario):
    id = "cooling_inefficiency"
    name = "Cooling Inefficiency (temp vs runtime)"
    description = "Holds the cooling valve wide open with the fan running while supply air stays warm — cooling demanded but ΔT collapses."

    def default_parameters(self) -> list[ScenarioParameter]:
        return [
            ScenarioParameter(name="device_id", description="Target device ID", default=1001),
            ScenarioParameter(name="supply_point", description="Supply air temp point", default="AHU-01/SupplyAirTemp"),
            ScenarioParameter(name="valve_point", description="Cooling valve point", default="AHU-01/CoolingValve"),
            ScenarioParameter(name="stuck_supply_temp", description="Held supply temp (°C, should be cool but isn't)", default=29.0),
            ScenarioParameter(name="valve_open", description="Cooling valve position (%)", default=100.0),
            ScenarioParameter(name="interval_s", description="Re-assert interval (s)", default=2.0),
        ]

    async def run(self) -> None:
        device_id = int(self._parameters[0].value)
        supply = str(self._parameters[1].value)
        valve = str(self._parameters[2].value)
        warm_temp = float(self._parameters[3].value)
        valve_open = float(self._parameters[4].value)
        interval = float(self._parameters[5].value)

        # Full cooling demanded, but supply air refuses to drop -> inefficiency.
        while self.is_running:
            try:
                await self._device_service.write_point_by_name(device_id, valve, valve_open)
                await self._device_service.write_point_by_name(device_id, supply, warm_temp)
            except Exception as e:
                logger.error("%s write failed: %s", self.id, e)
            await asyncio.sleep(interval)


class SensorStuckScenario(BaseScenario):
    id = "sensor_stuck"
    name = "Sensor Stuck (flatline)"
    description = "Freezes a sensor at a fixed value so it flatlines while the forecast expects normal variation."

    def default_parameters(self) -> list[ScenarioParameter]:
        return [
            ScenarioParameter(name="device_id", description="Target device ID", default=1001),
            ScenarioParameter(name="point_name", description="Sensor point to freeze", default="AHU-01/ReturnAirTemp"),
            ScenarioParameter(name="stuck_value", description="Frozen value", default=24.0),
            ScenarioParameter(name="interval_s", description="Re-assert interval (s)", default=1.0),
        ]

    async def run(self) -> None:
        device_id = int(self._parameters[0].value)
        point = str(self._parameters[1].value)
        stuck = float(self._parameters[2].value)
        interval = float(self._parameters[3].value)

        while self.is_running:
            try:
                await self._device_service.write_point_by_name(device_id, point, stuck)
            except Exception as e:
                logger.error("%s write failed for %s: %s", self.id, point, e)
            await asyncio.sleep(interval)


class CompressorShortCycleScenario(BaseScenario):
    id = "compressor_short_cycle"
    name = "Compressor Short-Cycling"
    description = "Oscillates a point rapidly between low and high to mimic unstable short-cycling."

    def default_parameters(self) -> list[ScenarioParameter]:
        return [
            ScenarioParameter(name="device_id", description="Target device ID", default=1001),
            ScenarioParameter(name="point_name", description="Point to oscillate", default="AHU-01/CoolingValve"),
            ScenarioParameter(name="low", description="Low value", default=0.0),
            ScenarioParameter(name="high", description="High value", default=100.0),
            ScenarioParameter(name="period_s", description="Full cycle period (s)", default=6.0),
        ]

    async def run(self) -> None:
        device_id = int(self._parameters[0].value)
        point = str(self._parameters[1].value)
        low = float(self._parameters[2].value)
        high = float(self._parameters[3].value)
        period = max(1.0, float(self._parameters[4].value))

        half = period / 2.0
        state_high = True
        while self.is_running:
            try:
                await self._device_service.write_point_by_name(
                    device_id, point, high if state_high else low
                )
            except Exception as e:
                logger.error("%s write failed for %s: %s", self.id, point, e)
            state_high = not state_high
            await asyncio.sleep(half)
