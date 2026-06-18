"""MQTT device-network adapter.

Exposes simulated devices over MQTT, mirroring ``BAC0Engine`` for the BACnet
side. Each point is published to a retained topic:

    <prefix>/<device_id>/<object_name>            (state, retained)
    <prefix>/<device_id>/<object_name>/set        (command, subscribed)

Implements the same ``DeviceNetworkPort`` contract, so it drops into the
``CompositeDeviceNetwork`` alongside BACnet/KNX. ``paho-mqtt`` is imported
lazily; if it is missing or the broker is unreachable, the engine logs and
degrades to a no-op rather than breaking boot.
"""

from __future__ import annotations

import logging

from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Device
from bacnet_lab.domain.value_objects import PointValue
from bacnet_lab.ports.device_network import DeviceNetworkPort

logger = logging.getLogger(__name__)


class MqttEngine(DeviceNetworkPort):
    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        prefix: str = "bacnet_lab",
        username: str = "",
        password: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._prefix = prefix.rstrip("/")
        self._username = username
        self._password = password
        self._client = None
        self._devices: dict[int, Device] = {}

    # ------------------------------------------------------------------ #
    def _topic(self, device_id: int, object_name: str) -> str:
        return f"{self._prefix}/{device_id}/{object_name}"

    async def _ensure_client(self) -> bool:
        if self._client is not None:
            return True
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.warning("paho-mqtt not installed; MQTT engine disabled (pip install paho-mqtt)")
            return False
        try:
            client = mqtt.Client()
            if self._username:
                client.username_pw_set(self._username, self._password)
            client.on_message = self._on_message
            client.connect(self._host, self._port)
            client.loop_start()  # background network thread
            self._client = client
            logger.info("MQTT engine connected to %s:%d (prefix=%s)",
                        self._host, self._port, self._prefix)
            return True
        except Exception as e:
            logger.warning("MQTT connect failed (%s:%d): %s — engine disabled",
                           self._host, self._port, e)
            return False

    def _on_message(self, client, userdata, msg) -> None:
        """Inbound command on .../set — update the matching point value."""
        try:
            if not msg.topic.endswith("/set"):
                return
            base = msg.topic[: -len("/set")]
            parts = base.split("/")
            device_id = int(parts[-2])
            object_name = parts[-1]
            device = self._devices.get(device_id)
            if not device:
                return
            for p in device.points:
                if p.object_name == object_name:
                    try:
                        p.present_value = float(msg.payload.decode())
                    except (ValueError, AttributeError):
                        p.present_value = msg.payload.decode()
                    break
        except Exception as e:
            logger.debug("MQTT inbound message ignored: %s", e)

    # ------------------------------------------------------------------ #
    async def start_device(self, device: Device, udp_port: int) -> None:
        if not await self._ensure_client():
            return
        self._devices[device.device_id] = device
        for point in device.points:
            state = self._topic(device.device_id, point.object_name)
            self._client.publish(state, str(point.present_value), retain=True)
            self._client.subscribe(f"{state}/set")
        logger.info("MQTT exposed device %s (ID=%d, %d points)",
                    device.name, device.device_id, len(device.points))

    async def stop_device(self, device_id: int) -> None:
        self._devices.pop(device_id, None)

    async def stop_all(self) -> None:
        self._devices.clear()
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception as e:
                logger.debug("MQTT disconnect error: %s", e)
            self._client = None

    async def write_point_value(
        self, device_id: int, object_type: PointType, instance: int, value: PointValue
    ) -> None:
        device = self._devices.get(device_id)
        if not device or self._client is None:
            return
        point = device.get_point(object_type, instance)
        if not point:
            return
        point.present_value = value
        self._client.publish(self._topic(device_id, point.object_name), str(value), retain=True)

    async def read_point_value(
        self, device_id: int, object_type: PointType, instance: int
    ) -> PointValue:
        device = self._devices.get(device_id)
        if not device:
            raise ValueError(f"Device {device_id} not found")
        point = device.get_point(object_type, instance)
        if not point:
            raise ValueError(f"Point {object_type}:{instance} not found on device {device_id}")
        return point.present_value
