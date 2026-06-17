from __future__ import annotations

import math

from bacnet_lab.adapters.simulation.generators.base import SignalGenerator, TickContext, clamp
from bacnet_lab.adapters.simulation.registry import register
from bacnet_lab.domain.value_objects import PointValue


def resolve_ref(values: dict[str, PointValue], ref: str | None) -> PointValue | None:
    """Resolve a point reference from ``ctx.values``.

    Value-dict keys are ``"<device_id>/<object_name>"`` and the object_name
    itself may contain a ``/`` (e.g. ``"1001/AHU-01/SupplyAirTemp"``). A ref
    may be given three ways, tried in order of specificity:
      1. the full key verbatim (``"1001/AHU-01/SupplyAirTemp"``)
      2. the object_name, i.e. key minus the device-id prefix (``"AHU-01/SupplyAirTemp"``)
      3. a bare trailing segment (``"SupplyAirTemp"``)
    Returns ``None`` when the reference is missing so callers can degrade
    gracefully (keep their last value).
    """
    if not ref:
        return None
    if ref in values:
        return values[ref]
    # match by object_name (strip the leading "<device_id>/")
    for key, val in values.items():
        if "/" in key and key.split("/", 1)[1] == ref:
            return val
    # match by bare trailing segment
    for key, val in values.items():
        if key.rsplit("/", 1)[-1] == ref:
            return val
    return None


@register("first_order_lag")
class FirstOrderLagGenerator(SignalGenerator):
    """Exponential approach toward a target (RC / thermal lag).

    Config:
      - ``target``        fixed target value (float), OR
      - ``target_point``  a point reference key into ``ctx.values``
      - ``tau_s``         time constant in seconds (default 60)

    Each tick: ``value += (target - value) * (1 - exp(-dt_sim / tau_s))``.
    The internal value persists across ticks (init from ``point.present_value``).
    """

    def __init__(self, point, config, rng) -> None:
        super().__init__(point, config, rng)
        self._value = float(point.present_value or 0.0)
        self._tau_s = max(1e-9, float(self.config.get("tau_s", 60.0)))

    def _resolve_target(self, ctx: TickContext) -> float | None:
        target_point = self.config.get("target_point")
        if target_point is not None:
            ref = resolve_ref(ctx.values, target_point)
            if ref is None:
                return None
            try:
                return float(ref)
            except (TypeError, ValueError):
                return None
        if "target" in self.config:
            return float(self.config["target"])
        return None

    def next(self, ctx: TickContext) -> PointValue:
        target = self._resolve_target(ctx)
        if target is None:
            # Referenced target absent: keep last value.
            return round(self._finish(self._value, ctx), 3)
        alpha = 1.0 - math.exp(-ctx.dt_sim / self._tau_s)
        self._value += (target - self._value) * alpha
        return round(self._finish(self._value, ctx), 3)


@register("ramp")
class RampGenerator(SignalGenerator):
    """Linear change toward ``target`` at ``rate`` (units per second).

    Config:
      - ``target``  destination value (float, default current value)
      - ``rate``    units per second (default 1.0)
      - ``hold``    when True, stay at target once reached (default True)

    The internal value persists across ticks.
    """

    def __init__(self, point, config, rng) -> None:
        super().__init__(point, config, rng)
        self._value = float(point.present_value or 0.0)
        self._rate = float(self.config.get("rate", 1.0))
        self._hold = bool(self.config.get("hold", True))

    def next(self, ctx: TickContext) -> PointValue:
        target = float(self.config.get("target", self._value))
        step = abs(self._rate) * ctx.dt_sim
        delta = target - self._value
        if abs(delta) <= step:
            self._value = target if self._hold else self._value + math.copysign(step, delta)
        else:
            self._value += math.copysign(step, delta)
        return round(self._finish(self._value, ctx), 3)


@register("step")
@register("square")
class StepGenerator(SignalGenerator):
    """Square-wave duty cycle.

    Config:
      - ``low``       value during the off portion (default 0)
      - ``high``      value during the on portion (default 1)
      - ``period_s``  full cycle length in seconds (default 60)
      - ``duty``      fraction of each period spent high, 0..1 (default 0.5)

    Output is ``high`` during the leading ``duty`` fraction of each period and
    ``low`` otherwise, driven by ``ctx.sim_seconds``.
    """

    def next(self, ctx: TickContext) -> PointValue:
        low = float(self.config.get("low", 0.0))
        high = float(self.config.get("high", 1.0))
        period_s = max(1e-9, float(self.config.get("period_s", 60.0)))
        duty = clamp(float(self.config.get("duty", 0.5)), [0.0, 1.0])
        phase = (ctx.sim_seconds % period_s) / period_s
        val = high if phase < duty else low
        return round(self._finish(val, ctx), 3)


@register("pid_actuator")
class PidActuatorGenerator(SignalGenerator):
    """Closed-loop PID output (0..100 typical) driving a process variable.

    Config:
      - ``process_var``  reference key into ``ctx.values`` (the measured PV)
      - ``setpoint``     fixed setpoint (float) OR a reference key into values
      - ``kp``           proportional gain (default 5)
      - ``ki``           integral gain (default 0.1)
      - ``kd``           derivative gain (default 0)
      - ``reverse``      flip error sign (cooling: open as PV rises) (default False)
      - ``bounds``       output clamp (default [0, 100])

    Maintains integral + last-error state with integral anti-windup (the
    integral term is clamped to the output bounds). Degrades gracefully (keeps
    last output) when the process variable is absent.
    """

    def __init__(self, point, config, rng) -> None:
        super().__init__(point, config, rng)
        if self.bounds is None:
            self.bounds = [0.0, 100.0]
        self._output = float(point.present_value or 0.0)
        self._integral = 0.0
        self._last_error: float | None = None
        self._kp = float(self.config.get("kp", 5.0))
        self._ki = float(self.config.get("ki", 0.1))
        self._kd = float(self.config.get("kd", 0.0))
        self._reverse = bool(self.config.get("reverse", False))

    def _resolve_setpoint(self, ctx: TickContext) -> float | None:
        sp = self.config.get("setpoint")
        if isinstance(sp, str):
            ref = resolve_ref(ctx.values, sp)
            if ref is None:
                return None
            try:
                return float(ref)
            except (TypeError, ValueError):
                return None
        if sp is None:
            return None
        return float(sp)

    def next(self, ctx: TickContext) -> PointValue:
        pv_ref = resolve_ref(ctx.values, self.config.get("process_var"))
        setpoint = self._resolve_setpoint(ctx)
        if pv_ref is None or setpoint is None:
            # Missing PV or setpoint: hold last output.
            return round(self._output, 3)
        try:
            pv = float(pv_ref)
        except (TypeError, ValueError):
            return round(self._output, 3)

        error = (pv - setpoint) if self._reverse else (setpoint - pv)

        # Integral with anti-windup (clamp accumulated integral to bounds).
        if ctx.dt_sim > 0:
            self._integral += error * ctx.dt_sim
        self._integral = clamp(self._integral, self.bounds)

        derivative = 0.0
        if self._last_error is not None and ctx.dt_sim > 0:
            derivative = (error - self._last_error) / ctx.dt_sim
        self._last_error = error

        raw = self._kp * error + self._ki * self._integral + self._kd * derivative
        self._output = clamp(raw, self.bounds)
        return round(self._output, 3)
