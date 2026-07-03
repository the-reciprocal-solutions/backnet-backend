"""KNX discovery adapter.

KNX has **no** runtime device auto-discovery (unlike BACnet's Who-Is). The
source of truth for what exists on a KNX installation is the ETS project: the
group addresses and their datapoint types (DPTs). This adapter therefore
supports two source modes:

* ``ets_file`` — decode an uploaded ETS export (``.knxproj``/``.csv``/``.xml``)
  and surface every group address as a :class:`DiscoveredPoint` under one
  synthetic device representing the imported project.
* ``gateway`` — best-effort connectivity check against a KNXnet/IP gateway via
  ``xknx``; confirms reachability but enumerates nothing (KNX has no scan).
"""

from __future__ import annotations

import base64

from bacnet_lab.adapters.knx.ets_import import parse_ets_file
from bacnet_lab.domain.models.discovery import DiscoveredDevice, DiscoveredPoint
from bacnet_lab.ports.discovery import DiscoveryError, ProtocolDiscoveryPort


class KnxDiscovery(ProtocolDiscoveryPort):
    protocol = "knx"
    label = "KNX"

    def config_schema(self) -> dict:
        return {
            "fields": [
                {"name": "mode", "label": "Source", "type": "select",
                 "options": ["ets_file", "gateway"], "default": "ets_file",
                 "required": True},
                {"name": "ets_file_b64", "label": "ETS File (base64)",
                 "type": "file", "default": "", "required": False},
                {"name": "ets_filename", "label": "ETS Filename", "type": "text",
                 "default": "project.knxproj", "required": False},
                {"name": "password", "label": "ETS Project Password",
                 "type": "password", "default": "", "required": False},
                {"name": "gateway_ip", "label": "Gateway IP", "type": "text",
                 "default": "", "required": False},
                {"name": "gateway_port", "label": "Gateway Port", "type": "number",
                 "default": 3671, "required": False},
            ],
            "notes": (
                "KNX has no live device scan. Import an ETS export (group "
                "addresses + DPTs), or connect a KNXnet/IP gateway to verify "
                "reachability."
            ),
        }

    async def discover(self, config: dict) -> list[DiscoveredDevice]:
        mode = config.get("mode", "ets_file")

        if mode == "ets_file":
            return await self._discover_ets_file(config)
        if mode == "gateway":
            return await self._discover_gateway(config)
        raise DiscoveryError(f"Unknown KNX discovery mode: {mode!r}")

    async def _discover_ets_file(self, config: dict) -> list[DiscoveredDevice]:
        b64 = config.get("ets_file_b64") or ""
        if not b64:
            raise DiscoveryError("No ETS file provided")

        try:
            content = base64.b64decode(b64)
        except Exception as e:  # noqa: BLE001
            raise DiscoveryError(f"Invalid base64 ETS file: {e}") from e

        filename = config.get("ets_filename", "project.knxproj")
        try:
            entries = parse_ets_file(
                filename, content, password=config.get("password") or None
            )
        except ValueError as e:
            raise DiscoveryError(str(e)) from e

        objects = [
            DiscoveredPoint(
                object_name=e.name or e.group_address,
                object_type=("1" if e.dpt.startswith("1") else e.dpt),
                group_address=e.group_address,
                dpt=e.dpt,
            )
            for e in entries
        ]
        device = DiscoveredDevice(
            ref="knx-ets",
            protocol="knx",
            name=f"KNX ETS Import ({filename})",
            address="ets",
            object_count=len(entries),
            objects=objects,
        )
        return [device]

    async def _discover_gateway(self, config: dict) -> list[DiscoveredDevice]:
        gateway_ip = config.get("gateway_ip") or ""
        gateway_port = int(config.get("gateway_port") or 3671)

        xknx = None
        try:
            from xknx import XKNX
            from xknx.io import ConnectionConfig, ConnectionType

            if gateway_ip:
                conn = ConnectionConfig(
                    connection_type=ConnectionType.TUNNELING,
                    gateway_ip=gateway_ip,
                    gateway_port=gateway_port,
                )
            else:
                conn = ConnectionConfig(connection_type=ConnectionType.ROUTING)
            xknx = XKNX(connection_config=conn)
            await xknx.start()
        except Exception as e:  # noqa: BLE001
            raise DiscoveryError(f"KNX gateway unreachable: {e}") from e
        finally:
            if xknx is not None:
                try:
                    await xknx.stop()
                except Exception:  # noqa: BLE001
                    pass

        device = DiscoveredDevice(
            ref="knx-gateway",
            protocol="knx",
            name=f"KNX Gateway {gateway_ip or 'multicast'}",
            address=gateway_ip or "multicast-routing",
            object_count=0,
            objects=[],
            raw={
                "note": (
                    "KNX has no device scan; import an ETS file to enumerate "
                    "group addresses"
                )
            },
        )
        return [device]
