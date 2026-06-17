from __future__ import annotations

import asyncio
import logging
import random
import time

from bacnet_lab.adapters.simulation.clock import SimulationClock
from bacnet_lab.adapters.simulation.faults import FaultInjector
from bacnet_lab.adapters.simulation.generators.base import SignalGenerator, TickContext
from bacnet_lab.adapters.simulation.world import WorldModel

# Importing the generator modules registers their models in the registry.
from bacnet_lab.adapters.simulation import registry as sim_registry
from bacnet_lab.adapters.simulation.generators import basic as _basic  # noqa: F401
from bacnet_lab.application.device_service import DeviceService
from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Point
from bacnet_lab.infrastructure.config import SimulationSettings

logger = logging.getLogger(__name__)

# Optional generator modules (P1+). Import if present so their models register.
for _mod in ("dynamic", "derived"):
    try:  # pragma: no cover - optional
        __import__(f"bacnet_lab.adapters.simulation.generators.{_mod}")
    except Exception:
        pass


def _key(device_id: int, point_name: str) -> str:
    return f"{device_id}/{point_name}"


class SimulationEngine:
    """Always-on engine that drives every point through a signal generator.

    Writes go through DeviceService so the existing BACnet/COV/event/webhook
    pipeline is reused unchanged — real BACnet clients see live values.
    """

    def __init__(self, device_service: DeviceService, settings: SimulationSettings) -> None:
        self._ds = device_service
        self._settings = settings
        self._clock = SimulationClock(speed=settings.speed)
        self._rng = random.Random(settings.seed)
        self._faults = FaultInjector(random.Random(settings.seed))
        self._generators: dict[str, tuple[int, Point, SignalGenerator]] = {}
        self._values: dict[str, object] = {}
        self._task: asyncio.Task | None = None
        self._running = False
        self._tick_count = 0
        self._writes_total = 0
        self._world: WorldModel | None = None
        self._world_state: dict[str, float] = {}

    # -- lifecycle ----------------------------------------------------------
    def build(self) -> None:
        """Instantiate one generator per point from config or heuristic default."""
        self._generators.clear()
        self._values.clear()
        if self._settings.world_enabled:
            self._world = WorldModel(self._settings.weather, self._settings.occupancy)
        else:
            self._world = None
        self._world_state = {}
        for device in self._ds.get_all_in_memory_devices():
            for point in device.points:
                key = _key(device.device_id, point.object_name)
                self._values[key] = point.present_value
                model, cfg = self._resolve_model(point)
                if model is None:
                    continue  # static point, no generator
                # deterministic per-point RNG stream
                seed = None if self._settings.seed is None else hash((self._settings.seed, key)) & 0xFFFFFFFF
                rng = random.Random(seed)
                try:
                    gen = sim_registry.create_generator(model, point, cfg, rng)
                    self._generators[key] = (device.device_id, point, gen)
                except ValueError as e:
                    logger.warning("Skipping point %s: %s", key, e)
        logger.info(
            "Simulation built: %d generators over %d points",
            len(self._generators), len(self._values),
        )

    async def start(self) -> None:
        if not self._settings.enabled:
            logger.info("Simulation disabled (BACNET_LAB_SIM_ENABLED=false)")
            return
        if self._running:
            return
        self.build()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Simulation engine started (tick_hz=%s speed=%s seed=%s generators=%d)",
            self._settings.tick_hz, self._settings.speed, self._settings.seed,
            len(self._generators),
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Simulation engine stopped")

    # -- loop ---------------------------------------------------------------
    async def _loop(self) -> None:
        hz = max(0.01, float(self._settings.tick_hz))
        period = 1.0 / hz
        last = time.monotonic()
        try:
            while self._running:
                now = time.monotonic()
                dt_real = now - last
                last = now
                dt_sim = self._clock.advance(dt_real)
                # Advance the shared world model and merge its state into the
                # value dict (which ctx.values references) so generators can
                # read "world/<var>" keys this tick.
                if self._world is not None:
                    self._world_state = self._world.step(
                        sim_seconds=self._clock.sim_seconds,
                        hour_of_day=self._clock.hour_of_day,
                        day_phase=self._clock.day_phase,
                        dt_sim=dt_sim,
                        values=self._values,
                    )
                    for var, val in self._world_state.items():
                        self._values[f"world/{var}"] = val
                ctx = TickContext(
                    sim_seconds=self._clock.sim_seconds,
                    dt_sim=dt_sim,
                    hour_of_day=self._clock.hour_of_day,
                    day_phase=self._clock.day_phase,
                    noise_level=self._settings.noise_level,
                    values=self._values,
                )
                if self._settings.faults_enabled:
                    self._faults.maybe_random(
                        list(self._generators.keys()), ctx,
                        self._settings.fault_rate, self._rng,
                    )
                await self._tick(ctx)
                self._tick_count += 1
                await asyncio.sleep(period)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Simulation loop error: %s", e, exc_info=True)
            self._running = False

    async def _tick(self, ctx: TickContext) -> None:
        for key, (device_id, point, gen) in self._generators.items():
            try:
                new_val = gen.next(ctx)
            except Exception as e:
                logger.debug("Generator error on %s: %s", key, e)
                continue
            new_val = self._faults.apply(key, new_val, ctx)
            # Offline faults stop writing so the point reads stale.
            if self._faults.is_offline(key):
                continue
            old_val = self._values.get(key)
            if not self._changed(old_val, new_val):
                continue
            self._values[key] = new_val
            try:
                await self._ds.write_point_by_name(device_id, point.object_name, new_val)
                self._writes_total += 1
            except Exception as e:
                logger.debug("Write failed for %s: %s", key, e)

    @staticmethod
    def _changed(old, new) -> bool:
        if old is None:
            return True
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            return abs(float(old) - float(new)) > 1e-6
        return old != new

    # -- heuristic default model -------------------------------------------
    def _resolve_model(self, point: Point) -> tuple[str | None, dict]:
        """Explicit per-point config wins; else pick a sensible default by type/units."""
        if point.simulation and point.simulation.get("model"):
            cfg = dict(point.simulation)
            return cfg.pop("model"), cfg
        return self._default_model(point)

    def _default_model(self, point: Point) -> tuple[str | None, dict]:
        ot = point.object_type
        units = (point.units or "").lower()
        pv = point.present_value

        # Binary / multistate: slow occupancy-like cycle
        if ot in (PointType.BINARY_INPUT, PointType.BINARY_VALUE):
            return "multistate_cycle", {"states": [False, True], "period_s": 1800.0}
        if ot in (PointType.MULTI_STATE_INPUT, PointType.MULTI_STATE_VALUE):
            return "multistate_cycle", {"states": [1, 2, 3], "period_s": 1200.0}
        if ot in (PointType.BINARY_OUTPUT, PointType.MULTI_STATE_OUTPUT, PointType.ANALOG_OUTPUT):
            return None, {}  # commands/outputs stay stable by default

        # Analog inputs/values: random walk around current value, type-tuned
        if isinstance(pv, (int, float)):
            base = float(pv)
            if "celsius" in units or "degree" in units:
                return "random_walk", {"center": base, "step": 0.15, "mean_reversion": 0.05,
                                       "bounds": [base - 8, base + 8]}
            if "percent" in units:
                return "random_walk", {"center": base, "step": 0.8, "mean_reversion": 0.05,
                                       "bounds": [0, 100]}
            if "ppm" in units or "co2" in units:
                return "random_walk", {"center": base, "step": 8.0, "mean_reversion": 0.05,
                                       "bounds": [350, 2000]}
            if "pascal" in units:
                return "random_walk", {"center": base, "step": 3.0, "mean_reversion": 0.05,
                                       "bounds": [0, 600]}
            span = max(1.0, abs(base) * 0.05)
            return "random_walk", {"center": base, "step": span * 0.1, "mean_reversion": 0.05,
                                   "bounds": [base - span, base + span]}
        return None, {}

    # -- introspection (for API) -------------------------------------------
    def status(self) -> dict:
        return {
            "enabled": self._settings.enabled,
            "running": self._running,
            "tick_hz": self._settings.tick_hz,
            "speed": self._settings.speed,
            "seed": self._settings.seed,
            "noise_level": self._settings.noise_level,
            "tick_count": self._tick_count,
            "sim_seconds": round(self._clock.sim_seconds, 1),
            "sim_hour": round(self._clock.hour_of_day, 2),
            "generator_count": len(self._generators),
            "models": sim_registry.available_models(),
            "world_enabled": self._settings.world_enabled,
            "world_state": dict(self._world_state),
            "faults_enabled": self._settings.faults_enabled,
            "active_faults": self._faults.active(),
        }

    # -- faults / metrics (for API) ----------------------------------------
    def inject_fault(
        self, point_key: str, kind: str, duration_s: float | None = None, **params: object
    ) -> dict:
        self._faults.inject(
            point_key, kind, duration_s=duration_s,
            started_s=self._clock.sim_seconds, **params,
        )
        return {"active_faults": self._faults.active()}

    def clear_faults(self, point_key: str | None = None) -> dict:
        cleared = self._faults.clear(point_key)
        return {"cleared": cleared, "active_faults": self._faults.active()}

    def active_faults(self) -> list[dict]:
        return self._faults.active()

    def metrics(self) -> dict:
        return {
            "tick_count": self._tick_count,
            "generator_count": len(self._generators),
            "running": self._running,
            "sim_seconds": round(self._clock.sim_seconds, 1),
            "writes_total": self._writes_total,
            "active_fault_count": self._faults.count(),
        }

    def list_generators(self) -> list[dict]:
        out = []
        for key, (device_id, point, gen) in self._generators.items():
            out.append({
                "key": key,
                "device_id": device_id,
                "point_name": point.object_name,
                "object_type": point.object_type.value,
                "model": gen.model,
                "value": self._values.get(key),
            })
        return out
