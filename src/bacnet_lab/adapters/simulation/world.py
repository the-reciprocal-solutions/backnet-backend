from __future__ import annotations

import math

from bacnet_lab.adapters.simulation.generators.base import SignalGenerator, TickContext
from bacnet_lab.adapters.simulation.registry import register
from bacnet_lab.domain.value_objects import PointValue

# weather profile -> (mean outdoor temp degC, diurnal amplitude degC)
_WEATHER: dict[str, tuple[float, float]] = {
    "temperate": (18.0, 8.0),
    "hot": (30.0, 6.0),
    "cold": (2.0, 6.0),
}

# CO2 targets (ppm)
_CO2_OCCUPIED = 900.0
_CO2_EMPTY = 420.0


class WorldModel:
    """Shared building environment advanced once per tick.

    Holds coupled physical state (outdoor temperature, zone temperature/CO2/
    humidity, occupancy) and exposes it to generators by writing synthetic
    ``"world/<var>"`` keys into the engine value dict. Generators read those
    keys via the ``world_var`` model so simulated points become physically
    coupled rather than independent random walks.
    """

    def __init__(self, weather: str, occupancy: str) -> None:
        self.weather = weather if weather in _WEATHER else "temperate"
        self.occupancy_mode = occupancy
        mean, _amp = _WEATHER[self.weather]

        self.outdoor_temp: float = mean
        self.zone_temp: float = 21.0
        self.zone_co2: float = _CO2_EMPTY
        self.zone_humidity: float = 45.0
        self.occupancy: float = 0.0

    # -- schedule -----------------------------------------------------------
    def _occupancy_for(self, hour_of_day: float) -> float:
        if self.occupancy_mode == "24x7":
            return 1.0
        if self.occupancy_mode == "none":
            return 0.0
        # default: office hours 7..19
        return 1.0 if 7.0 <= hour_of_day < 19.0 else 0.0

    # -- HVAC readback ------------------------------------------------------
    @staticmethod
    def _avg_actuator(values: dict, needle: str) -> float | None:
        total = 0.0
        count = 0
        for key, val in values.items():
            if needle in key and isinstance(val, (int, float)):
                total += float(val)
                count += 1
        if count == 0:
            return None
        return total / count

    # -- step ---------------------------------------------------------------
    def step(
        self,
        sim_seconds: float,
        hour_of_day: float,
        day_phase: float,
        dt_sim: float,
        values: dict,
    ) -> dict[str, float]:
        mean, amp = _WEATHER[self.weather]

        # Outdoor temp: diurnal sine, min near 5:00, max near 15:00.
        # sin is max when its argument is pi/2; shift so hour 15 -> peak.
        angle = 2.0 * math.pi * (hour_of_day - 9.0) / 24.0
        self.outdoor_temp = mean + amp * math.sin(angle)

        # Occupancy schedule.
        self.occupancy = self._occupancy_for(hour_of_day)

        # First-order helper: x -> x + (target - x) * (1 - exp(-dt/tau)).
        def _relax(current: float, target: float, tau_s: float) -> float:
            if dt_sim <= 0.0 or tau_s <= 0.0:
                return current
            k = 1.0 - math.exp(-dt_sim / tau_s)
            return current + (target - current) * k

        # Zone CO2: rises toward occupied target when occupied, decays when empty.
        co2_target = _CO2_OCCUPIED if self.occupancy > 0.0 else _CO2_EMPTY
        # ramp-up faster than decay
        co2_tau = 900.0 if self.occupancy > 0.0 else 1800.0
        self.zone_co2 = _relax(self.zone_co2, co2_target, co2_tau)

        # Zone humidity: drift 40-60%, loosely tied to occupancy.
        hum_target = 55.0 if self.occupancy > 0.0 else 42.0
        self.zone_humidity = _relax(self.zone_humidity, hum_target, 1800.0)

        # Zone temperature heat balance.
        # Envelope loss pulls zone toward outdoor temp (small coefficient).
        envelope_coeff = 1.0 / 7200.0  # per second
        # Internal gain from people (degC/hour at full occupancy).
        internal_gain_c_per_s = (1.5 / 3600.0) * self.occupancy

        # HVAC actuator readback (0..100). Cooling subtracts, heating adds.
        cooling = self._avg_actuator(values, "CoolingValve") or 0.0
        heating = self._avg_actuator(values, "HeatingValve") or 0.0
        fan = self._avg_actuator(values, "FanSpeed")
        fan_factor = 1.0 if fan is None else (0.2 + 0.8 * (fan / 100.0))

        # Max HVAC authority in degC/hour at 100% valve and full fan.
        cool_c_per_s = (6.0 / 3600.0) * (cooling / 100.0) * fan_factor
        heat_c_per_s = (6.0 / 3600.0) * (heating / 100.0) * fan_factor

        if dt_sim > 0.0:
            d_temp = (
                (self.outdoor_temp - self.zone_temp) * envelope_coeff
                + internal_gain_c_per_s
                - cool_c_per_s
                + heat_c_per_s
            ) * dt_sim
            self.zone_temp += d_temp

        return {
            "outdoor_temp": round(self.outdoor_temp, 3),
            "zone_temp": round(self.zone_temp, 3),
            "zone_co2": round(self.zone_co2, 3),
            "zone_humidity": round(self.zone_humidity, 3),
            "occupancy": round(self.occupancy, 3),
        }


@register("world_var")
class WorldVarGenerator(SignalGenerator):
    """Mirrors a shared :class:`WorldModel` variable onto a point.

    Reads ``ctx.values["world/<var>"]`` (populated by the engine each tick
    before generators run) and applies ``scale * x + offset``, then noise and
    bounds. Config:
      - ``var``     world variable name, e.g. ``"zone_temp"`` (required-ish)
      - ``scale``   multiplier (default 1.0)
      - ``offset``  additive offset (default 0.0)
      - ``bounds``  ``[lo, hi]`` clamp (inherited)
      - ``noise``   gaussian overlay (inherited)
    If the world key is missing, the last emitted value is held.
    """

    def __init__(self, point, config, rng) -> None:
        super().__init__(point, config, rng)
        self._var = str(self.config.get("var", "zone_temp"))
        self._scale = float(self.config.get("scale", 1.0))
        self._offset = float(self.config.get("offset", 0.0))

    def next(self, ctx: TickContext) -> PointValue:
        raw = ctx.values.get(f"world/{self._var}")
        if not isinstance(raw, (int, float)):
            return self.value
        val = self._scale * float(raw) + self._offset
        self.value = round(self._finish(val, ctx), 3)
        return self.value
