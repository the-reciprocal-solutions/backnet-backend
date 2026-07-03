"""MQTT live discovery adapter.

Connects to a broker, subscribes to ``<prefix>/#`` and groups the topics seen
during a listen window into devices — mirroring the publish convention of
``MqttEngine`` (``<prefix>/<device_id>/<object_name>``). ``paho-mqtt`` is
imported lazily; the client is always disconnected and the listen window is hard-
bounded so the scan never hangs.
"""

from __future__ import annotations

import asyncio
import logging

from bacnet_lab.domain.models.discovery import DiscoveredDevice, DiscoveredPoint
from bacnet_lab.ports.discovery import DiscoveryError, ProtocolDiscoveryPort

logger = logging.getLogger(__name__)


class MqttDiscovery(ProtocolDiscoveryPort):
    protocol = "mqtt"
    label = "MQTT"

    def config_schema(self) -> dict:
        return {
            "fields": [
                {"name": "host", "label": "Broker Host", "type": "text", "default": "127.0.0.1", "required": True},
                {"name": "port", "label": "Port", "type": "number", "default": 1883, "required": True},
                {"name": "prefix", "label": "Topic Prefix", "type": "text", "default": "bacnet_lab", "required": True},
                {"name": "username", "label": "Username", "type": "text", "default": "", "required": False},
                {"name": "password", "label": "Password", "type": "password", "default": "", "required": False},
                {"name": "window_s", "label": "Listen Window (s)", "type": "number", "default": 5, "required": True},
            ],
            "notes": "Subscribes to <prefix>/# and groups published topics by device for the listen window.",
        }

    async def discover(self, config: dict) -> list[DiscoveredDevice]:
        host = config.get("host", "127.0.0.1")
        port = int(config.get("port", 1883))
        prefix = str(config.get("prefix", "bacnet_lab")).rstrip("/")
        username = config.get("username", "")
        password = config.get("password", "")
        window_s = float(config.get("window_s", 5))

        try:
            import paho.mqtt.client as mqtt
        except ImportError as e:  # pragma: no cover
            raise DiscoveryError("paho-mqtt not installed; MQTT discovery unavailable") from e

        # device_id -> {object_name -> last_value}
        seen: dict[str, dict[str, object]] = {}

        def _on_message(_c, _u, msg) -> None:
            try:
                topic = msg.topic
                if not topic.startswith(prefix + "/"):
                    return
                rest = topic[len(prefix) + 1:].split("/")
                if len(rest) < 2:
                    return
                device_id, object_name = rest[0], rest[1]
                if object_name == "set":  # command topic, not a state point
                    return
                try:
                    val = msg.payload.decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    val = None
                seen.setdefault(device_id, {})[object_name] = val
            except Exception:  # noqa: BLE001
                logger.debug("MQTT discovery message parse error", exc_info=True)

        client = mqtt.Client()
        if username:
            client.username_pw_set(username, password)
        client.on_message = _on_message
        try:
            client.connect(host, port)
        except Exception as e:
            raise DiscoveryError(f"MQTT broker unreachable ({host}:{port}): {e}") from e

        try:
            client.subscribe(f"{prefix}/#")
            client.loop_start()
            await asyncio.sleep(window_s)  # collect for the window
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("MQTT disconnect error", exc_info=True)

        out: list[DiscoveredDevice] = []
        for device_id, objs in seen.items():
            out.append(DiscoveredDevice(
                ref=f"mqtt-{device_id}",
                protocol="mqtt",
                name=f"MQTT Device {device_id}",
                address=f"{host}:{port}/{prefix}/{device_id}",
                device_id=int(device_id) if str(device_id).isdigit() else None,
                object_count=len(objs),
                objects=[DiscoveredPoint(object_name=n, present_value=v) for n, v in objs.items()],
            ))
        return out
