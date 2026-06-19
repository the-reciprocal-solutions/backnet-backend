from __future__ import annotations

import asyncio
import logging
import httpx

from bacnet_lab.adapters.http.dependencies import get_container
from bacnet_lab.infrastructure.config import load_settings

logger = logging.getLogger(__name__)


async def deliver_anomaly_to_webhook(anomaly: dict, url: str | None = None) -> None:
    """POST an enriched anomaly dict to a configured webhook URL.
    
    If the URL is not provided, it resolves it from the app container settings,
    falling back to default file settings.
    Retries once on failure and logs if it gives up.
    """
    if url is None:
        try:
            container = get_container()
            url = container.settings.webhook.url
        except RuntimeError:
            # Fallback for standalone test execution or when container is not initialized
            settings = load_settings()
            url = settings.webhook.url

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