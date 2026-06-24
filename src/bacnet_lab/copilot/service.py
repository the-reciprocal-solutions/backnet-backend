"""Reasoning copilot: Chronos (what) + DB evidence (why) + LLM (narration).

The LLM is grounded — it receives ONLY measured numbers (forecast quantiles and
DB-computed deltas/events) and is instructed to explain using those numbers, not
to predict or speculate. This keeps explanations faithful to the data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from bacnet_lab.copilot.llm import LLMClient
from bacnet_lab.copilot.reasoning_db import ReasoningDB
from bacnet_lab.forecasting.service import ForecastService
from bacnet_lab.infrastructure.config import LLMSettings, TimescaleSettings

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a building-automation (BMS) diagnostics assistant. You are given a "
    "Chronos forecast for one BACnet point and a set of MEASURED evidence numbers "
    "(recent changes in related points and recent events). Using ONLY these "
    "numbers, respond in exactly this format:\n\n"
    "Prediction: <point> ≈ <p50><units> in <horizon> (range <p10>–<p90>)\n"
    "Reason:\n- <driver point>: <old>→<new> (<delta><units>)\n- ...\n\n"
    "Cite the largest driver changes first. Do NOT invent values, do NOT add "
    "drivers not in the evidence. If evidence is empty, say the value is expected "
    "to stay near its current reading. Keep it under 6 lines."
)

# Structured, machine-readable reasoning. Strict JSON, grounded in evidence only.
_SYSTEM_JSON = (
    "You are a building-automation (BMS) diagnostics assistant. You are given a "
    "Chronos forecast for one BACnet point and MEASURED evidence (recent changes "
    "in related driver points, the target point's own change, and recent events). "
    "Using ONLY these numbers, return STRICT JSON (no prose, no markdown) with "
    "exactly these keys:\n"
    '{\n'
    '  "root_cause": "<one short sentence naming the most likely cause, '
    'grounded in the evidence>",\n'
    '  "contributing_factors": ["<short factor citing a measured driver>", ...],\n'
    '  "recommended_action": "<one short actionable maintenance step>",\n'
    '  "confidence": "low" | "medium" | "high"\n'
    '}\n'
    "Cite only drivers present in the evidence. Do NOT invent values or points. "
    "If evidence is empty, set root_cause/recommended_action to null and "
    "contributing_factors to []. Keep each string under 140 chars."
)


@dataclass
class StructuredReasoning:
    root_cause: str | None = None
    contributing_factors: list[str] = field(default_factory=list)
    recommended_action: str | None = None
    confidence: str | None = None


@dataclass
class CopilotResult:
    object_name: str
    predicted_value: float | None
    units: str
    horizon: str
    forecast: dict
    evidence: dict
    answer: str
    llm_model: str
    grounded: bool = True
    extras: dict = field(default_factory=dict)
    # Deepened, structured reasoning (additive; safe defaults so nothing breaks).
    root_cause: str | None = None
    contributing_factors: list[str] = field(default_factory=list)
    recommended_action: str | None = None
    confidence: str | None = None


class CopilotService:
    def __init__(
        self,
        forecast_service: ForecastService,
        llm_settings: LLMSettings,
        ts_settings: TimescaleSettings,
    ) -> None:
        self._fc = forecast_service
        self._llm_settings = llm_settings
        self._db = ReasoningDB(ts_settings.dsn)
        self._llm = LLMClient(
            llm_settings.base_url, llm_settings.api_key,
            llm_settings.model, llm_settings.timeout_s,
        )

    async def start(self) -> None:
        if self._llm_settings.enabled:
            await self._db.connect()

    async def stop(self) -> None:
        await self._db.close()

    def info(self) -> dict:
        return {
            "enabled": self._llm_settings.enabled,
            "llm_model": self._llm.model,
            "base_url": self._llm_settings.base_url,
            "db_ready": self._db.ready,
        }

    async def explain(
        self, object_name: str, horizon: int = 6, resolution: str = "1m", window_s: int = 1800
    ) -> CopilotResult:
        # 1. WHAT — Chronos forecast.
        fr = await self._fc.forecast_point(
            object_name, lookback_s=max(window_s, 3600), resolution=resolution,
            horizon=horizon, store=False,
        )
        p50 = fr.p50[-1] if fr.p50 else None
        p10 = fr.p10[-1] if fr.p10 else None
        p90 = fr.p90[-1] if fr.p90 else None

        # 2. WHY — measured evidence from the DB.
        dev = await self._db.resolve_device(object_name)
        device_id = dev[0] if dev else None
        target = await self._db.point_delta(object_name, window_s)
        drivers = await self._db.driver_deltas(device_id, window_s, object_name) if device_id else []
        events = await self._db.recent_events(device_id) if device_id else []
        units = (target or {}).get("units", "")

        step_min = {"1m": 1, "15m": 15, "1h": 60, "raw": 0}.get(resolution, 1)
        horizon_label = f"{horizon * step_min} min" if step_min else f"{horizon} steps"

        evidence = {
            "target": target,
            "forecast": {"horizon": horizon_label, "p10": _r(p10), "p50": _r(p50), "p90": _r(p90)},
            "drivers": drivers,
            "events": events,
        }

        # 3. NARRATE — grounded LLM (skipped if disabled/unreachable).
        answer = ""
        grounded = True
        structured: StructuredReasoning | None = None
        if self._llm_settings.enabled:
            user = (
                f"Point: {object_name} (units: {units or 'n/a'})\n"
                f"Evidence JSON:\n{json.dumps(evidence, default=str)}"
            )
            answer = await self._llm.chat(_SYSTEM, user)
            # Ask for the structured, machine-readable reasoning separately so a
            # parse failure on one never corrupts the narration text.
            raw_json = await self._llm.chat(_SYSTEM_JSON, user, response_json=True)
            structured = _parse_structured(raw_json)
        if structured is None:
            # Deterministic, grounded fallback (LLM disabled or parse failed).
            structured = _fallback_structured(
                object_name, units, evidence, drivers, target
            )
        if not answer:
            # Deterministic fallback so the endpoint always returns something useful.
            grounded = True
            lines = [f"Prediction: {object_name} ≈ {_r(p50)}{units} in {horizon_label} "
                     f"(range {_r(p10)}–{_r(p90)})", "Reason:"]
            for d in drivers[:4]:
                lines.append(f"- {d['point']}: {d['old']}→{d['new']} ({d['delta']:+}{d.get('units','')})")
            if not drivers:
                lines.append("- no significant driver changes; value expected near current reading")
            answer = "\n".join(lines)

        return CopilotResult(
            object_name=object_name, predicted_value=_r(p50), units=units,
            horizon=horizon_label, forecast=evidence["forecast"], evidence=evidence,
            answer=answer, llm_model=self._llm.model, grounded=grounded,
            extras={"forecast_model": fr.model, "device_id": device_id},
            root_cause=structured.root_cause,
            contributing_factors=structured.contributing_factors,
            recommended_action=structured.recommended_action,
            confidence=structured.confidence,
        )

    async def ask(self, question: str, object_name: str | None = None) -> dict:
        """Free-form question; if a point is named, ground it with that point's evidence."""
        if not self._llm_settings.enabled:
            return {"answer": "", "error": "LLM disabled"}
        context = ""
        if object_name:
            res = await self.explain(object_name)
            context = f"\nGrounded evidence:\n{json.dumps(res.evidence, default=str)}"
        system = (
            "You are a BMS assistant. Answer using only the grounded evidence "
            "provided. If none is provided or it is insufficient, say so."
        )
        answer = await self._llm.chat(system, f"{question}{context}")
        return {"answer": answer, "llm_model": self._llm.model, "object_name": object_name}


def _r(v):
    return round(float(v), 3) if isinstance(v, (int, float)) else v


_CONFIDENCE_VALUES = {"low", "medium", "high"}


def _parse_structured(raw: str) -> StructuredReasoning | None:
    """Defensively parse the LLM's structured-reasoning JSON.

    Returns None on ANY problem (empty, non-JSON, wrong shape) so the caller
    falls back to the grounded deterministic reasoning. Tolerates a JSON object
    wrapped in stray prose/markdown by extracting the outermost braces.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    try:
        data = json.loads(text)
    except Exception:
        try:
            start, end = text.index("{"), text.rindex("}") + 1
            data = json.loads(text[start:end])
        except Exception:
            return None
    if not isinstance(data, dict):
        return None

    def _str_or_none(v):
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None

    factors_raw = data.get("contributing_factors")
    factors: list[str] = []
    if isinstance(factors_raw, list):
        factors = [str(f).strip() for f in factors_raw if str(f).strip()]

    conf = _str_or_none(data.get("confidence"))
    conf = conf.lower() if conf else None
    if conf not in _CONFIDENCE_VALUES:
        conf = None

    return StructuredReasoning(
        root_cause=_str_or_none(data.get("root_cause")),
        contributing_factors=factors,
        recommended_action=_str_or_none(data.get("recommended_action")),
        confidence=conf,
    )


def _fallback_structured(
    object_name: str, units: str, evidence: dict,
    drivers: list[dict], target: dict | None,
) -> StructuredReasoning:
    """Grounded deterministic reasoning — derived from MEASURED evidence only.

    No fabricated numbers: every cited figure comes from the driver/target
    deltas already computed from the DB.
    """
    factors: list[str] = []
    for d in drivers[:3]:
        du = d.get("units", "") or ""
        factors.append(
            f"{d['point']} changed {d['old']}→{d['new']} ({d['delta']:+}{du})"
        )

    # Root cause from the target's own breach direction, grounded in its delta.
    root_cause: str | None = None
    tdelta = (target or {}).get("delta")
    if isinstance(tdelta, (int, float)) and abs(tdelta) > 1e-6:
        direction = "rising" if tdelta > 0 else "falling"
        root_cause = (
            f"{object_name} is {direction} ({tdelta:+}{units}) over the window"
        )
        if drivers:
            top = drivers[0]
            root_cause += f"; most correlated with {top['point']}"
    elif drivers:
        top = drivers[0]
        root_cause = (
            f"{object_name} tracking change in {top['point']} "
            f"({top['delta']:+}{top.get('units', '') or ''})"
        )

    # Recommended action keyed to the point kind (no invented values).
    recommended_action: str | None = None
    if root_cause is not None:
        recommended_action = _recommend(object_name, drivers)

    # Confidence from how much measured signal we have.
    if drivers and target and isinstance(tdelta, (int, float)) and abs(tdelta) > 1e-6:
        confidence: str | None = "high"
    elif drivers or (isinstance(tdelta, (int, float)) and abs(tdelta) > 1e-6):
        confidence = "medium"
    elif target is not None:
        confidence = "low"
    else:
        confidence = None

    return StructuredReasoning(
        root_cause=root_cause,
        contributing_factors=factors,
        recommended_action=recommended_action,
        confidence=confidence,
    )


def _recommend(point_name: str, drivers: list[dict]) -> str:
    n = (point_name or "").lower()
    asset = point_name.split("/")[0] if "/" in point_name else point_name
    if "vibrat" in n:
        return f"Inspect {asset} bearing/mounts for imbalance or wear"
    if "temp" in n:
        return f"Check {asset} cooling/airflow and setpoints"
    if "press" in n:
        return f"Check {asset} filters/dampers for restriction"
    if drivers:
        return f"Review {drivers[0]['point']} and verify {asset} stays in band"
    return f"Monitor {asset} — value trending out of operating band"
