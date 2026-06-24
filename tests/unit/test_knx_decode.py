"""Unit tests for KNX pure helpers (no xknx required)."""

import pytest

from bacnet_lab.adapters.knx.engine import (
    _dpt_main,
    _knx_float16,
    _knx_float16_decode,
)


@pytest.mark.parametrize("value", [21.5, 0.0, -5.0, 100.0])
def test_float16_roundtrip(value):
    raw = _knx_float16(value)
    decoded = _knx_float16_decode(raw)
    assert abs(decoded - value) <= 0.1


def test_float16_decode_short_input():
    assert _knx_float16_decode([]) == 0.0
    assert _knx_float16_decode([0x0C]) == 0.0


def test_dpt_main():
    assert _dpt_main("5.001") == 5
    assert _dpt_main("9.001") == 9
    assert _dpt_main("1") == 1
    assert _dpt_main("") is None
    assert _dpt_main("bogus") is None
