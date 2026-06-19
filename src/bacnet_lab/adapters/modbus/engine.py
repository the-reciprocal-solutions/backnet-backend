from __future__ import annotations

import logging
from pymodbus.client import AsyncModbusTcpClient

from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Device, Point
from bacnet_lab.domain.value_objects import PointValue
from bacnet_lab.ports.device_network import DeviceNetworkPort

logger = logging.getLogger(__name__)


class ModbusEngine(DeviceNetworkPort):
    """Manages connection to a Modbus TCP server and reads/writes registers.
    
    Implements the DeviceNetworkPort interface to support reading, writing,
    and discovering virtual or physical Modbus devices under the hexagonal architecture.
    """

    def __init__(self, host: str, port: int, unit_start: int = 1, unit_end: int = 10) -> None:
        self.host = host
        self.port = port
        self.unit_start = unit_start
        self.unit_end = unit_end
        self._devices: dict[int, Device] = {}
        self._client: AsyncModbusTcpClient | None = None

    async def _get_client(self) -> AsyncModbusTcpClient:
        """Helper to get or create an active, connected AsyncModbusTcpClient."""
        if self._client is None:
            self._client = AsyncModbusTcpClient(self.host, port=self.port)
        if not self._client.connected:
            success = await self._client.connect()
            if not success:
                raise IOError(f"Could not connect to Modbus TCP server at {self.host}:{self.port}")
        return self._client

    async def start_device(self, device: Device, udp_port: int) -> None:
        """Register a device in memory and ensure client is connected."""
        self._devices[device.device_id] = device
        try:
            await self._get_client()
            logger.info("Started Modbus device %s (ID=%d)", device.name, device.device_id)
        except Exception as e:
            logger.error("Failed to connect/initialize Modbus client for device %d: %s", device.device_id, e)
            raise

    async def stop_device(self, device_id: int) -> None:
        """Remove a device from memory."""
        self._devices.pop(device_id, None)
        logger.info("Stopped Modbus device %d", device_id)

    async def stop_all(self) -> None:
        """Clear all registered devices and disconnect the Modbus client."""
        self._devices.clear()
        if self._client:
            self._client.close()
            self._client = None
            logger.info("Stopped all Modbus devices and closed client connection.")

    async def write_point_value(
        self, device_id: int, object_type: PointType, instance: int, value: PointValue
    ) -> None:
        """Write a value to a Modbus register/coil."""
        client = await self._get_client()
        unit_id = device_id  # Map device ID directly to Modbus unit ID

        if object_type in (PointType.ANALOG_VALUE, PointType.ANALOG_OUTPUT):
            val = int(value)
            response = await client.write_register(instance, val, device_id=unit_id)
            if response.isError():
                raise IOError(f"Modbus error writing holding register {instance} on unit {unit_id}: {response}")
        elif object_type in (PointType.BINARY_VALUE, PointType.BINARY_OUTPUT):
            val = bool(value)
            response = await client.write_coil(instance, val, device_id=unit_id)
            if response.isError():
                raise IOError(f"Modbus error writing coil {instance} on unit {unit_id}: {response}")
        else:
            raise ValueError(f"Writing to read-only or unsupported Modbus point type: {object_type}")

        # Update cache if device is tracked
        device = self._devices.get(device_id)
        if device:
            point = device.get_point(object_type, instance)
            if point:
                point.present_value = value

    async def read_point_value(
        self, device_id: int, object_type: PointType, instance: int
    ) -> PointValue:
        """Read a value from a Modbus register/coil."""
        client = await self._get_client()
        unit_id = device_id  # Map device ID directly to Modbus unit ID

        if object_type == PointType.ANALOG_INPUT:
            response = await client.read_input_registers(instance, device_id=unit_id)
            if response.isError():
                raise IOError(f"Modbus error reading input register {instance} on unit {unit_id}: {response}")
            val = response.registers[0]
        elif object_type in (PointType.ANALOG_VALUE, PointType.ANALOG_OUTPUT):
            response = await client.read_holding_registers(instance, device_id=unit_id)
            if response.isError():
                raise IOError(f"Modbus error reading holding register {instance} on unit {unit_id}: {response}")
            val = response.registers[0]
        elif object_type == PointType.BINARY_INPUT:
            response = await client.read_discrete_inputs(instance, device_id=unit_id)
            if response.isError():
                raise IOError(f"Modbus error reading discrete input {instance} on unit {unit_id}: {response}")
            val = response.bits[0]
        elif object_type in (PointType.BINARY_VALUE, PointType.BINARY_OUTPUT):
            response = await client.read_coils(instance, device_id=unit_id)
            if response.isError():
                raise IOError(f"Modbus error reading coil {instance} on unit {unit_id}: {response}")
            val = response.bits[0]
        else:
            raise ValueError(f"Unsupported Modbus point type: {object_type}")

        # Update cache if device is tracked
        device = self._devices.get(device_id)
        if device:
            point = device.get_point(object_type, instance)
            if point:
                point.present_value = val

        return val

    async def discover(self) -> list[Device]:
        """Perform a scan of the configured unit-ID range to discover responding Modbus devices."""
        client = await self._get_client()
        discovered_devices: list[Device] = []

        logger.info("Starting Modbus scan from unit %d to %d", self.unit_start, self.unit_end)
        for unit_id in range(self.unit_start, self.unit_end + 1):
            try:
                # Probe by reading holding register 0 (standard baseline check)
                response = await client.read_holding_registers(0, device_id=unit_id)
                if response is not None and not response.isError():
                    device = Device(
                        device_id=unit_id,
                        name=f"Modbus Device {unit_id}",
                        description=f"Discovered Modbus Device on Unit ID {unit_id}",
                        points=[
                            Point(
                                object_type=PointType.ANALOG_VALUE,
                                object_instance=0,
                                object_name="HoldingRegister_0",
                                present_value=response.registers[0],
                                description="Default holding register 0"
                            )
                        ]
                    )
                    discovered_devices.append(device)
                    logger.info("Discovered Modbus unit ID %d", unit_id)
            except Exception as e:
                logger.debug("Probing unit ID %d failed/timed out: %s", unit_id, e)

        return discovered_devices