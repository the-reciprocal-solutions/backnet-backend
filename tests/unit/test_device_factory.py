from pathlib import Path

from bacnet_lab.adapters.bacnet.device_factory import load_all_devices, load_device_from_yaml
from bacnet_lab.domain.enums import PointType


def test_load_device_from_yaml():
    path = Path("config/devices/ahu_01.yaml")
    device = load_device_from_yaml(path)

    assert device.device_id == 1001
    assert device.name == "AHU-01"
    assert len(device.points) == 13

    supply_temp = device.get_point_by_name("AHU-01/SupplyAirTemp")
    assert supply_temp is not None
    assert supply_temp.object_type == PointType.ANALOG_INPUT
    assert supply_temp.present_value == 22.5


def test_load_all_devices():
    from pathlib import Path

    devices = load_all_devices("config/devices")
    # One device per YAML file in the config dir (grows as protocols are added).
    expected = len(list(Path("config/devices").glob("*.yaml")))
    assert len(devices) == expected

    # The original BACnet/MQTT fleet must always be present.
    device_ids = {d.device_id for d in devices}
    assert {1001, 2001, 2002, 3001, 4001, 5001, 5002} <= device_ids


def test_load_all_devices_missing_dir():
    devices = load_all_devices("nonexistent_dir")
    assert devices == []
