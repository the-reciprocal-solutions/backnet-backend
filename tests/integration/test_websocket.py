import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bacnet_lab.adapters.http.app import create_app  # pyright: ignore[reportMissingImports]
from bacnet_lab.adapters.web.websocket import manager  # pyright: ignore[reportMissingImports]


def test_websocket_broadcast_and_lifecycle():
    # Setup test app and client
    app = create_app()
    client = TestClient(app)
    
    # Ensure a clean state for the manager
    manager.active_connections.clear()
    
    # Connect client 1
    with client.websocket_connect("/api/ws") as ws1:
        # Connect client 2
        with client.websocket_connect("/api/ws") as ws2:
            assert len(manager.active_connections) == 2
            
            # Broadcast a test message
            asyncio.run(manager.broadcast({"hello": "world"}))
            
            # Verify both clients receive the broadcasted message
            data1 = ws1.receive_json()
            data2 = ws2.receive_json()
            
            assert data1 == {"hello": "world"}
            assert data2 == {"hello": "world"}
        
        # Client 2 has disconnected by exiting its context block.
        # Now broadcast a second message. During this broadcast,
        # manager will detect the failure on client 2 and drop/disconnect it.
        asyncio.run(manager.broadcast({"next": "message"}))
        
        # Client 1 should receive the second message cleanly
        data1_second = ws1.receive_json()
        assert data1_second == {"next": "message"}
        
        # Client 2 should be removed from active connections
        assert len(manager.active_connections) == 1