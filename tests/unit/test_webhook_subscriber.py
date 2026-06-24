import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bacnet_lab.adapters.event_bus.in_process import InProcessEventPublisher
from bacnet_lab.adapters.webhook import subscriber as subscriber_mod
from bacnet_lab.adapters.webhook.subscriber import (
    WebhookSubscriber,
    deliver_anomaly_to_webhook,
)
from bacnet_lab.domain.events import AnomalyEnriched, AlarmRaised


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


@pytest.mark.asyncio
async def test_webhook_subscriber_fires_on_anomaly_enriched(monkeypatch):
    delivered = []

    async def fake_deliver(anomaly, url=None):
        delivered.append((anomaly, url))

    monkeypatch.setattr(subscriber_mod, "deliver_anomaly_to_webhook", fake_deliver)

    publisher = InProcessEventPublisher()
    WebhookSubscriber(publisher, "http://example.com/webhook", enabled=True)

    event = AnomalyEnriched(device_id=42, point="temp", value=99.0, severity="high")
    await publisher.publish(event)

    assert len(delivered) == 1
    anomaly, url = delivered[0]
    assert url == "http://example.com/webhook"
    assert anomaly == event.to_message()


@pytest.mark.asyncio
async def test_webhook_subscriber_ignores_pre_reasoning_alarm(monkeypatch):
    delivered = []

    async def fake_deliver(anomaly, url=None):
        delivered.append((anomaly, url))

    monkeypatch.setattr(subscriber_mod, "deliver_anomaly_to_webhook", fake_deliver)

    publisher = InProcessEventPublisher()
    WebhookSubscriber(publisher, "http://example.com/webhook", enabled=True)

    await publisher.publish(AlarmRaised())

    assert delivered == []


@pytest.mark.asyncio
async def test_webhook_subscriber_disabled_does_not_subscribe(monkeypatch):
    delivered = []

    async def fake_deliver(anomaly, url=None):
        delivered.append((anomaly, url))

    monkeypatch.setattr(subscriber_mod, "deliver_anomaly_to_webhook", fake_deliver)

    publisher = InProcessEventPublisher()
    WebhookSubscriber(publisher, "http://example.com/webhook", enabled=False)

    await publisher.publish(AnomalyEnriched(device_id=1, point="temp"))

    assert delivered == []