import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bacnet_lab.adapters.webhook.subscriber import deliver_anomaly_to_webhook


@pytest.mark.asyncio
async def test_webhook_delivery_success():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.post = AsyncMock(return_value=mock_response)
    
    # Mock httpx.AsyncClient context manager to yield our mock client
    mock_async_context = MagicMock()
    mock_async_context.__aenter__ = AsyncMock(return_value=mock_client)
    mock_async_context.__aexit__ = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_async_context):
        anomaly = {"type": "anomaly", "device_id": 42, "severity": "high"}
        await deliver_anomaly_to_webhook(anomaly, url="http://example.com/webhook")
        
        # Verify post was called once with the correct parameters
        mock_client.post.assert_called_once_with("http://example.com/webhook", json=anomaly)


@pytest.mark.asyncio
async def test_webhook_delivery_retry_once_and_succeed():
    mock_client = MagicMock()
    mock_fail_response = MagicMock()
    mock_fail_response.status_code = 502
    mock_success_response = MagicMock()
    mock_success_response.status_code = 200
    
    # Set up mock to fail on the first call, and succeed on the second
    mock_client.post = AsyncMock(side_effect=[mock_fail_response, mock_success_response])
    
    mock_async_context = MagicMock()
    mock_async_context.__aenter__ = AsyncMock(return_value=mock_client)
    mock_async_context.__aexit__ = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_async_context):
        with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
            anomaly = {"type": "anomaly"}
            await deliver_anomaly_to_webhook(anomaly, url="http://example.com/webhook")
            
            # Verify it was called twice and waited 1s in between
            assert mock_client.post.call_count == 2
            mock_sleep.assert_called_once_with(1.0)


@pytest.mark.asyncio
async def test_webhook_delivery_failure_gives_up_silently():
    mock_client = MagicMock()
    # Mock post throwing a connection timeout error
    mock_client.post = AsyncMock(side_effect=Exception("Network Timeout Error"))
    
    mock_async_context = MagicMock()
    mock_async_context.__aenter__ = AsyncMock(return_value=mock_client)
    mock_async_context.__aexit__ = AsyncMock()

    with patch("httpx.AsyncClient", return_value=mock_async_context):
        with patch("asyncio.sleep", AsyncMock()) as mock_sleep:
            anomaly = {"type": "anomaly"}
            
            # The function should catch the exception internally and log it, rather than crashing the caller
            await deliver_anomaly_to_webhook(anomaly, url="http://example.com/webhook")
            
            assert mock_client.post.call_count == 2
            mock_sleep.assert_called_once_with(1.0)