from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from bacnet_lab.adapters.http.dependencies import get_container

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


@router.websocket("/api/ws")
async def anomaly_ws(ws: WebSocket) -> None:
    """Live stream of enriched anomalies + work orders (message.type-dispatched
    by the frontend ws.js client). Inbound client messages are ignored (keep-alive)."""
    manager = get_container().ws_manager
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep the socket open; content unused
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        logger.debug("WS loop ended: %s", e)
        manager.disconnect(ws)
