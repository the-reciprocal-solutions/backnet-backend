"""Verify the forecasting model end-to-end against the real TimescaleDB series.

Backtest: pull a real point's series from the DB, hold out the last H points,
forecast from the truncated context, then score the forecast against the actual
held-out values. Prints WHICH model ran (naive vs chronos) plus accuracy and
prediction-interval coverage — so "the model works with the DB" is measurable,
not assumed.

Run inside the app container (has asyncpg + reaches TimescaleDB):

    docker compose exec -T bacnet-lab python - < scripts/verify_forecast.py
"""

from __future__ import annotations

import asyncio
import os

from bacnet_lab.forecasting.chronos_model import ChronosForecaster
from bacnet_lab.forecasting.db import ForecastDB

DSN = os.getenv("BACNET_LAB_TSDB_DSN", "postgres://bacnet:bacnet@localhost:5544/bacnet")
POINT = os.getenv("VERIFY_POINT", "AHU-01/SupplyAirTemp")
RES = os.getenv("VERIFY_RES", "1m")
HORIZON = int(os.getenv("VERIFY_HORIZON", "12"))
LOOKBACK_S = int(os.getenv("VERIFY_LOOKBACK_S", str(6 * 3600)))


def _mae(pred: list[float], actual: list[float]) -> float:
    return sum(abs(p - a) for p, a in zip(pred, actual)) / max(1, len(actual))


async def main() -> None:
    db = ForecastDB(DSN)
    await db.connect()
    if not db.ready:
        print("FAIL: ForecastDB not connected to TimescaleDB")
        return

    times, values = await db.fetch_series(POINT, LOOKBACK_S, RES)
    print(f"DB series: point={POINT} res={RES} points={len(values)}")
    if len(values) < HORIZON + 10:
        print(f"FAIL: need >= {HORIZON + 10} points, got {len(values)} — let it run longer")
        await db.close()
        return

    context = values[:-HORIZON]
    actual = values[-HORIZON:]

    model = ChronosForecaster()
    print(f"chronos_available={model.available()}  model_name={model.model_name}")

    fc = model.forecast(context, HORIZON)
    p10, p50, p90 = fc["p10"], fc["p50"], fc["p90"]

    mae = _mae(p50, actual)
    persistence = _mae([context[-1]] * HORIZON, actual)  # naive baseline to beat
    coverage = sum(1 for a, lo, hi in zip(actual, p10, p90) if lo <= a <= hi) / len(actual)

    print(f"--- backtest (held out last {HORIZON}) ---")
    print(f"model_ran        : {fc['model']}")
    print(f"MAE (p50)        : {mae:.4f}")
    print(f"persistence MAE  : {persistence:.4f}   (model should be <= this)")
    print(f"P10-P90 coverage : {coverage:.0%}      (want ~80%)")
    print(f"sample p50[:3]   : {[round(x, 3) for x in p50[:3]]}")
    print(f"sample actual[:3]: {[round(x, 3) for x in actual[:3]]}")
    verdict = "PASS" if mae <= persistence * 1.5 and coverage >= 0.5 else "WEAK"
    print(f"VERDICT          : {verdict}")
    await db.close()


asyncio.run(main())
