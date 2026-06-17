from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass

from bacnet_lab.domain.models.device import Point
from bacnet_lab.domain.value_objects import PointValue


@dataclass
class TickContext:
    """State passed to every generator on each tick.

    ``values`` holds the current value of every point in the simulation keyed
    ``"<device_id>/<object_name>"`` — this lets coupled/derived generators read
    other points (e.g. a return-air temp derived from supply-air temp).
    """

    sim_seconds: float          # cumulative simulated seconds
    dt_sim: float               # simulated seconds since last tick
    hour_of_day: float          # 0..24
    day_phase: float            # 0..1
    noise_level: float          # global noise multiplier (settings)
    values: dict[str, PointValue]


def clamp(value: float, bounds: list | tuple | None) -> float:
    if not bounds or len(bounds) != 2:
        return value
    lo, hi = bounds
    return max(lo, min(hi, value))


class SignalGenerator(ABC):
    """One generator instance per simulated point.

    Subclasses register themselves with ``@register("model_name")`` and
    implement :meth:`next`, returning the point's new present value for this
    tick. The engine handles writing the value and change detection.

    Config keys common to all generators:
      - ``bounds: [lo, hi]``      clamp numeric output
      - ``noise: {type, sigma}``  optional gaussian overlay (applied by helper)
    """

    model: str = "base"

    def __init__(self, point: Point, config: dict, rng: random.Random) -> None:
        self.point = point
        self.config = config or {}
        self.rng = rng
        self.bounds = self.config.get("bounds")
        self.value: PointValue = point.present_value

    @abstractmethod
    def next(self, ctx: TickContext) -> PointValue:
        """Return the new present value for this tick."""
        ...

    # -- helpers ------------------------------------------------------------
    def _apply_noise(self, value: float, ctx: TickContext) -> float:
        noise = self.config.get("noise")
        if not noise:
            return value
        sigma = float(noise.get("sigma", 0.0)) * ctx.noise_level
        if sigma <= 0:
            return value
        return value + self.rng.gauss(0.0, sigma)

    def _finish(self, value: float, ctx: TickContext) -> float:
        return clamp(self._apply_noise(value, ctx), self.bounds)
