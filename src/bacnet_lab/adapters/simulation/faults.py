from __future__ import annotations

import random
from dataclasses import dataclass, field

from bacnet_lab.adapters.simulation.generators.base import TickContext

# Fault kinds understood by the injector.
FAULT_KINDS = ("stuck", "spike", "drift", "offline", "noise_burst")

# Default durations (simulated seconds) per kind when none supplied.
_DEFAULT_DURATION: dict[str, float] = {
    "stuck": 120.0,
    "spike": 5.0,
    "drift": 300.0,
    "offline": 120.0,
    "noise_burst": 30.0,
}


@dataclass
class ActiveFault:
    """A fault currently overlaid on a single point."""

    point_key: str
    kind: str
    started_s: float
    duration_s: float
    params: dict = field(default_factory=dict)
    # Frozen value captured for ``stuck`` faults (set on first apply).
    frozen_value: object | None = None

    def expired(self, sim_seconds: float) -> bool:
        if self.duration_s <= 0:
            return False  # 0 / negative => indefinite until cleared
        return sim_seconds >= self.started_s + self.duration_s

    def to_dict(self) -> dict:
        return {
            "point_key": self.point_key,
            "kind": self.kind,
            "started_s": round(self.started_s, 1),
            "duration_s": self.duration_s,
            "params": dict(self.params),
        }


class FaultInjector:
    """Overlays faults onto point values produced by the generators.

    One :class:`ActiveFault` may be active per ``point_key`` at a time; a new
    fault on the same key replaces the old one.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        self._active: dict[str, ActiveFault] = {}
        self._rng = rng or random.Random()

    # -- control ------------------------------------------------------------
    def inject(
        self,
        point_key: str,
        kind: str,
        duration_s: float | None = None,
        started_s: float = 0.0,
        **params: object,
    ) -> ActiveFault:
        """Start a fault on ``point_key``. Returns the created fault."""
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind: {kind!r}")
        if duration_s is None:
            duration_s = _DEFAULT_DURATION.get(kind, 60.0)
        fault = ActiveFault(
            point_key=point_key,
            kind=kind,
            started_s=float(started_s),
            duration_s=float(duration_s),
            params=dict(params),
        )
        self._active[point_key] = fault
        return fault

    def clear(self, point_key: str | None = None) -> int:
        """Clear one fault (by key) or all. Returns number cleared."""
        if point_key is None:
            n = len(self._active)
            self._active.clear()
            return n
        return 1 if self._active.pop(point_key, None) is not None else 0

    def is_offline(self, point_key: str) -> bool:
        f = self._active.get(point_key)
        return f is not None and f.kind == "offline"

    # -- per-tick application ----------------------------------------------
    def apply(self, point_key: str, value: object, ctx: TickContext) -> object:
        """Transform ``value`` if a fault is active on ``point_key``.

        Expires faults whose duration has elapsed (using ``ctx.sim_seconds``).
        Returns the (possibly modified) value. For non-numeric values only
        ``stuck``/``offline`` have an effect; other kinds pass through.
        """
        fault = self._active.get(point_key)
        if fault is None:
            return value
        if fault.expired(ctx.sim_seconds):
            del self._active[point_key]
            return value

        elapsed = max(0.0, ctx.sim_seconds - fault.started_s)
        kind = fault.kind

        if kind == "offline":
            # Engine skips writes for offline points; keep last/captured value.
            if fault.frozen_value is None:
                fault.frozen_value = value
            return fault.frozen_value

        if kind == "stuck":
            if fault.frozen_value is None:
                fault.frozen_value = value
            return fault.frozen_value

        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return value

        base = float(value)

        if kind == "spike":
            offset = float(fault.params.get("offset", fault.params.get("magnitude", 50.0)))
            return base + offset

        if kind == "drift":
            rate = fault.params.get("rate")
            if rate is not None:
                # absolute units per simulated second
                return base + float(rate) * elapsed
            # else grow linearly to ``magnitude`` over the duration
            magnitude = float(fault.params.get("magnitude", 20.0))
            frac = elapsed / fault.duration_s if fault.duration_s > 0 else 1.0
            return base + magnitude * min(1.0, frac)

        if kind == "noise_burst":
            sigma = float(fault.params.get("sigma", fault.params.get("magnitude", 10.0)))
            return base + self._rng.gauss(0.0, sigma)

        return value

    # -- random injection ---------------------------------------------------
    def maybe_random(
        self,
        point_keys: list[str],
        ctx: TickContext,
        rate_per_hour: float,
        rng: random.Random,
    ) -> ActiveFault | None:
        """With probability derived from ``rate_per_hour`` and ``ctx.dt_sim``,
        start a random fault on a random point. Returns the fault if started."""
        if rate_per_hour <= 0 or not point_keys or ctx.dt_sim <= 0:
            return None
        # Expected faults this tick = rate_per_hour * (dt_sim / 3600).
        prob = rate_per_hour * (ctx.dt_sim / 3600.0)
        if prob <= 0:
            return None
        if rng.random() >= min(1.0, prob):
            return None
        point_key = rng.choice(point_keys)
        if point_key in self._active:
            return None  # already faulted; skip
        kind = rng.choice(FAULT_KINDS)
        return self.inject(point_key, kind, started_s=ctx.sim_seconds)

    # -- introspection ------------------------------------------------------
    def active(self) -> list[dict]:
        return [f.to_dict() for f in self._active.values()]

    def count(self) -> int:
        return len(self._active)
