from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from bacnet_lab.adapters.http.dependencies import get_container

router = APIRouter(tags=["metrics"])

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# (metric name, type, help, metrics()-key) — order is the exposition order.
_METRICS: list[tuple[str, str, str, str]] = [
    ("bacnet_sim_tick_total", "counter", "Total simulation ticks executed.", "tick_count"),
    ("bacnet_sim_writes_total", "counter", "Total point writes performed by the engine.", "writes_total"),
    ("bacnet_sim_generators", "gauge", "Number of active signal generators.", "generator_count"),
    ("bacnet_sim_running", "gauge", "Whether the simulation loop is running (1/0).", "running"),
    ("bacnet_sim_active_faults", "gauge", "Number of currently active faults.", "active_fault_count"),
    ("bacnet_sim_seconds", "gauge", "Cumulative simulated seconds.", "sim_seconds"),
]


def _fmt(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return repr(value)
    return str(value)


@router.get("/metrics")
async def metrics() -> Response:
    container = get_container()
    data = container.simulation_engine.metrics()
    lines: list[str] = []
    for name, mtype, help_text, key in _METRICS:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        lines.append(f"{name} {_fmt(data.get(key, 0))}")
    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type=_CONTENT_TYPE)
