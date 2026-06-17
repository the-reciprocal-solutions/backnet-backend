"""Direct, standalone TimescaleDB access for the forecasting module.

This layer is independent of the app's ``TimescaleTimeSeries`` adapter so the
model service can run on its own. It reads recent series from the existing
continuous aggregates (or raw hypertable) and persists forecasts into its own
``forecast`` table.

Every method is fail-safe: errors are logged and degrade to an empty result
rather than crashing the caller.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import asyncpg

logger = logging.getLogger(__name__)

# resolution -> (continuous aggregate / raw table, time column, value column)
_RESOLUTION_SOURCES: dict[str, tuple[str, str, str]] = {
    "1m": ("point_reading_1m", "bucket", "avg"),
    "15m": ("point_reading_15m", "bucket", "avg"),
    "1h": ("point_reading_1h", "bucket", "avg"),
    "raw": ("point_reading", "time", "value_num"),
}


class ForecastDB:
    """Standalone asyncpg access for fetching series and storing forecasts."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        try:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3)
            logger.info("ForecastDB connected")
        except Exception as e:
            logger.error("ForecastDB unavailable, forecasting DB disabled: %s", e)
            self._pool = None

    async def close(self) -> None:
        if self._pool:
            try:
                await self._pool.close()
            except Exception as e:  # pragma: no cover - best-effort
                logger.warning("ForecastDB close failed: %s", e)
            self._pool = None

    async def _resolve_point_id(
        self, conn: asyncpg.Connection, object_name: str
    ) -> int | None:
        row = await conn.fetchrow(
            "SELECT point_id FROM point WHERE object_name = $1 LIMIT 1",
            object_name,
        )
        return row["point_id"] if row else None

    async def fetch_series(
        self,
        object_name: str,
        lookback: timedelta | int,
        resolution: str = "1m",
    ) -> tuple[list[datetime], list[float]]:
        """Return an evenly-spaced recent numeric series for one point.

        ``lookback`` is a ``timedelta`` or an int number of seconds. The matching
        continuous aggregate is used for resolution in {'1m','15m','1h'};
        resolution 'raw' reads the raw hypertable. Returns ``([], [])`` on any
        failure or unknown point/resolution.
        """
        if not self.ready:
            return [], []
        source = _RESOLUTION_SOURCES.get(resolution)
        if source is None:
            logger.warning("ForecastDB unknown resolution %r", resolution)
            return [], []
        table, time_col, value_col = source
        if isinstance(lookback, timedelta):
            window = lookback
        else:
            window = timedelta(seconds=int(lookback))
        since = datetime.now().astimezone() - window
        try:
            async with self._pool.acquire() as conn:
                pid = await self._resolve_point_id(conn, object_name)
                if pid is None:
                    logger.warning("ForecastDB unknown point %r", object_name)
                    return [], []
                rows = await conn.fetch(
                    f"""
                    SELECT {time_col} AS ts, {value_col} AS val
                    FROM {table}
                    WHERE point_id = $1 AND {time_col} >= $2 AND {value_col} IS NOT NULL
                    ORDER BY {time_col} ASC
                    """,
                    pid,
                    since,
                )
            times = [r["ts"] for r in rows]
            values = [float(r["val"]) for r in rows]
            return times, values
        except Exception as e:
            logger.error("ForecastDB fetch_series failed for %r: %s", object_name, e)
            return [], []

    async def ensure_forecast_table(self) -> None:
        """Create the forecast table + index. Best-effort hypertable on made_at."""
        if not self.ready:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS forecast (
                        made_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                        point_id   BIGINT NOT NULL,
                        horizon_ts TIMESTAMPTZ NOT NULL,
                        p10 DOUBLE PRECISION, p50 DOUBLE PRECISION, p90 DOUBLE PRECISION,
                        model TEXT NOT NULL
                    )
                    """
                )
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS ix_forecast_point "
                    "ON forecast(point_id, horizon_ts DESC)"
                )
                # Optional: make it a hypertable on made_at if TimescaleDB exists.
                try:
                    await conn.execute(
                        "SELECT create_hypertable('forecast', 'made_at', "
                        "chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE)"
                    )
                except Exception as e:
                    logger.info(
                        "ForecastDB hypertable on forecast skipped (no TimescaleDB?): %s",
                        str(e).split(chr(10))[0],
                    )
            logger.info("ForecastDB forecast table ready")
        except Exception as e:
            logger.error("ForecastDB ensure_forecast_table failed: %s", e)

    async def store_forecast(
        self,
        object_name: str,
        made_at: datetime,
        horizons: list[datetime],
        p10: list[float],
        p50: list[float],
        p90: list[float],
        model: str,
    ) -> None:
        """Resolve point_id by object_name and insert one row per horizon step."""
        if not self.ready or not horizons:
            return
        try:
            async with self._pool.acquire() as conn:
                pid = await self._resolve_point_id(conn, object_name)
                if pid is None:
                    logger.warning(
                        "ForecastDB store_forecast: unknown point %r", object_name
                    )
                    return
                records = [
                    (
                        made_at,
                        pid,
                        horizons[i],
                        _at(p10, i),
                        _at(p50, i),
                        _at(p90, i),
                        model,
                    )
                    for i in range(len(horizons))
                ]
                await conn.executemany(
                    """
                    INSERT INTO forecast
                        (made_at, point_id, horizon_ts, p10, p50, p90, model)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    """,
                    records,
                )
        except Exception as e:
            logger.error("ForecastDB store_forecast failed for %r: %s", object_name, e)

    async def latest_forecast(
        self, object_name: str, limit: int = 12
    ) -> list[dict]:
        """Read back the most recent forecast rows for a point."""
        if not self.ready:
            return []
        try:
            async with self._pool.acquire() as conn:
                pid = await self._resolve_point_id(conn, object_name)
                if pid is None:
                    return []
                rows = await conn.fetch(
                    """
                    SELECT made_at, point_id, horizon_ts, p10, p50, p90, model
                    FROM forecast
                    WHERE point_id = $1
                    ORDER BY made_at DESC, horizon_ts ASC
                    LIMIT $2
                    """,
                    pid,
                    int(limit),
                )
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("ForecastDB latest_forecast failed for %r: %s", object_name, e)
            return []


def _at(seq: list[float], i: int) -> float | None:
    """Safe positional access; returns None when out of range."""
    try:
        return float(seq[i])
    except (IndexError, TypeError, ValueError):
        return None
