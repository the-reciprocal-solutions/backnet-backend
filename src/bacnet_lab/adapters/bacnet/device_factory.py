from __future__ import annotations

import copy
import logging
from pathlib import Path

import yaml

from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Device, Point

logger = logging.getLogger(__name__)


def _parse_point(data: dict) -> Point:
    pv = data.get("present_value", 0)
    return Point(
        object_type=PointType(data["object_type"]),
        object_instance=data["object_instance"],
        object_name=data["object_name"],
        description=data.get("description", ""),
        present_value=pv,
        units=data.get("units", ""),
        cov_increment=data.get("cov_increment", 0.0),
        simulation=data.get("simulation"),
    )


def load_device_from_yaml(path: Path) -> Device:
    with open(path) as f:
        data = yaml.safe_load(f)
    points = [_parse_point(p) for p in data.get("points", [])]
    device = Device(
        device_id=data["device_id"],
        name=data["name"],
        description=data.get("description", ""),
        points=points,
    )
    logger.info("Loaded device %s (%d) with %d points", device.name, device.device_id, len(points))
    return device


def load_all_devices(devices_dir: str) -> list[Device]:
    path = Path(devices_dir)
    if not path.exists():
        logger.warning("Devices directory %s does not exist", devices_dir)
        return []
    devices = []
    for yaml_file in sorted(path.glob("*.yaml")):
        devices.append(load_device_from_yaml(yaml_file))
    return devices


# Keys inside a point's `simulation` dict whose string values are references to
# sibling points (by object_name, e.g. "AHU-01/SupplyAirTemp", or bare name).
_SIM_REF_KEYS = ("target_point", "process_var", "setpoint")


def _rewrite_ref(ref: object, old_prefix: str, new_prefix: str) -> object:
    """Rewrite a single point reference if it is a full-form ref on the
    template's own device. Bare refs and cross-device refs are left untouched."""
    if not isinstance(ref, str):
        return ref
    head = old_prefix + "/"
    if ref.startswith(head):
        return new_prefix + "/" + ref[len(head):]
    return ref


def _rewrite_simulation(sim: dict, old_prefix: str, new_prefix: str) -> dict:
    """Deep-copy a simulation dict and rewrite intra-device references so a
    clone's points point at the clone's renamed siblings."""
    new_sim = copy.deepcopy(sim)
    for key in _SIM_REF_KEYS:
        if key in new_sim:
            new_sim[key] = _rewrite_ref(new_sim[key], old_prefix, new_prefix)
    sources = new_sim.get("sources")
    if isinstance(sources, dict):
        new_sim["sources"] = {
            label: _rewrite_ref(target, old_prefix, new_prefix)
            for label, target in sources.items()
        }
    return new_sim


def _clone_point(point: Point, old_prefix: str, new_prefix: str) -> Point:
    new_point = copy.deepcopy(point)
    # Rename the device prefix in the object_name ("AHU-01/X" -> "AHU-01-2/X").
    name = new_point.object_name
    head = old_prefix + "/"
    if name.startswith(head):
        new_point.object_name = new_prefix + "/" + name[len(head):]
    if isinstance(new_point.simulation, dict):
        new_point.simulation = _rewrite_simulation(
            new_point.simulation, old_prefix, new_prefix
        )
    return new_point


def scale_devices(devices: list[Device], target_count: int) -> list[Device]:
    """Replicate the loaded device templates (round-robin) until the list has
    ``target_count`` devices. Each clone gets a unique device_id, a unique name
    (``f"{base.name}-{copy_index}"``) and uniquely-prefixed point object_names,
    plus rewritten intra-device simulation references so each clone is a fully
    independent BACnet device. If ``target_count`` is <= 0 or <= the number of
    templates, the input is returned unchanged."""
    if target_count <= 0 or target_count <= len(devices) or not devices:
        return devices

    # Running counter for collision-free device ids, starting above the max
    # existing template id.
    next_id = max(d.device_id for d in devices) + 1

    result = list(devices)
    n_templates = len(devices)
    while len(result) < target_count:
        template_index = len(result) % n_templates
        # copy_index counts how many times this template has been cloned so far.
        copy_index = (len(result) // n_templates) + 1
        base = devices[template_index]
        new_name = f"{base.name}-{copy_index}"
        new_points = [_clone_point(p, base.name, new_name) for p in base.points]
        clone = Device(
            device_id=next_id,
            name=new_name,
            description=base.description,
            points=new_points,
        )
        next_id += 1
        result.append(clone)

    logger.info(
        "Scaled devices: %d templates -> %d devices", n_templates, len(result)
    )
    return result
