"""Deterministic reasoning-evidence extraction from TimescaleDB.

This is the GROUNDING layer: it computes real measured facts (deltas, driver
changes, recent events) that the LLM must explain. The LLM never invents these
numbers — it only narrates what this module returns. Own asyncpg pool, decoupled
from the app (consistent with the forecasting module).
"""

from __future__ import annotations

import logging
from datetime import timedelta

import asyncpg

logger = logging.getLogger(__name__)


class ReasoningDB:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        try:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
            logger.info("ReasoningDB connected")
        except Exception as e:
            logger.error("ReasoningDB connect failed: %s", e)
            self._pool = None

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def resolve_device(self, object_name: str) -> tuple[int, str] | None:
        """object_name -> (device_id, device name)."""
        if not self.ready:
            return None
        try:
            async with self._pool.acquire() as c:
                row = await c.fetchrow(
                    """
                    SELECT d.device_id, d.name FROM point p
                    JOIN device d USING (device_id)
                    WHERE p.object_name = $1 LIMIT 1
                    """,
                    object_name,
                )
                return (row["device_id"], row["name"]) if row else None
        except Exception as e:
            logger.error("resolve_device failed: %s", e)
            return None

    async def point_delta(self, object_name: str, window_s: int) -> dict | None:
        """Current value + change over the window for one point."""
        if not self.ready:
            return None
        try:
            async with self._pool.acquire() as c:
                rows = await c.fetch(
                    """
                    SELECT pr.time, pr.value_num, pr.value_bool, p.units
                    FROM point_reading pr JOIN point p USING (point_id)
                    WHERE p.object_name = $1 AND pr.time >= now() - $2::interval
                    ORDER BY pr.time ASC
                    """,
                    object_name, timedelta(seconds=window_s),
                )
            if not rows:
                return None
            first, last = rows[0], rows[-1]
            cur = last["value_num"] if last["value_num"] is not None else last["value_bool"]
            old = first["value_num"] if first["value_num"] is not None else first["value_bool"]
            delta = None
            if isinstance(cur, (int, float)) and isinstance(old, (int, float)):
                delta = round(float(cur) - float(old), 3)
            return {
                "point": object_name, "current": cur, "previous": old,
                "delta": delta, "units": last["units"], "samples": len(rows),
            }
        except Exception as e:
            logger.error("point_delta failed: %s", e)
            return None

    async def driver_deltas(
        self, device_id: int, window_s: int, exclude: str, top_k: int = 5
    ) -> list[dict]:
        """Per-point change over the window for all OTHER points on the device,
        ranked by absolute change — the candidate causal drivers."""
        if not self.ready:
            return []
        try:
            async with self._pool.acquire() as c:
                # first()/last() are TimescaleDB aggregates: value at the
                # earliest / latest timestamp within the group.
                rows = await c.fetch(
                    """
                    SELECT p.object_name, p.units,
                           first(pr.value_num, pr.time) AS old,
                           last(pr.value_num, pr.time)  AS new
                    FROM point_reading pr JOIN point p USING (point_id)
                    WHERE p.device_id = $2 AND p.object_name <> $3
                      AND pr.time >= now() - $1::interval
                      AND pr.value_num IS NOT NULL
                    GROUP BY p.object_name, p.units
                    """,
                    timedelta(seconds=window_s), device_id, exclude,
                )
            out = []
            for r in rows:
                old, new = r["old"], r["new"]
                if old is None or new is None:
                    continue
                delta = round(float(new) - float(old), 3)
                if abs(delta) < 1e-6:
                    continue
                out.append({
                    "point": r["object_name"], "old": round(float(old), 3),
                    "new": round(float(new), 3), "delta": delta, "units": r["units"],
                })
            out.sort(key=lambda x: abs(x["delta"]), reverse=True)
            return out[:top_k]
        except Exception as e:
            logger.error("driver_deltas failed: %s", e)
            return []

    async def recent_events(self, device_id: int, limit: int = 10) -> list[dict]:
        """Recent event_log rows for the device (alarms, status, faults)."""
        if not self.ready:
            return []
        try:
            async with self._pool.acquire() as c:
                rows = await c.fetch(
                    """
                    SELECT time, event_type, severity, payload
                    FROM event_log
                    WHERE device_id = $1 AND event_type <> 'point_value_changed'
                    ORDER BY time DESC LIMIT $2
                    """,
                    device_id, limit,
                )
            return [
                {"time": r["time"].isoformat(), "type": r["event_type"],
                 "severity": r["severity"]}
                for r in rows
            ]
        except Exception as e:
            logger.error("recent_events failed: %s", e)
            return []
