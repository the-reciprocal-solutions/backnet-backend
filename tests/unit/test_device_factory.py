from pathlib import Path

from bacnet_lab.adapters.bacnet.device_factory import load_all_devices, load_device_from_yaml
from bacnet_lab.domain.enums import PointType


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_load_device_from_yaml():
    path = BACKEND_ROOT / "config" / "devices" / "ahu_01.yaml"
    device = load_device_from_yaml(path)

    assert device.device_id == 1001
    assert device.name == "AHU-01"

    supply_temp = device.get_point_by_name("AHU-01/SupplyAirTemp")
    assert supply_temp is not None
    assert supply_temp.object_type == PointType.ANALOG_INPUT
    assert supply_temp.present_value == 22.5


def test_load_all_devices():
    devices = load_all_devices(str(BACKEND_ROOT / "config" / "devices"))
    assert len(devices) == 8

    device_ids = {d.device_id for d in devices}
    assert 1001 in device_ids


def test_load_all_devices_missing_dir():
    devices = load_all_devices("nonexistent_dir")
    assert devices == []
