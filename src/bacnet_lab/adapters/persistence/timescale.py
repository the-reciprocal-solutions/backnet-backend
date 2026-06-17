from __future__ import annotations

import json
import logging
from datetime import datetime

import asyncpg

from bacnet_lab.domain.models.device import Device
from bacnet_lab.domain.value_objects import PointValue
from bacnet_lab.ports.timeseries import TimeSeriesPort

logger = logging.getLogger(__name__)


# Narrow / long schema — new devices/points are ROWS, never new columns.
_SCHEMA = [
    "CREATE EXTENSION IF NOT EXISTS timescaledb",
    """
    CREATE TABLE IF NOT EXISTS device (
        device_id   BIGINT PRIMARY KEY,
        name        TEXT NOT NULL,
        description TEXT DEFAULT '',
        ip          TEXT DEFAULT '',
        port        INT DEFAULT 0,
        status      TEXT DEFAULT 'online',
        first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
        tags        JSONB DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS point (
        point_id        BIGSERIAL PRIMARY KEY,
        device_id       BIGINT NOT NULL REFERENCES device(device_id),
        object_type     TEXT NOT NULL,
        object_instance INT NOT NULL,
        object_name     TEXT NOT NULL,
        units           TEXT DEFAULT '',
        cov_increment   REAL DEFAULT 0,
        sim_model       TEXT DEFAULT '',
        value_kind      TEXT NOT NULL DEFAULT 'num',
        tags            JSONB DEFAULT '{}',
        UNIQUE (device_id, object_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS point_reading (
        time       TIMESTAMPTZ NOT NULL,
        point_id   BIGINT NOT NULL,
        value_num  DOUBLE PRECISION,
        value_bool BOOLEAN,
        value_text TEXT,
        quality    SMALLINT DEFAULT 0
    )
    """,
    "SELECT create_hypertable('point_reading', 'time', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE)",
    "CREATE INDEX IF NOT EXISTS ix_reading_point_time ON point_reading (point_id, time DESC)",
    """
    CREATE TABLE IF NOT EXISTS event_log (
        time       TIMESTAMPTZ NOT NULL,
        event_type TEXT NOT NULL,
        device_id  BIGINT,
        point_id   BIGINT,
        severity   TEXT,
        payload    JSONB NOT NULL
    )
    """,
    "SELECT create_hypertable('event_log', 'time', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE)",
    "CREATE INDEX IF NOT EXISTS ix_event_type_time ON event_log (event_type, time DESC)",
]


def _cagg(name: str, bucket: str) -> str:
    # Continuous aggregate. Numeric stats + last value of each typed column.
    # WITH NO DATA so creation is instant; the refresh policy backfills.
    return f"""
    CREATE MATERIALIZED VIEW IF NOT EXISTS {name}
    WITH (timescaledb.continuous) AS
    SELECT point_id,
           time_bucket(INTERVAL '{bucket}', time) AS bucket,
           avg(value_num)  AS avg,
           min(value_num)  AS min,
           max(value_num)  AS max,
           last(value_num, time)  AS last_num,
           last(value_bool, time) AS last_bool,
           count(*) AS n
    FROM point_reading
    GROUP BY point_id, bucket
    WITH NO DATA
    """


# P2: rollups, refresh policies, retention, compression. Each runs as its own
# statement (continuous aggregates cannot run inside a transaction) and is
# idempotent (IF NOT EXISTS / if_not_exists), so connect() can re-run safely.
_AGGREGATES = [
    _cagg("point_reading_1m", "1 minute"),
    _cagg("point_reading_15m", "15 minutes"),
    _cagg("point_reading_1h", "1 hour"),
    "SELECT add_continuous_aggregate_policy('point_reading_1m',  start_offset => INTERVAL '2 hours', end_offset => INTERVAL '1 minute',  schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE)",
    "SELECT add_continuous_aggregate_policy('point_reading_15m', start_offset => INTERVAL '1 day',   end_offset => INTERVAL '15 minutes', schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE)",
    "SELECT add_continuous_aggregate_policy('point_reading_1h',  start_offset => INTERVAL '7 days',  end_offset => INTERVAL '1 hour',     schedule_interval => INTERVAL '30 minutes', if_not_exists => TRUE)",
    "SELECT add_retention_policy('point_reading', INTERVAL '90 days', if_not_exists => TRUE)",
    "SELECT add_retention_policy('event_log',     INTERVAL '180 days', if_not_exists => TRUE)",
    "ALTER TABLE point_reading SET (timescaledb.compress, timescaledb.compress_segmentby = 'point_id', timescaledb.compress_orderby = 'time DESC')",
    "SELECT add_compression_policy('point_reading', INTERVAL '7 days', if_not_exists => TRUE)",
    # Read-time pivot: latest value per point as a JSONB object per device.
    # "Columns per device" for the UI with ZERO DDL when points change.
    """
    CREATE OR REPLACE VIEW device_latest AS
    SELECT d.device_id, d.name,
           jsonb_object_agg(p.object_name,
               coalesce(to_jsonb(lr.value_num), to_jsonb(lr.value_bool), to_jsonb(lr.value_text))) AS points
    FROM device d
    JOIN point p USING (device_id)
    JOIN LATERAL (
        SELECT value_num, value_bool, value_text
        FROM point_reading WHERE point_id = p.point_id
        ORDER BY time DESC LIMIT 1
    ) lr ON true
    GROUP BY d.device_id, d.name
    """,
]

# resolution -> source relation for history queries
_RES_TABLE = {
    "raw": "point_reading",
    "1m": "point_reading_1m",
    "15m": "point_reading_15m",
    "1h": "point_reading_1h",
}


def _value_kind(value: PointValue) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    return "text"


def _split_value(value: PointValue) -> tuple[float | None, bool | None, str | None]:
    if isinstance(value, bool):
        return None, value, None
    if isinstance(value, (int, float)):
        return float(value), None, None
    return None, None, str(value)


class TimescaleTimeSeries(TimeSeriesPort):
    """TimescaleDB (asyncpg) implementation. Fail-safe: any error degrades to a
    log line and disables further writes rather than crashing the sim loop."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._ready = False
        # object_name -> point_id cache (resolved at register time)
        self._point_ids: dict[str, int] = {}

    @property
    def ready(self) -> bool:
        return self._ready and self._pool is not None

    async def connect(self) -> None:
        try:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            async with self._pool.acquire() as conn:
                for stmt in _SCHEMA:
                    await conn.execute(stmt)
                # P2 rollups + policies: best-effort, each independent. A failure
                # (e.g. policy already present, or non-Timescale Postgres) must
                # not block base time-series writes.
                for stmt in _AGGREGATES:
                    try:
                        await conn.execute(stmt)
                    except Exception as e:
                        logger.warning("TimescaleDB aggregate/policy skipped: %s", str(e).split(chr(10))[0])
            self._ready = True
            logger.info("TimescaleDB connected and schema ready")
        except Exception as e:
            logger.error("TimescaleDB unavailable, time-series disabled: %s", e)
            self._ready = False

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
        self._ready = False

    async def register_devices(self, devices: list[Device]) -> None:
        if not self.ready:
            return
        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    for d in devices:
                        ip = d.address.ip if d.address else ""
                        port = d.address.port if d.address else 0
                        await conn.execute(
                            """
                            INSERT INTO device (device_id, name, description, ip, port, status, last_seen)
                            VALUES ($1,$2,$3,$4,$5,$6, now())
                            ON CONFLICT (device_id) DO UPDATE
                              SET name=EXCLUDED.name, description=EXCLUDED.description,
                                  ip=EXCLUDED.ip, port=EXCLUDED.port,
                                  status=EXCLUDED.status, last_seen=now()
                            """,
                            d.device_id, d.name, d.description, ip, port, d.status.value,
                        )
                        for p in d.points:
                            sim_model = (p.simulation or {}).get("model", "") if p.simulation else ""
                            row = await conn.fetchrow(
                                """
                                INSERT INTO point (device_id, object_type, object_instance,
                                    object_name, units, cov_increment, sim_model, value_kind)
                                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                                ON CONFLICT (device_id, object_name) DO UPDATE
                                  SET units=EXCLUDED.units, sim_model=EXCLUDED.sim_model,
                                      value_kind=EXCLUDED.value_kind
                                RETURNING point_id
                                """,
                                d.device_id, p.object_type.value, p.object_instance,
                                p.object_name, p.units, p.cov_increment, sim_model,
                                _value_kind(p.present_value),
                            )
                            self._point_ids[p.object_name] = row["point_id"]
            logger.info("TimescaleDB registered %d devices (%d points cached)",
                        len(devices), len(self._point_ids))
        except Exception as e:
            logger.error("TimescaleDB register_devices failed: %s", e)

    async def write_readings(
        self, rows: list[tuple[datetime, int, str, PointValue]]
    ) -> None:
        if not self.ready or not rows:
            return
        records = []
        for ts, _device_id, object_name, value in rows:
            pid = self._point_ids.get(object_name)
            if pid is None:
                continue  # unregistered point; skip (registrar runs on metadata change)
            num, bln, txt = _split_value(value)
            records.append((ts, pid, num, bln, txt, 0))
        if not records:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.copy_records_to_table(
                    "point_reading",
                    records=records,
                    columns=["time", "point_id", "value_num", "value_bool", "value_text", "quality"],
                )
        except Exception as e:
            logger.error("TimescaleDB write_readings failed: %s", e)

    async def write_event(
        self,
        time: datetime,
        event_type: str,
        device_id: int | None,
        point_name: str | None,
        severity: str | None,
        payload: dict,
    ) -> None:
        if not self.ready:
            return
        pid = self._point_ids.get(point_name) if point_name else None
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO event_log (time, event_type, device_id, point_id, severity, payload)
                    VALUES ($1,$2,$3,$4,$5,$6)
                    """,
                    time, event_type, device_id, pid, severity, json.dumps(payload, default=str),
                )
        except Exception as e:
            logger.error("TimescaleDB write_event failed: %s", e)

    # -- reads (P3 history API) --------------------------------------------
    async def query_history(
        self,
        object_name: str,
        frm: datetime,
        to: datetime,
        resolution: str = "raw",
        limit: int = 5000,
    ) -> list[dict]:
        """Return a time series for one point. resolution: raw|1m|15m|1h."""
        if not self.ready:
            return []
        table = _RES_TABLE.get(resolution, "point_reading")
        try:
            async with self._pool.acquire() as conn:
                if table == "point_reading":
                    rows = await conn.fetch(
                        """
                        SELECT pr.time AS t, pr.value_num, pr.value_bool, pr.value_text
                        FROM point_reading pr JOIN point p USING (point_id)
                        WHERE p.object_name = $1 AND pr.time BETWEEN $2 AND $3
                        ORDER BY pr.time ASC LIMIT $4
                        """,
                        object_name, frm, to, limit,
                    )
                    return [
                        {"time": r["t"].isoformat(),
                         "value": r["value_num"] if r["value_num"] is not None
                                  else (r["value_bool"] if r["value_bool"] is not None else r["value_text"])}
                        for r in rows
                    ]
                rows = await conn.fetch(
                    f"""
                    SELECT a.bucket AS t, a.avg, a.min, a.max, a.last_num, a.n
                    FROM {table} a JOIN point p USING (point_id)
                    WHERE p.object_name = $1 AND a.bucket BETWEEN $2 AND $3
                    ORDER BY a.bucket ASC LIMIT $4
                    """,
                    object_name, frm, to, limit,
                )
                return [
                    {"time": r["t"].isoformat(), "avg": r["avg"], "min": r["min"],
                     "max": r["max"], "last": r["last_num"], "n": r["n"]}
                    for r in rows
                ]
        except Exception as e:
            logger.error("TimescaleDB query_history failed: %s", e)
            return []

    async def device_latest(self) -> list[dict]:
        """Pivot view: latest value of every point per device (JSONB)."""
        if not self.ready:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("SELECT device_id, name, points FROM device_latest ORDER BY device_id")
                return [
                    {"device_id": r["device_id"], "name": r["name"],
                     "points": json.loads(r["points"]) if isinstance(r["points"], str) else r["points"]}
                    for r in rows
                ]
        except Exception as e:
            logger.error("TimescaleDB device_latest failed: %s", e)
            return []
