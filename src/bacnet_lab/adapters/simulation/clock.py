from __future__ import annotations


class SimulationClock:
    """Tracks simulated time, decoupled from wall-clock.

    Each real tick advances sim time by ``dt_real * speed``. With speed=60 a
    24h cycle compresses into 24 real minutes; with speed=1 it is real-time.
    """

    def __init__(self, speed: float = 1.0) -> None:
        self.speed = max(0.0, float(speed))
        self._sim_seconds = 0.0

    def advance(self, dt_real: float) -> float:
        """Advance sim time by dt_real (wall seconds) * speed. Returns sim dt."""
        dt_sim = dt_real * self.speed
        self._sim_seconds += dt_sim
        return dt_sim

    @property
    def sim_seconds(self) -> float:
        return self._sim_seconds

    @property
    def hour_of_day(self) -> float:
        """Sim hour in [0, 24)."""
        return (self._sim_seconds % 86400.0) / 3600.0

    @property
    def day_phase(self) -> float:
        """Fraction of the current day in [0, 1)."""
        return (self._sim_seconds % 86400.0) / 86400.0
