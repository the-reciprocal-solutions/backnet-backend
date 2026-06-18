import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bacnet_lab.adapters.modbus.engine import ModbusEngine
from bacnet_lab.domain.enums import PointType
from bacnet_lab.domain.models.device import Device, Point


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.connected = False
    client.connect = AsyncMock(return_value=True)
    client.close = MagicMock()
    
    # Mock register and bit responses
    client.read_holding_registers = AsyncMock()
    client.read_input_registers = AsyncMock()
    client.read_discrete_inputs = AsyncMock()
    client.read_coils = AsyncMock()
    client.write_register = AsyncMock()
    client.write_coil = AsyncMock()
    
    return client


@pytest.mark.asyncio
async def test_modbus_engine_lifecycle(mock_client):
    with patch("bacnet_lab.adapters.modbus.engine.AsyncModbusTcpClient", return_value=mock_client):
        engine = ModbusEngine(host="127.0.0.1", port=5020)
        
        # Test start_device connects
        device = Device(device_id=1, name="Test Device")
        await engine.start_device(device, udp_port=1234)
        
        assert mock_client.connect.called
        assert engine._devices[1] == device
        
        # Test stop_device
        await engine.stop_device(1)
        assert 1 not in engine._devices
        
        # Test stop_all closes connection
        await engine.start_device(device, udp_port=1234)
        await engine.stop_all()
        assert len(engine._devices) == 0
        assert mock_client.close.called


@pytest.mark.asyncio
async def test_modbus_read_write_values(mock_client):
    with patch("bacnet_lab.adapters.modbus.engine.AsyncModbusTcpClient", return_value=mock_client):
        engine = ModbusEngine(host="127.0.0.1", port=5020)
        
        device = Device(device_id=5, name="Device 5", points=[
            Point(object_type=PointType.ANALOG_VALUE, object_instance=2, object_name="Holding_2")
        ])
        await engine.start_device(device, udp_port=1234)
        
        # 1. Test Read Analog Value
        mock_response = MagicMock()
        mock_response.isError = MagicMock(return_value=False)
        mock_response.registers = [123]
        mock_client.read_holding_registers.return_value = mock_response
        
        val = await engine.read_point_value(device_id=5, object_type=PointType.ANALOG_VALUE, instance=2)
        assert val == 123
        mock_client.read_holding_registers.assert_called_with(2, device_id=5)
        # Check cache updated
        assert device.points[0].present_value == 123
        
        # 2. Test Write Analog Value
        mock_write_response = MagicMock()
        mock_write_response.isError = MagicMock(return_value=False)
        mock_client.write_register.return_value = mock_write_response
        
        await engine.write_point_value(device_id=5, object_type=PointType.ANALOG_VALUE, instance=2, value=456)
        mock_client.write_register.assert_called_with(2, 456, device_id=5)
        # Check cache updated
        assert device.points[0].present_value == 456


@pytest.mark.asyncio
async def test_modbus_discovery(mock_client):
    with patch("bacnet_lab.adapters.modbus.engine.AsyncModbusTcpClient", return_value=mock_client):
        # Scan unit range 1 to 3
        engine = ModbusEngine(host="127.0.0.1", port=5020, unit_start=1, unit_end=3)
        
        # Mock behavior: unit 1 responds, unit 2 fails (isError), unit 3 throws exception
        mock_success = MagicMock()
        mock_success.isError = MagicMock(return_value=False)
        mock_success.registers = [99]
        
        mock_error = MagicMock()
        mock_error.isError = MagicMock(return_value=True)
        
        async def mock_read(address, **kwargs):
            device_id = kwargs.get("device_id")
            if device_id == 1:
                return mock_success
            elif device_id == 2:
                return mock_error
            else:
                raise ConnectionError("Timeout")
                
        mock_client.read_holding_registers.side_effect = mock_read
        
        discovered = await engine.discover()
        
        # Only Unit 1 should be successfully discovered
        assert len(discovered) == 1
        assert discovered[0].device_id == 1
        assert discovered[0].name == "Modbus Device 1"
        assert discovered[0].points[0].present_value == 99