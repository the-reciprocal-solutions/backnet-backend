from __future__ import annotations

import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept connection and add to set of active clients."""
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info("New WebSocket connection accepted. Active connections: %d", len(self.active_connections))

    def disconnect(self, websocket: WebSocket) -> None:
        """Remove connection from the set of active clients."""
        self.active_connections.discard(websocket)
        logger.info("WebSocket connection disconnected. Active connections: %d", len(self.active_connections))

    async def broadcast(self, message: dict) -> None:
        """Send JSON message to all active clients.
        
        Drops and removes any client that errors out on sending, preventing one
        dead connection from blocking the entire broadcast loop.
        """
        if not self.active_connections:
            return

        # Iterate over a list copy to safely remove failed connections during iteration
        failed_connections = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.warning("Failed to send message to WebSocket client, marking for removal: %s", e)
                failed_connections.append(connection)

        for connection in failed_connections:
            self.disconnect(connection)


manager = ConnectionManager()


@router.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """FastAPI WebSocket endpoint for the client connection and keep-alive loop."""
    await manager.connect(websocket)
    try:
        while True:
            # Await client messages to keep the connection alive.
            # We ignore any sent content as the WebSocket is outbound only.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error("Error in WebSocket connection loop: %s", e)
        manager.disconnect(websocket)


@router.get("/api/ws/test")
async def test_broadcast_route():
    """HTTP GET endpoint to trigger a websocket broadcast on the running server."""
    payload = {
        "type": "anomaly",
        "device_id": 1001,
        "point": "AHU-1.vibration",
        "value": 8.3,
        "unit": "mm/s",
        "severity": "high",
        "anomaly": {"score": 0.91, "kind": "vibration_spike"},
        "ts": "2026-06-18T10:30:00Z"
    }
    await manager.broadcast(payload)
    return {"status": "Test message broadcasted successfully!", "payload": payload}