from __future__ import annotations

import math

from bacnet_lab.adapters.simulation.generators.base import SignalGenerator, TickContext, clamp
from bacnet_lab.adapters.simulation.registry import register
from bacnet_lab.domain.value_objects import PointValue


@register("constant")
class ConstantGenerator(SignalGenerator):
    """Holds a fixed value (optionally with noise overlay)."""

    def next(self, ctx: TickContext) -> PointValue:
        base = float(self.config.get("value", self.point.present_value))
        return round(self._finish(base, ctx), 3)


@register("sine")
class SineGenerator(SignalGenerator):
    """Sinusoid: value = mean + amplitude * sin(2*pi*(hour - phase_hour)/period_h).

    Defaults to a 24h diurnal cycle.
    """

    def next(self, ctx: TickContext) -> PointValue:
        mean = float(self.config.get("mean", self.point.present_value))
        amplitude = float(self.config.get("amplitude", 1.0))
        period_h = float(self.config.get("period_h", 24.0))
        phase_h = float(self.config.get("phase_h", 0.0))
        angle = 2 * math.pi * ((ctx.hour_of_day - phase_h) / period_h)
        val = mean + amplitude * math.sin(angle)
        return round(self._finish(val, ctx), 3)


@register("random_walk")
class RandomWalkGenerator(SignalGenerator):
    """Brownian drift: value += N(0, step), clamped to bounds.

    Optional ``mean_reversion`` pulls the value back toward ``center``.
    """

    def __init__(self, point, config, rng) -> None:
        super().__init__(point, config, rng)
        self._center = float(self.config.get("center", float(point.present_value or 0.0)))
        self._current = float(point.present_value or self._center)

    def next(self, ctx: TickContext) -> PointValue:
        step = float(self.config.get("step", 0.1)) * ctx.noise_level
        reversion = float(self.config.get("mean_reversion", 0.0))
        self._current += self.rng.gauss(0.0, step)
        if reversion > 0:
            self._current += (self._center - self._current) * reversion
        self._current = clamp(self._current, self.bounds)
        return round(self._current, 3)


@register("gaussian_noise")
class GaussianNoiseGenerator(SignalGenerator):
    """Pure gaussian jitter around a fixed mean."""

    def next(self, ctx: TickContext) -> PointValue:
        mean = float(self.config.get("mean", self.point.present_value))
        sigma = float(self.config.get("sigma", 0.1)) * ctx.noise_level
        val = mean + self.rng.gauss(0.0, sigma)
        return round(clamp(val, self.bounds), 3)


@register("multistate_cycle")
class MultistateCycleGenerator(SignalGenerator):
    """Rotates through a list of discrete states on a fixed period.

    For binary/multistate points (occupancy, fan stage, mode).
    """

    def __init__(self, point, config, rng) -> None:
        super().__init__(point, config, rng)
        self._states = self.config.get("states", [point.present_value])
        self._period_s = float(self.config.get("period_s", 3600.0))

    def next(self, ctx: TickContext) -> PointValue:
        if not self._states:
            return self.point.present_value
        idx = int((ctx.sim_seconds // max(1.0, self._period_s)) % len(self._states))
        return self._states[idx]
