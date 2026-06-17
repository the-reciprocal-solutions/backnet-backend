from __future__ import annotations

import random

from bacnet_lab.adapters.simulation.generators.base import SignalGenerator
from bacnet_lab.domain.models.device import Point

# model name -> SignalGenerator subclass
_REGISTRY: dict[str, type[SignalGenerator]] = {}


def register(model: str):
    """Class decorator: register a SignalGenerator under a model name."""

    def _wrap(cls: type[SignalGenerator]) -> type[SignalGenerator]:
        cls.model = model
        _REGISTRY[model] = cls
        return cls

    return _wrap


def available_models() -> list[str]:
    return sorted(_REGISTRY.keys())


def create_generator(
    model: str, point: Point, config: dict, rng: random.Random
) -> SignalGenerator:
    cls = _REGISTRY.get(model)
    if cls is None:
        raise ValueError(
            f"Unknown signal model '{model}'. Available: {available_models()}"
        )
    return cls(point, config, rng)
