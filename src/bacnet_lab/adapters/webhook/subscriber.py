from __future__ import annotations

import asyncio
import logging
import httpx

from bacnet_lab.domain.events import AnomalyEnriched, WorkOrderAssigned

logger = logging.getLogger(__name__)


async def deliver_anomaly_to_webhook(anomaly: dict, url: str | None = None) -> None:
    """POST an enriched anomaly dict to a configured webhook URL.

    The caller is responsible for passing the configured ``url``.
    Retries once on failure and logs if it gives up.
    """
    if not url:
        logger.warning("Webhook URL is not configured. Skipping delivery.")
        return

    # Attempt delivery up to 2 times (initial attempt + 1 retry)
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=anomaly)
                if 200 <= response.status_code < 300:
                    logger.info("Webhook delivery succeeded on attempt %d", attempt + 1)
                    return
                else:
                    logger.warning(
                        "Webhook POST returned status %d on attempt %d",
                        response.status_code, attempt + 1
                    )
        except Exception as e:
            logger.error("Webhook delivery attempt %d failed with exception: %s", attempt + 1, e)

        # Wait 1 second before the retry attempt
        if attempt == 0:
            await asyncio.sleep(1.0)

    logger.error("Failed to deliver webhook after retrying. Giving up.")


class WebhookSubscriber:
    """Bus subscriber that fires a webhook for enriched anomalies + work orders.

    Mirrors ``WsBroadcaster``: subscribes to the event bus and forwards
    ``AnomalyEnriched`` and ``WorkOrderAssigned`` events (post-reasoning) to the
    configured webhook URL using their frozen ``to_message()`` wire contract.
    Exceptions are swallowed and logged so a failing webhook never blocks the bus.
    """

    def __init__(self, event_publisher, url: str, enabled: bool = True) -> None:
        self.url = url
        self.enabled = enabled
        if enabled and url:
            event_publisher.subscribe(self._on_event)

    async def _on_event(self, event) -> None:
        if isinstance(event, (AnomalyEnriched, WorkOrderAssigned)):
            try:
                await deliver_anomaly_to_webhook(event.to_message(), self.url)
            except Exception as e:
                logger.error("WebhookSubscriber failed to deliver event: %s", e)
