from __future__ import annotations

import math

from bacnet_lab.adapters.simulation.generators.base import SignalGenerator, TickContext, clamp
from bacnet_lab.adapters.simulation.generators.dynamic import resolve_ref
from bacnet_lab.adapters.simulation.registry import register
from bacnet_lab.domain.value_objects import PointValue

# Safe functions exposed to the expression namespace.
_SAFE_FUNCS = {
    "min": min,
    "max": max,
    "abs": abs,
    "sin": math.sin,
    "cos": math.cos,
    "sqrt": math.sqrt,
    "exp": math.exp,
    "pi": math.pi,
}


@register("derived")
class DerivedGenerator(SignalGenerator):
    """Value computed from other points via a safe arithmetic expression.

    Config:
      - ``sources``  dict of ``alias -> point-reference-key`` (full
        ``"<device_id>/<object_name>"`` or bare name)
      - ``expr``     arithmetic expression string using the aliases, e.g.
        ``"supply + 0.5*(return - supply)"``. Safe math helpers are available:
        ``min, max, abs, sin, cos, sqrt, exp, pi``.

    Evaluated with ``eval(expr, {"__builtins__": {}}, namespace)`` where the
    namespace is the resolved source values plus the safe helpers. On any error
    or missing source, the last good value is kept unchanged.
    """

    def next(self, ctx: TickContext) -> PointValue:
        expr = self.config.get("expr")
        sources = self.config.get("sources") or {}
        if not expr:
            return round(clamp(float(self.value or 0.0), self.bounds), 3)

        namespace = dict(_SAFE_FUNCS)
        for alias, ref in sources.items():
            resolved = resolve_ref(ctx.values, ref)
            if resolved is None:
                # Missing source: keep last good value.
                return round(clamp(float(self.value or 0.0), self.bounds), 3)
            try:
                namespace[alias] = float(resolved)
            except (TypeError, ValueError):
                return round(clamp(float(self.value or 0.0), self.bounds), 3)

        try:
            result = float(eval(expr, {"__builtins__": {}}, namespace))
        except Exception:
            return round(clamp(float(self.value or 0.0), self.bounds), 3)

        self.value = clamp(result, self.bounds)
        return round(self.value, 3)
