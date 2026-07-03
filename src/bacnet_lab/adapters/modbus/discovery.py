"""Modbus TCP live discovery adapter.

Probes a range of Modbus unit IDs on a target host via the real
``ModbusEngine`` (pymodbus ``AsyncModbusTcpClient``) and maps the responding
domain ``Device`` records into transient ``DiscoveredDevice`` results.
"""

from __future__ import annotations

from bacnet_lab.domain.models.discovery import DiscoveredDevice, DiscoveredPoint
from bacnet_lab.ports.discovery import DiscoveryError, ProtocolDiscoveryPort


class ModbusDiscovery(ProtocolDiscoveryPort):
    """Discover real Modbus TCP devices by scanning a unit-ID range."""

    protocol = "modbus"
    label = "Modbus TCP"

    def config_schema(self) -> dict:
        return {
            "fields": [
                {"name": "host", "label": "Host / IP", "type": "text", "default": "127.0.0.1", "required": True},
                {"name": "port", "label": "Port", "type": "number", "default": 502, "required": True},
                {"name": "unit_start", "label": "Unit ID From", "type": "number", "default": 1, "required": True},
                {"name": "unit_end", "label": "Unit ID To", "type": "number", "default": 10, "required": True},
                {"name": "timeout_s", "label": "Timeout (s)", "type": "number", "default": 5, "required": False},
                {"name": "deep_scan", "label": "Deep Scan (read all registers)", "type": "bool", "default": False, "required": False},
            ],
            "notes": "Probes each Modbus unit ID in the range on the target host.",
        }

    async def discover(self, config: dict) -> list[DiscoveredDevice]:
        # Import lazily so the module parses/imports even when pymodbus is
        # unavailable in the local test environment.
        from bacnet_lab.adapters.modbus.engine import ModbusEngine

        host = config.get("host", "127.0.0.1")
        port = int(config.get("port", 502))
        unit_start = int(config.get("unit_start", 1))
        unit_end = int(config.get("unit_end", 10))

        engine = ModbusEngine(host=host, port=port, unit_start=unit_start, unit_end=unit_end)
        try:
            devices = await engine.discover()
        except Exception as e:
            raise DiscoveryError(f"Modbus discovery failed: {e}") from e
        finally:
            try:
                await engine.stop_all()
            except Exception:
                pass

        return [
            DiscoveredDevice(
                ref=f"unit-{d.device_id}",
                protocol="modbus",
                name=d.name,
                address=f"{host}:{port}#{d.device_id}",
                device_id=d.device_id,
                object_count=len(d.points),
                objects=[
                    DiscoveredPoint(
                        object_name=p.object_name,
                        object_type=p.object_type.value,
                        object_instance=p.object_instance,
                        present_value=p.present_value,
                        units=p.units,
                    )
                    for p in d.points
                ],
            )
            for d in devices
        ]
