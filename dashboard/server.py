"""
dashboard/server.py — Live dashboard WebSocket server
=======================================================
Streams agent events to the browser in real-time.
Run alongside the agent system:

    python dashboard/server.py          # starts on ws://localhost:8765
    # open dashboard/index.html in browser

Or start it from main.py with --dashboard flag.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

# Ensure project root is on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("dashboard")


async def run_server(host: str = "127.0.0.1", port: int = 8765):
    """
    Start the WebSocket server.
    Requires: pip install websockets
    Falls back gracefully if websockets not installed.
    """
    try:
        import websockets
    except ImportError:
        log.error("websockets not installed — run: pip install websockets")
        return

    from core.events import subscribe_events, read_events, Event

    # All currently connected WebSocket clients
    clients: set = set()

    async def broadcast(event: "Event"):
        """Called for every emitted event — fans out to all WS clients."""
        if not clients:
            return
        msg = json.dumps(event.to_dict())
        dead = set()
        for ws in list(clients):
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        clients.difference_update(dead)

    # Register broadcaster with event system
    subscribe_events(broadcast)

    async def handler(websocket):
        clients.add(websocket)
        log.info("Client connected (%d total)", len(clients))

        try:
            # Send last 200 events as backfill so browser shows history
            backfill = read_events(last_n=200)
            for event in backfill:
                try:
                    await websocket.send(json.dumps(event.to_dict()))
                except Exception:
                    break

            # Keep connection alive; recv() blocks until client disconnects
            await websocket.wait_closed()

        finally:
            clients.discard(websocket)
            log.info("Client disconnected (%d total)", len(clients))

    log.info("Dashboard WebSocket server starting on ws://%s:%d", host, port)
    log.info("Open dashboard/index.html in your browser")

    async with websockets.serve(handler, host, port):
        await asyncio.Future()   # run forever


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(name)s  %(message)s")
    asyncio.run(run_server(args.host, args.port))
