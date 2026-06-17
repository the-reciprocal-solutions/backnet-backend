"""Self-contained time-series forecasting module for the BACnet simulator.

This package is intentionally decoupled from the application core: it owns its
own direct TimescaleDB access (see :mod:`db`) and a Chronos model wrapper with a
graceful naive fallback (see :mod:`chronos_model`). The model service can be run
standalone, independently of the app's ``TimescaleTimeSeries`` adapter.

Heavy ML dependencies are OPTIONAL and lazily imported. To enable real
zero-shot Chronos forecasting:

    pip install chronos-forecasting torch

Without those installed, the service transparently falls back to a naive
persistence + linear-drift forecaster, so the API still works.
"""

from __future__ import annotations

from bacnet_lab.forecasting.service import ForecastResult, ForecastService

__all__ = ["ForecastService", "ForecastResult"]
