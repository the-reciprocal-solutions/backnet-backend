"""Port: live device discovery, one implementation per protocol.

Each protocol (BACnet, Modbus, KNX, MQTT, …) provides an adapter that knows how
to connect to a real network with operator-supplied settings and return the
devices it finds. The ``DiscoveryService`` drives these behind protocol-namespaced
HTTP routes (``/api/<protocol>/discover``), so the API surface is identical per
protocol — only the path segment and the config fields differ.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from bacnet_lab.domain.models.discovery import DiscoveredDevice


class ProtocolDiscoveryPort(ABC):
    """Contract every protocol discovery adapter implements.

    Implementations MUST be fail-safe: a connection/timeout error raises
    ``DiscoveryError`` with a clear message rather than hanging or crashing the
    scan worker.
    """

    #: lowercase protocol id used in the API path, e.g. "bacnet", "modbus".
    protocol: str = ""
    #: human label for the protocol-select cards.
    label: str = ""

    @abstractmethod
    def config_schema(self) -> dict:
        """Return the scan-config field spec for this protocol.

        Shape: ``{"fields": [{"name", "label", "type", "default", "required",
        "options"?}], "notes"?: str}`` — the frontend renders the Scan
        Configuration form from this, so each protocol can expose its own
        settings (IP range, port, unit range, gateway, broker, …).
        """
        ...

    @abstractmethod
    async def discover(self, config: dict) -> list[DiscoveredDevice]:
        """Connect with ``config`` and return discovered devices.

        ``config`` is validated against ``config_schema()`` keys by the caller.
        Raise ``DiscoveryError`` on connection failure / bad config.
        """
        ...


class DiscoveryError(Exception):
    """Raised when a discovery scan cannot connect or the config is invalid."""
