"""Predictive-failure engine (server-side).

Ports the frontend heuristic in ``src/services/predict.js`` to Python. Instead
of raising a noise alarm on every P10-P90 forecast wiggle, this asks: will a
point's FORECAST trajectory leave its normal operating envelope (toward a
breakdown/stop condition) within the horizon? Only those are surfaced, ranked
by time-to-failure.

Method:
* Operating envelope = mu +/- 4*sigma from recent history.
* Catastrophic hard limits by units override the envelope.
* First forecast step whose p50 crosses a bound -> ETA + severity.
* Confidence high when the p10/p90 envelope edge also crosses, else medium.

Also exposes per-asset health scoring and fleet KPIs.
"""

from __future__ import annotations

import logging
import re
import statistics
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Catastrophic hard limits by unit -- known stop/fail bounds, override envelope.
HARD_LIMITS: dict[str, dict[str, float | None]] = {
    "degreesCelsius": {"hi": 45.0, "lo": 0.0},  # coil overheat / freeze
    "amperes": {"hi": None, "lo": None},  # data-driven (overcurrent)
    "pascals": {"hi": None, "lo": None},  # clogged filter / duct
    "partsPerMillion": {"hi": 2000.0, "lo": None},  # ventilation failure (CO2)
    "percentRelativeHumidity": {"hi": 90.0, "lo": None},
    "kilowatts": {"hi": None, "lo": None},
    "volts": {"hi": None, "lo": None},
    "hertz": {"hi": None, "lo": None},
}

# Which points are worth scanning -- rotating/critical analog signals.
_CRIT_NAME = re.compile(
    r"(temp|current|amp|pressure|fan|speed|power|co2|humid|valve|flow|freq|volt|motor|bearing|vibrat)",
    re.IGNORECASE,
)
_SKIP_NAME = re.compile(r"(setpoint|enable|command|mode|status|dirty)", re.IGNORECASE)

# Severity weights for active alarms when scoring health.
_ALARM_WEIGHTS = {"critical": 40, "high": 25, "medium": 10, "low": 5}

_MIN_HISTORY = 8


def _round(v: float) -> float:
    if v is None:
        return v
    return v if float(v).is_integer() else round(float(v), 2)


def _is_analog(object_type: str | None) -> bool:
    return bool(object_type) and "analog" in str(object_type).lower()


def _is_critical(point_name: str, object_type: str | None, units: str) -> bool:
    if _SKIP_NAME.search(point_name or ""):
        return False
    if not _is_analog(object_type):
        return False
    return bool(_CRIT_NAME.search(point_name or "")) or units in HARD_LIMITS


class PredictionService:
    """Server-side predictive-failure + health + KPI engine."""

    def __init__(
        self,
        forecast_service,
        device_service,
        asset_service,
        alarm_service,
    ) -> None:
        self._forecast_service = forecast_service
        self._device_service = device_service
        self._asset_service = asset_service
        self._alarm_service = alarm_service

    # ------------------------------------------------------------------ #
    # Predictions
    # ------------------------------------------------------------------ #
    async def scan_predictions(
        self, horizon_steps: int = 15, resolution: str = "1m"
    ) -> list[dict]:
        """Scan all critical analog points for predicted envelope breaches."""
        out: list[dict] = []
        devices = self._device_service.get_all_in_memory_devices()
        for device in devices:
            for point in getattr(device, "points", []):
                try:
                    units = getattr(point, "units", "") or ""
                    object_type = getattr(point, "object_type", None)
                    object_type_str = getattr(object_type, "value", object_type)
                    if not _is_critical(point.object_name, object_type_str, units):
                        continue
                    result = await self._evaluate_point(device, point, units)
                    if result:
                        out.append(result)
                except Exception as e:  # one bad point must not kill the scan
                    logger.warning(
                        "Prediction scan failed for %s/%s: %s",
                        getattr(device, "name", "?"),
                        getattr(point, "object_name", "?"),
                        e,
                    )

        rank = {"critical": 0, "high": 1, "elevated": 2, "watch": 3}
        out.sort(
            key=lambda r: (
                rank.get(r["level"], 9),
                r["eta_minutes"] if r["eta_minutes"] is not None else 1_000_000_000,
            )
        )
        return out

    async def _evaluate_point(self, device, point, units: str) -> dict | None:
        # point.object_name already carries the device prefix (e.g. AHU-01/SupplyAirTemp)
        name = point.object_name

        times, values = await self._forecast_service.db.fetch_series(
            name, timedelta(seconds=3600), "1m"
        )
        vals = [float(v) for v in (values or []) if isinstance(v, (int, float))]
        if len(vals) < _MIN_HISTORY:
            return None

        rows = await self._forecast_service.db.latest_forecast(name)
        traj = self._latest_trajectory(rows)
        if not traj:
            return None

        m = statistics.fmean(vals)
        s = statistics.pstdev(vals) or abs(m) * 0.02 or 1.0

        hard = HARD_LIMITS.get(units, {}) if units else {}
        hi_candidates = [m + 4 * s]
        lo_candidates = [m - 4 * s]
        if hard.get("hi") is not None:
            hi_candidates.append(hard["hi"])
        if hard.get("lo") is not None:
            lo_candidates.append(hard["lo"])
        hi = min(hi_candidates)
        lo = max(lo_candidates)

        current = getattr(point, "present_value", None)
        current = float(current) if isinstance(current, (int, float)) else None

        now = datetime.now(timezone.utc)
        breach = None

        # Immediate breach: the live value is ALREADY beyond a failure bound.
        # (Chronos mean-reverts, so a current excursion may not show in p50.)
        if current is not None and (current >= hi or current <= lo):
            over_hi = current >= hi
            breach = {
                "eta": 0,
                "predicted": current,
                "bound": hi if over_hi else lo,
                "direction": "rising" if over_hi else "falling",
                "confidence": "high",
            }

        for f in traj if breach is None else []:
            t = f["horizon_ts"]
            if t is None:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t < now:
                continue
            p50 = f["p50"]
            if p50 is None:
                continue
            over_hi = p50 >= hi
            under_lo = p50 <= lo
            if over_hi or under_lo:
                p90 = f.get("p90")
                p10 = f.get("p10")
                if over_hi:
                    env_cross = p90 is not None and p90 >= hi
                else:
                    env_cross = p10 is not None and p10 <= lo
                eta = max(0, round((t - now).total_seconds() / 60.0))
                breach = {
                    "eta": eta,
                    "predicted": p50,
                    "bound": hi if over_hi else lo,
                    "direction": "rising" if over_hi else "falling",
                    "confidence": "high" if env_cross else "medium",
                }
                break

        if breach is None:
            return self._degradation_watch(
                name, device, point, units, traj, hi, lo, s, current
            )

        eta = breach["eta"]
        level = "critical" if eta <= 30 else "high" if eta <= 120 else "elevated"
        return {
            "point": name,
            "device_id": getattr(device, "device_id", None),
            "units": units,
            "current": _round(current) if current is not None else None,
            "predicted": _round(breach["predicted"]),
            "bound": _round(breach["bound"]),
            "direction": breach["direction"],
            "eta_minutes": eta,
            "confidence": breach["confidence"],
            "level": level,
            "reason": (
                f"Forecast {breach['direction']} to {_round(breach['predicted'])}"
                f"{self._unit(units)} crossing operating limit {_round(breach['bound'])}"
                f"{self._unit(units)} in ~{eta} min."
            ),
        }

    def _degradation_watch(
        self, name, device, point, units, traj, hi, lo, s, current
    ) -> dict | None:
        p50s = [f["p50"] for f in traj if f["p50"] is not None]
        if len(p50s) < 2:
            return None
        first, last = p50s[0], p50s[-1]
        slope = last - first
        margin = (hi - last) if slope > 0 else (last - lo)
        span = hi - lo
        if span <= 0:
            return None
        if abs(slope) > 0.5 * s and margin < 0.4 * span:
            return {
                "point": name,
                "device_id": getattr(device, "device_id", None),
                "units": units,
                "current": _round(current) if current is not None else None,
                "predicted": _round(last),
                "bound": _round(hi if slope > 0 else lo),
                "direction": "rising" if slope > 0 else "falling",
                "eta_minutes": None,
                "confidence": "low",
                "level": "watch",
                "reason": (
                    f"Trending {'up' if slope > 0 else 'down'} toward operating "
                    "limit; not yet projected to breach within horizon."
                ),
            }
        return None

    @staticmethod
    def _latest_trajectory(rows: list[dict]) -> list[dict]:
        """Keep only the most-recent forecast batch, sorted by horizon_ts."""
        if not rows:
            return []
        made_ats = [r.get("made_at") for r in rows if r.get("made_at") is not None]
        if made_ats:
            newest = max(made_ats)
            batch = [r for r in rows if r.get("made_at") == newest]
        else:
            batch = list(rows)
        batch.sort(key=lambda r: (r.get("horizon_ts") or datetime.min))
        return batch

    @staticmethod
    def _unit(u: str) -> str:
        return f" {u}" if u and u != "noUnits" else ""

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #
    async def asset_health(self, asset_id: str) -> dict | None:
        asset = await self._asset_service.get_asset(asset_id)
        if asset is None:
            return None

        alarms = await self._alarm_service.get_active_alarms()
        device_alarms = [a for a in alarms if a.device_id == asset.device_id]

        predictions = await self.scan_predictions()
        device_predictions = [
            p for p in predictions if p.get("device_id") == asset.device_id
        ]

        score = 100
        for a in device_alarms:
            sev = getattr(a.severity, "value", a.severity)
            score -= _ALARM_WEIGHTS.get(str(sev).lower(), 0)

        # 20 penalty per predicted failure (watches excluded -- they have no ETA).
        failures = [p for p in device_predictions if p.get("level") != "watch"]
        score -= 20 * len(failures)
        score = max(0, min(100, score))

        status = "Healthy" if score >= 80 else "Watch" if score >= 50 else "At-Risk"

        etas = [
            p["eta_minutes"]
            for p in device_predictions
            if p.get("eta_minutes") is not None
        ]
        rul = min(etas) if etas else None

        return {
            "asset_id": asset.id,
            "name": asset.name,
            "score": score,
            "status": status,
            "active_alarms": len(device_alarms),
            "predictions": device_predictions,
            "rul_minutes": rul,
        }

    # ------------------------------------------------------------------ #
    # Fleet KPI
    # ------------------------------------------------------------------ #
    async def fleet_kpi(self) -> dict:
        assets = await self._asset_service.list_assets()
        alarms = await self._alarm_service.get_active_alarms()
        predictions = await self.scan_predictions()

        by_level = {"critical": 0, "high": 0, "elevated": 0, "watch": 0}
        failures = 0
        preds_by_device: dict = {}
        for p in predictions:
            lvl = p.get("level", "")
            if lvl in by_level:
                by_level[lvl] += 1
            if lvl != "watch":
                failures += 1
            preds_by_device.setdefault(p.get("device_id"), []).append(p)

        alarms_by_device: dict = {}
        for a in alarms:
            alarms_by_device.setdefault(a.device_id, []).append(a)

        scores: list[int] = []
        at_risk = 0
        for asset in assets:
            score = 100
            for a in alarms_by_device.get(asset.device_id, []):
                sev = getattr(a.severity, "value", a.severity)
                score -= _ALARM_WEIGHTS.get(str(sev).lower(), 0)
            dev_failures = [
                p
                for p in preds_by_device.get(asset.device_id, [])
                if p.get("level") != "watch"
            ]
            score -= 20 * len(dev_failures)
            score = max(0, min(100, score))
            scores.append(score)
            if score < 50:
                at_risk += 1

        avg_health = round(statistics.fmean(scores)) if scores else 100

        return {
            "avg_health": avg_health,
            "assets_total": len(assets),
            "assets_at_risk": at_risk,
            "active_alarms": len(alarms),
            "predicted_failures": failures,
            "by_level": by_level,
        }
