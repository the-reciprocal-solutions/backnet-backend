from collections import Counter

from bacnet_lab.adapters.bacnet.device_factory import (
    _HARD_LIMIT_UNITS,
    _point_is_dynamic_analog,
    generate_fleet,
    load_all_devices,
)


def _fleet(n: int = 100):
    base = load_all_devices("config/devices")
    return base, generate_fleet(base, n)


def test_fleet_reaches_target_count():
    _base, fleet = _fleet(100)
    assert len(fleet) >= 100


def test_fleet_spans_all_four_protocols():
    _base, fleet = _fleet(100)
    protos = Counter(d.protocol for d in fleet)
    assert set(protos) >= {"bacnet", "mqtt", "knx", "modbus"}


def test_fleet_device_ids_unique():
    _base, fleet = _fleet(100)
    ids = [d.device_id for d in fleet]
    assert len(ids) == len(set(ids))


def test_every_device_anomaly_capable():
    _base, fleet = _fleet(100)
    for d in fleet:
        assert any(_point_is_dynamic_analog(p) for p in d.points), d.name


def test_generated_devices_have_hard_limit_unit():
    base, fleet = _fleet(100)
    for d in fleet[len(base):]:
        assert any(
            p.units in _HARD_LIMIT_UNITS and _point_is_dynamic_analog(p)
            for p in d.points
        ), d.name


def test_generated_names_vary():
    base, fleet = _fleet(100)
    gen_names = [d.name for d in fleet[len(base):]]
    # Not identical clones: names are all distinct.
    assert len(gen_names) == len(set(gen_names))


def test_bacnet_subset_is_capped():
    # Boot safety: only a small slice of GENERATED devices speak real BACnet.
    base, fleet = _fleet(100)
    gen_bacnet = [d for d in fleet[len(base):] if d.protocol == "bacnet"]
    assert len(gen_bacnet) <= 8


def test_fleet_off_when_below_template_count():
    base = load_all_devices("config/devices")
    assert generate_fleet(base, 0) is base
    assert generate_fleet(base, len(base)) is base
