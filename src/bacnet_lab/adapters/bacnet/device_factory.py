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
        group_address=data.get("group_address", ""),
        dpt=data.get("dpt", ""),
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
        protocol=data.get("protocol", "bacnet"),
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
            protocol=base.protocol,
        )
        next_id += 1
        result.append(clone)

    logger.info(
        "Scaled devices: %d templates -> %d devices", n_templates, len(result)
    )
    return result


# ---------------------------------------------------------------------------
# Fleet generation (task B5)
# ---------------------------------------------------------------------------
#
# DESIGN / BOOT-SAFETY CHOICE
# ---------------------------
# Goal: present a fleet of 100+ devices spanning all 4 protocols, every device
# anomaly-capable. The hard constraint is that boot must NOT spin up 100 real
# BAC0 UDP stacks (one per BACnet device is heavy).
#
# How heavy boot is avoided:
#   * The `protocol` field is honoured at network-startup time. `BAC0Engine`
#     (the authoritative engine, always present) now SKIPS any device whose
#     `protocol != "bacnet"` — it never opens a UDP stack for those. The
#     mqtt/knx engines are only added to the network when explicitly enabled,
#     and are no-ops otherwise. So a generated fleet that is mostly
#     mqtt/knx/modbus costs nothing at the network layer: those devices are
#     persisted to the DB, held in memory, and simulated, but open no sockets.
#   * The generator therefore assigns only a SMALL, capped subset of generated
#     devices to "bacnet" (see `bacnet_quota`); the rest round-robin across
#     mqtt / knx / modbus. The base YAML devices keep their own protocol tag.
#
# Each generated device is built from a rotated base template but mutated:
# new device_id, new name (TYPE-NN), uniquely-prefixed point object_names,
# rewritten intra-device sim references, and jittered numeric ranges so the
# fleet is varied rather than identical clones. Every generated device is
# guaranteed >=1 dynamic (non-constant) analog point, and at least one
# hard-limit-capable unit, so anomaly injection can fire.

# Protocols other than bacnet are no-op-at-boot (see DESIGN note above).
_FLEET_PROTOCOLS = ("mqtt", "knx", "modbus", "bacnet")

# Dynamic (varying) analog sim models — at least one of these per device makes
# it anomaly-capable. `constant`/`ramp` are intentionally excluded as "dynamic"
# for the anomaly-capability guarantee.
_DYNAMIC_MODELS = ("random_walk", "sine", "first_order_lag", "derived", "pid_actuator")

# Units a hard-limit anomaly injector can fire on.
_HARD_LIMIT_UNITS = (
    "degreesCelsius",
    "millimetersPerSecond",
    "partsPerMillion",
    "percentRelativeHumidity",
)

# Device "kinds" the generator rotates names through, with a preferred base
# template name to clone from when available and a fallback unit.
_FLEET_KINDS = (
    ("AHU", "AHU-01", "degreesCelsius"),
    ("FCU", "FCU-01", "degreesCelsius"),
    ("TEMP", "OAT-01", "degreesCelsius"),
    ("CO2", "CO2-01", "partsPerMillion"),
    ("METER", "PM-01", "percentRelativeHumidity"),
)


def _point_is_dynamic_analog(point: Point) -> bool:
    if point.object_type not in (
        PointType.ANALOG_INPUT,
        PointType.ANALOG_OUTPUT,
        PointType.ANALOG_VALUE,
    ):
        return False
    sim = point.simulation
    return isinstance(sim, dict) and sim.get("model") in _DYNAMIC_MODELS


def _jitter_sim_ranges(sim: dict, factor: float) -> None:
    """Scale a few numeric sim parameters in-place so generated devices differ
    from their template (varied value ranges, not identical clones)."""
    for key in ("center", "mean", "amplitude", "value", "target"):
        v = sim.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            sim[key] = round(v * factor, 4)
    bounds = sim.get("bounds")
    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        lo, hi = bounds
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            sim["bounds"] = [round(lo * factor, 4), round(hi * factor, 4)]


def _build_fleet_device(
    base: Device, device_id: int, new_name: str, factor: float
) -> Device:
    """Clone `base` into a new device with mutated name/ids/ranges."""
    new_points: list[Point] = []
    for p in base.points:
        cp = _clone_point(p, base.name, new_name)
        if isinstance(cp.simulation, dict):
            _jitter_sim_ranges(cp.simulation, factor)
        new_points.append(cp)
    return Device(
        device_id=device_id,
        name=new_name,
        description=f"{base.description} (generated fleet)",
        points=new_points,
        protocol=base.protocol,
    )


def _ensure_anomaly_capable(device: Device) -> None:
    """Guarantee the device has >=1 dynamic analog point AND >=1 hard-limit
    capable unit, mutating points in place if the template lacked them."""
    has_dynamic = any(_point_is_dynamic_analog(p) for p in device.points)
    has_hard_unit = any(
        p.units in _HARD_LIMIT_UNITS
        for p in device.points
        if _point_is_dynamic_analog(p)
    )
    if has_dynamic and has_hard_unit:
        return

    # Find an analog point to promote, else synthesize one.
    target = next(
        (p for p in device.points if p.object_type == PointType.ANALOG_INPUT),
        None,
    )
    if target is None:
        instance = max((p.object_instance for p in device.points), default=0) + 1
        target = Point(
            object_type=PointType.ANALOG_INPUT,
            object_instance=instance,
            object_name=f"{device.name}/Temperature",
            description="Synthesized anomaly-capable analog point",
            present_value=22.0,
            units="degreesCelsius",
            cov_increment=0.5,
        )
        device.points.append(target)

    if not _point_is_dynamic_analog(target):
        center = target.present_value if isinstance(target.present_value, (int, float)) else 22.0
        target.simulation = {
            "model": "random_walk",
            "center": float(center),
            "step": max(abs(float(center)) * 0.01, 0.1),
            "mean_reversion": 0.1,
            "bounds": [float(center) - 20.0, float(center) + 20.0],
        }
    if target.units not in _HARD_LIMIT_UNITS:
        target.units = "degreesCelsius"


def generate_fleet(base_devices: list[Device], target_count: int) -> list[Device]:
    """Generate a varied, multi-protocol fleet of at least ``target_count``
    devices, returning the base devices followed by generated ones.

    See the DESIGN note above for the boot-safety rationale. If
    ``target_count <= len(base_devices)`` or there are no templates, the input
    is returned unchanged.
    """
    if target_count <= 0 or not base_devices or target_count <= len(base_devices):
        return base_devices

    # Index templates by name for kind-preferred cloning; fall back to rotation.
    by_name = {d.name: d for d in base_devices}
    n_templates = len(base_devices)

    # Cap how many generated devices speak real BACnet so boot never opens more
    # than a handful of UDP stacks. ~5% of the fleet, min 2, max 8.
    bacnet_quota = max(2, min(8, target_count // 20))

    next_id = max(d.device_id for d in base_devices) + 1
    result = list(base_devices)
    gen_index = 0
    while len(result) < target_count:
        kind, base_name, _unit = _FLEET_KINDS[gen_index % len(_FLEET_KINDS)]
        base = by_name.get(base_name) or base_devices[gen_index % n_templates]

        # Assign protocol: a small leading slice are real BACnet, the rest
        # round-robin across the no-op-at-boot protocols.
        if gen_index < bacnet_quota:
            protocol = "bacnet"
        else:
            non_bacnet = ("mqtt", "knx", "modbus")
            protocol = non_bacnet[(gen_index - bacnet_quota) % len(non_bacnet)]

        new_name = f"{kind}-{gen_index + 1:03d}"
        # Vary numeric ranges per device (0.8 .. 1.2 deterministic by index).
        factor = 0.8 + ((gen_index % 9) * 0.05)
        device = _build_fleet_device(base, next_id, new_name, factor)
        device.protocol = protocol
        _ensure_anomaly_capable(device)

        result.append(device)
        next_id += 1
        gen_index += 1

    n_generated = len(result) - n_templates
    logger.info(
        "Generated fleet: %d templates -> %d devices (%d generated, %d bacnet quota)",
        n_templates, len(result), n_generated, bacnet_quota,
    )
    return result
