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

_SYSTEM_JSON = (
    "You are a building-automation (BMS) diagnostics assistant. You are given a "
    "Chronos forecast for one BACnet point and a set of MEASURED evidence numbers "
    "(recent changes in related points and recent events). Using ONLY these "
    "numbers, respond in a JSON object with the following keys:\n"
    "1. \"explanation\": A brief narration in exactly this format:\n"
    "   Prediction: <point> ≈ <p50><units> in <horizon> (range <p10>–<p90>)\n"
    "   Reason:\n"
    "   - <driver point>: <old>→<new> (<delta><units>)\n"
    "   - ...\n"
    "   (Cite largest driver changes first; do NOT invent values; keep it under 6 lines).\n"
    "2. \"root_cause\": A string explaining the most probable root cause of the anomaly based on the evidence.\n"
    "3. \"contributing_factors\": A JSON array of strings citing specific contributing evidence/measurements.\n"
    "4. \"recommended_action\": A string suggesting a concrete physical inspection or maintenance action.\n"
    "5. \"confidence\": A JSON number between 0.0 and 1.0 representing your diagnostic confidence."
)


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
    root_cause: str | None = None
    contributing_factors: list[str] | None = None
    recommended_action: str | None = None
    confidence: float | None = None


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
        root_cause = None
        contributing_factors = None
        recommended_action = None
        confidence = None
        grounded = True

        if self._llm_settings.enabled:
            user = (
                f"Point: {object_name} (units: {units or 'n/a'})\n"
                f"Evidence JSON:\n{json.dumps(evidence, default=str)}"
            )
            raw_answer = await self._llm.chat(_SYSTEM_JSON, user, response_json=True)
            if raw_answer:
                try:
                    parsed = json.loads(raw_answer)
                    answer = parsed.get("explanation", "")
                    root_cause = parsed.get("root_cause")
                    contributing_factors = parsed.get("contributing_factors")
                    recommended_action = parsed.get("recommended_action")
                    confidence = parsed.get("confidence")
                    if isinstance(confidence, (int, float)):
                        confidence = round(float(confidence), 3)
                except Exception as parse_err:
                    logger.warning("Failed to parse JSON response from LLM: %s", parse_err)
                    answer = raw_answer

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
            root_cause = None
            contributing_factors = None
            recommended_action = None
            confidence = None

        return CopilotResult(
            object_name=object_name, predicted_value=_r(p50), units=units,
            horizon=horizon_label, forecast=evidence["forecast"], evidence=evidence,
            answer=answer, llm_model=self._llm.model, grounded=grounded,
            extras={"forecast_model": fr.model, "device_id": device_id},
            root_cause=root_cause,
            contributing_factors=contributing_factors,
            recommended_action=recommended_action,
            confidence=confidence,
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
