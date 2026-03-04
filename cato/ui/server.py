"""
cato/ui/server.py — aiohttp server that serves the Cato dashboard.

Mounts:
  GET /          → dashboard.html (SPA)
  GET /health    → JSON health payload
  GET /ws        → WebSocket upgrade (delegates to gateway)
  POST /config   → Save config (stub; gateway wires real handler)
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web, WSMsgType

logger = logging.getLogger(__name__)

_DASHBOARD = Path(__file__).parent / "dashboard.html"
_START_TIME = time.monotonic()


async def create_ui_app(gateway: Optional[Any] = None) -> web.Application:
    """Create and return the aiohttp Application serving the dashboard.

    Args:
        gateway: The Gateway instance. May be None for standalone testing;
                 pages will render but WebSocket will show disconnected state.
    """
    app = web.Application()

    # ------------------------------------------------------------------ #
    # Route handlers                                                       #
    # ------------------------------------------------------------------ #

    async def serve_dashboard(request: web.Request) -> web.FileResponse:
        """Serve the single-page dashboard HTML."""
        return web.FileResponse(_DASHBOARD)

    async def health(request: web.Request) -> web.Response:
        """Return JSON health payload consumed by the UI health pill."""
        sessions = len(gateway._lanes) if gateway is not None else 0
        uptime   = int(time.monotonic() - _START_TIME)
        return web.json_response({
            "status":   "ok",
            "version":  "0.1.0",
            "sessions": sessions,
            "uptime":   uptime,
        })

    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        """Upgrade HTTP → WebSocket and proxy messages through the gateway."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        if gateway is not None:
            gateway.register_websocket(ws)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if gateway is not None:
                        await gateway.handle_ws_message(ws, msg.data)
                    else:
                        # Standalone: echo health only
                        try:
                            data = json.loads(msg.data)
                            if data.get("type") == "health":
                                await ws.send_str(json.dumps({
                                    "type":     "health",
                                    "status":   "ok",
                                    "sessions": 0,
                                    "uptime":   int(time.monotonic() - _START_TIME),
                                }))
                        except (json.JSONDecodeError, KeyError):
                            pass
                elif msg.type == WSMsgType.ERROR:
                    logger.warning("WebSocket error: %s", ws.exception())
        finally:
            if gateway is not None:
                gateway.unregister_websocket(ws)

        return ws

    async def save_config(request: web.Request) -> web.Response:
        """Stub POST /config endpoint. Replace with real persistence as needed."""
        try:
            body = await request.json()
            logger.info("Config save requested: %d keys", len(body))
            # TODO: wire to CatoConfig.save() once that method exists
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("Config save error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=400)

    # ------------------------------------------------------------------ #
    # Router                                                               #
    # ------------------------------------------------------------------ #

    app.router.add_get("/",        serve_dashboard)
    app.router.add_get("/health",  health)
    app.router.add_get("/ws",      websocket_handler)
    app.router.add_post("/config", save_config)

    logger.info("UI app created — dashboard: %s", _DASHBOARD)
    return app
