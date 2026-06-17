"""Chronos zero-shot forecaster with a graceful naive fallback.

Heavy ML dependencies (``torch``, ``chronos-forecasting``) are OPTIONAL and
imported LAZILY inside methods, never at module top level. To enable the real
model:

    pip install chronos-forecasting torch

When those imports fail, :meth:`ChronosForecaster.forecast` transparently falls
back to a naive persistence + linear-drift forecaster whose p10/p90 band widens
with the horizon. The forecasting endpoint therefore works with no torch
installed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ChronosForecaster:
    """Wraps Chronos zero-shot forecasting; falls back to naive when unavailable."""

    def __init__(
        self,
        model_name: str = "amazon/chronos-bolt-small",
        device: str = "cpu",
    ) -> None:
        self.model_name = model_name
        self.device = device
        self._available: bool | None = None  # cached availability probe
        self._pipeline = None  # cached loaded model instance

    def available(self) -> bool:
        """True only if both ``torch`` and ``chronos`` import succeed (cached)."""
        if self._available is not None:
            return self._available
        try:
            import torch  # noqa: F401
            import chronos  # noqa: F401

            self._available = True
        except Exception as e:
            logger.info("Chronos/torch unavailable, using naive fallback: %s", e)
            self._available = False
        return self._available

    def _load_pipeline(self):
        """Lazily load + cache the Chronos pipeline. Returns None on failure."""
        if self._pipeline is not None:
            return self._pipeline
        try:
            import torch

            # BaseChronosPipeline is the universal loader — returns the right
            # pipeline for BOTH chronos-bolt (predict_quantiles) and classic
            # chronos-t5 (predict). Requires chronos-forecasting >= 1.4.
            # Fall back to the classic loader on very old installs.
            try:
                from chronos import BaseChronosPipeline  # type: ignore
                loader = BaseChronosPipeline
            except Exception:
                from chronos import ChronosPipeline  # type: ignore
                loader = ChronosPipeline

            dtype = torch.bfloat16 if self.device != "cpu" else torch.float32
            self._pipeline = loader.from_pretrained(
                self.model_name,
                device_map=self.device,
                dtype=dtype,
            )
            logger.info("Chronos pipeline loaded (%s): %s", loader.__name__, self.model_name)
        except Exception as e:
            logger.error("Chronos pipeline load failed, falling back to naive: %s", e)
            self._pipeline = None
            self._available = False
        return self._pipeline

    def forecast(
        self,
        values: list[float],
        horizon: int,
        quantiles: tuple[float, float, float] = (0.1, 0.5, 0.9),
    ) -> dict:
        """Forecast ``horizon`` steps ahead.

        Returns ``{"p10": [...], "p50": [...], "p90": [...], "model": <name>}``.
        Uses Chronos when available, otherwise a naive forecaster (model
        ``"naive"``).
        """
        horizon = max(int(horizon), 1)
        if self.available():
            result = self._forecast_chronos(values, horizon, quantiles)
            if result is not None:
                return result
        return self._forecast_naive(values, horizon)

    def _forecast_chronos(
        self,
        values: list[float],
        horizon: int,
        quantiles: tuple[float, float, float],
    ) -> dict | None:
        if not values:
            return None
        pipeline = self._load_pipeline()
        if pipeline is None:
            return None
        try:
            import torch

            context = torch.tensor([float(v) for v in values], dtype=torch.float32)
            q_levels = list(quantiles)

            # Bolt models expose predict_quantiles; classic models expose predict.
            if hasattr(pipeline, "predict_quantiles"):
                # inputs is the first POSITIONAL arg for both bolt + classic.
                q_tensor, _mean = pipeline.predict_quantiles(
                    context,
                    prediction_length=horizon,
                    quantile_levels=q_levels,
                )
                # shape: (num_series=1, horizon, num_quantiles)
                series = q_tensor[0]
                p10 = series[:, 0].tolist()
                p50 = series[:, 1].tolist()
                p90 = series[:, 2].tolist()
            else:
                forecast = pipeline.predict(context, prediction_length=horizon)
                # shape: (num_series=1, num_samples, horizon)
                samples = forecast[0]
                lo, mid, hi = q_levels
                p10 = torch.quantile(samples, lo, dim=0).tolist()
                p50 = torch.quantile(samples, mid, dim=0).tolist()
                p90 = torch.quantile(samples, hi, dim=0).tolist()

            return {
                "p10": [float(x) for x in p10],
                "p50": [float(x) for x in p50],
                "p90": [float(x) for x in p90],
                "model": self.model_name,
            }
        except Exception as e:
            logger.error("Chronos forecast failed, falling back to naive: %s", e)
            return None

    def _forecast_naive(self, values: list[float], horizon: int) -> dict:
        """Persistence + linear drift from the last K points; widening band.

        - Level: last observed value.
        - Drift: average step over the last K points (capped K).
        - Band: p10/p90 spread grows ~ sqrt(step) with a residual-based scale,
          so uncertainty widens further into the future.
        """
        if not values:
            zeros = [0.0] * horizon
            return {"p10": zeros, "p50": list(zeros), "p90": list(zeros), "model": "naive"}

        k = min(len(values), 12)
        recent = [float(v) for v in values[-k:]]
        last = recent[-1]

        # Linear drift = mean of consecutive deltas over the recent window.
        if len(recent) >= 2:
            deltas = [recent[i + 1] - recent[i] for i in range(len(recent) - 1)]
            drift = sum(deltas) / len(deltas)
        else:
            drift = 0.0

        # Scale of the band from recent volatility (std of deltas), with a floor.
        if len(recent) >= 2:
            mean_d = drift
            var = sum((d - mean_d) ** 2 for d in deltas) / len(deltas)
            sigma = var ** 0.5
        else:
            sigma = 0.0
        # Floor so a flat series still produces a non-degenerate band.
        sigma = max(sigma, abs(last) * 0.01, 1e-6)

        p10: list[float] = []
        p50: list[float] = []
        p90: list[float] = []
        for step in range(1, horizon + 1):
            center = last + drift * step
            # ~1.28 * sigma ≈ 80% interval for one step; grows with sqrt(step).
            spread = 1.2816 * sigma * (step ** 0.5)
            p50.append(center)
            p10.append(center - spread)
            p90.append(center + spread)

        return {"p10": p10, "p50": p50, "p90": p90, "model": "naive"}
