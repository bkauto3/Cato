"""
cato/gateway.py — Central message bus for CATO.

- Receives messages from channel adapters (Telegram, WhatsApp) via asyncio queues
- Routes messages to per-session FIFO LaneQueues (never interleave sessions)
- Drives the AgentLoop for each task
- Sends responses back to the originating channel adapter
- Exposes a WebSocket + REST server on 127.0.0.1:18789
- Fires cron-scheduled tasks into lane queues via croniter
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from .budget import BudgetExceeded, BudgetManager
from .config import CatoConfig
from .heartbeat import HeartbeatMonitor
from .node import NodeManager
from .platform import get_data_dir
from .vault import Vault

logger = logging.getLogger(__name__)

_CATO_DIR      = get_data_dir()
_WS_HOST       = "127.0.0.1"
_WS_PORT       = 18789  # default; overridden by config.webchat_port + 1
_LANE_QUEUE_MAX = 64


# ---------------------------------------------------------------------------
# LaneQueue — per-session FIFO serialiser
# ---------------------------------------------------------------------------

class LaneQueue:
    """Serialises message processing for one session_id (one task at a time)."""

    def __init__(self, session_id: str, gateway: "Gateway") -> None:
        self._session_id = session_id
        self._gateway = gateway
        self._queue: asyncio.Queue[Optional[dict]] = asyncio.Queue(maxsize=_LANE_QUEUE_MAX)
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name=f"lane-{self._session_id}")

    async def enqueue(self, task: dict) -> None:
        """Add a task dict. Blocks on back-pressure when queue is full."""
        await self._queue.put(task)

    async def stop(self) -> None:
        """Signal the worker to exit after draining the queue."""
        await self._queue.put(None)
        if self._task:
            await self._task

    async def run_forever(self) -> None:
        """Process tasks sequentially — FIFO, never concurrent within session."""
        while True:
            task = await self._queue.get()
            if task is None:
                self._queue.task_done()
                break
            try:
                await self._gateway._process_task(task)
            except Exception as exc:
                logger.error("Lane %s error: %s", self._session_id, exc)
            finally:
                self._queue.task_done()


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class Gateway:
    """Central message bus. One instance per CATO process."""

    def __init__(self, config: CatoConfig, budget: BudgetManager, vault: Vault) -> None:
        self._cfg        = config
        self._budget     = budget
        self._vault      = vault
        self._lanes:     dict[str, LaneQueue] = {}
        self._adapters:  list[Any] = []
        self._ws_clients: set[Any] = set()
        self._start_time: float = 0.0
        self._bg_tasks:  list[asyncio.Task] = []
        self._agent_loop: Optional[Any] = None
        # Lock guards lazy agent-loop initialization (first message triggers it)
        self._agent_loop_lock: asyncio.Lock = asyncio.Lock()
        self._agent_loop_initializing: bool = False
        # Node manager for remote device capability registration
        self._nodes: NodeManager = NodeManager()
        # Heartbeat monitor (set in start())
        self._heartbeat_monitor: Optional[HeartbeatMonitor] = None

    def register_adapter(self, adapter: Any) -> None:
        """Register a channel adapter (must expose start/stop/send)."""
        adapter.gateway = self
        self._adapters.append(adapter)
        logger.info("Adapter registered: %s", type(adapter).__name__)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start adapters, WebSocket server, and cron scheduler."""
        self._start_time = time.monotonic()
        # NOTE: Agent loop is initialized lazily on first message (_ensure_agent_loop),
        # NOT here at startup.  sentence_transformers/PyTorch import takes 15-30s and
        # holds the GIL even inside run_in_executor, which would prevent aiohttp from
        # servicing /health requests.  Lazy init means the HTTP server is immediately
        # responsive and the heavy import only happens when the first chat message arrives.
        for adapter in self._adapters:
            # Start adapters as background tasks to avoid blocking the event loop
            # (e.g. Telegram start_polling makes network calls that can stall aiohttp)
            self._bg_tasks.append(
                asyncio.create_task(
                    self._start_adapter(adapter),
                    name=f"adapter-{type(adapter).__name__}",
                )
            )
        self._bg_tasks.append(asyncio.create_task(self._run_websocket_server(), name="websocket-server"))
        self._bg_tasks.append(asyncio.create_task(self._run_cron_scheduler(), name="cron-scheduler"))
        # Heartbeat monitor — checks HEARTBEAT.md for every agent on a schedule
        hb_monitor = HeartbeatMonitor(self, _CATO_DIR)
        self._bg_tasks.append(asyncio.create_task(hb_monitor.run_forever(), name="heartbeat-monitor"))
        self._heartbeat_monitor = hb_monitor
        # Node keepalive pinger — proactively pings registered nodes so stale ones are evicted
        self._bg_tasks.append(asyncio.create_task(self._nodes.run_ping_loop(), name="node-pinger"))
        logger.info("Gateway started — ws://%s:%d", _WS_HOST, _WS_PORT)

    async def _start_adapter(self, adapter: Any) -> None:
        """Start a single adapter with a 30s timeout, logging any errors."""
        try:
            await asyncio.wait_for(adapter.start(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("Adapter %s timed out during start (>30s) — skipping", type(adapter).__name__)
        except Exception as exc:
            logger.error("Adapter %s failed to start: %s", type(adapter).__name__, exc)

    async def stop(self) -> None:
        """Drain lane queues, stop adapters, cancel background tasks."""
        await asyncio.gather(*(lane.stop() for lane in self._lanes.values()), return_exceptions=True)
        for adapter in self._adapters:
            try:
                await adapter.stop()
            except Exception as exc:
                logger.warning("Adapter stop error: %s", exc)
        for t in self._bg_tasks:
            t.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        logger.info("Gateway stopped.")

    # ------------------------------------------------------------------
    # Public ingestion / dispatch
    # ------------------------------------------------------------------

    async def ingest(self, session_id: str, message: str, channel: str,
                     agent_id: str = "") -> None:
        """Called by adapters when a user message arrives. Routes to lane queue."""
        lane = self._get_or_create_lane(session_id)
        await lane.enqueue({
            "session_id": session_id,
            "message":    message,
            "channel":    channel,
            "agent_id":   agent_id or self._cfg.agent_name,
        })

    async def send(self, session_id: str, text: str, channel: str) -> None:
        """Called by agent loop to deliver a response to the originating channel."""
        if channel in ("web", "cron", "heartbeat"):
            await self._ws_broadcast({
                "type": "response", "session_id": session_id,
                "text": text, "cost_footer": self._budget.format_footer(),
                "channel": channel,
            })
            return
        for adapter in self._adapters:
            if adapter.channel_name == channel.lower():
                try:
                    await adapter.send(session_id, text)
                except Exception as exc:
                    logger.error("Adapter send error (%s): %s", channel, exc)
                return
        logger.warning("No adapter for channel=%s", channel)

    # ------------------------------------------------------------------
    # Internal task processing
    # ------------------------------------------------------------------

    async def _process_task(self, task: dict) -> None:
        session_id = task["session_id"]
        channel    = task["channel"]
        agent_id   = task.get("agent_id", self._cfg.agent_name)
        try:
            # Lazy-init: build agent loop on first message (avoids GIL block at startup)
            await self._ensure_agent_loop()
            if self._agent_loop is None:
                await self.send(session_id, "[error: agent loop failed to initialise]", channel)
                return
            result = await self._agent_loop.run(
                session_id=session_id,
                message=task["message"],
                agent_id=agent_id,
            )
            text, footer = result if isinstance(result, tuple) else (str(result), "")
            await self.send(session_id, f"{text}\n\n{footer}".strip(), channel)
        except BudgetExceeded as exc:
            await self.send(session_id, f"Budget cap reached: {exc}", channel)
        except Exception as exc:
            logger.error("session=%s processing error: %s", session_id, exc, exc_info=True)
            await self.send(session_id, f"[internal error: {exc}]", channel)

    # ------------------------------------------------------------------
    # Lane management
    # ------------------------------------------------------------------

    def _get_or_create_lane(self, session_id: str) -> LaneQueue:
        # Safe: asyncio event loop is single-threaded. No await between check and insert.
        # IMPORTANT: Never call this from run_in_executor() or a thread pool.
        if session_id not in self._lanes:
            lane = LaneQueue(session_id, self)
            lane.start()
            self._lanes[session_id] = lane
        return self._lanes[session_id]

    # ------------------------------------------------------------------
    # WebSocket server  (ws://127.0.0.1:18789)
    # ------------------------------------------------------------------

    async def _run_websocket_server(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("websockets not installed — WebSocket server disabled")
            return

        async def handler(ws: Any, path: str = "/") -> None:
            self._ws_clients.add(ws)
            try:
                async for raw in ws:
                    await self._handle_ws_message(ws, raw)
            except Exception:
                pass
            finally:
                self._ws_clients.discard(ws)
                self._nodes.remove_by_ws(ws)

        ws_port = getattr(self._cfg, "webchat_port", None) or _WS_PORT
        ws_port = ws_port + 1  # HTTP uses webchat_port, WS uses webchat_port+1
        try:
            async with websockets.serve(handler, _WS_HOST, ws_port):
                await asyncio.Future()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("WebSocket server error: %s", exc)

    async def _handle_ws_message(self, ws: Any, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(json.dumps({"type": "error", "text": "invalid JSON"}))
            return
        msg_type = data.get("type", "message")

        # Route node-protocol messages to NodeManager
        if msg_type.startswith("node_"):
            reply = await self._nodes.handle_message(ws, data)
            if reply is not None:
                await ws.send(json.dumps(reply))
            return

        if msg_type == "health":
            await ws.send(json.dumps({
                "type": "health", "status": "ok",
                "sessions": len(self._lanes),
                "uptime": int(time.monotonic() - self._start_time),
            }))
        elif msg_type == "message":
            text = data.get("text", "").strip()
            if text:
                await self.ingest(
                    data.get("session_id", "web-default"),
                    text,
                    data.get("channel", "web"),
                    data.get("agent_id", self._cfg.agent_name),
                )
        elif msg_type == "set_vault_key":
            # Onboarding wizard: save an API key into the vault
            vault_key = data.get("vault_key", "").strip()
            value     = data.get("value", "").strip()
            if vault_key and value and self._vault is not None:
                try:
                    self._vault.set(vault_key, value)
                    logger.info("Vault key saved via UI: %s", vault_key)
                    await ws.send(json.dumps({"type": "vault_key_saved", "vault_key": vault_key}))
                except Exception as exc:
                    await ws.send(json.dumps({"type": "error", "text": f"vault save failed: {exc}"}))
            else:
                await ws.send(json.dumps({"type": "error", "text": "vault_key and value required"}))
        else:
            await ws.send(json.dumps({"type": "error", "text": f"unknown type: {msg_type}"}))

    def register_websocket(self, ws: Any) -> None:
        """Register a WebSocket client (called by ui/server.py)."""
        self._ws_clients.add(ws)

    def unregister_websocket(self, ws: Any) -> None:
        """Unregister a WebSocket client on disconnect."""
        self._ws_clients.discard(ws)

    async def handle_ws_message(self, ws: Any, raw: str) -> None:
        """Handle an incoming WebSocket message (called by ui/server.py)."""
        await self._handle_ws_message(ws, raw)

    async def _ws_broadcast(self, payload: dict) -> None:
        if not self._ws_clients:
            return
        raw = json.dumps(payload)
        dead: set = set()
        for ws in list(self._ws_clients):
            try:
                await ws.send(raw)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ------------------------------------------------------------------
    # Cron scheduler
    # ------------------------------------------------------------------

    async def _run_cron_scheduler(self) -> None:
        """
        Poll CRONS.json for all agents every 60 s and fire due tasks.

        CRONS.json format: [{schedule, prompt, session_id, agent_id, announce}]
        """
        try:
            from croniter import croniter
        except ImportError:
            logger.warning("croniter not installed — cron scheduler disabled")
            return

        logger.info("Cron scheduler started")
        fired: dict[str, float] = {}   # key → last fire timestamp

        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                # Evict stale keys older than 2 minutes
                fired = {k: v for k, v in fired.items() if now - v < 120}

                if not _CATO_DIR.exists():
                    continue

                agents_root = _CATO_DIR / "agents"
                if not agents_root.is_dir():
                    continue
                for agent_dir in agents_root.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    crons_path = agent_dir / "CRONS.json"
                    if not crons_path.exists():
                        continue
                    crons = await self._load_crons(crons_path)
                    for entry in crons:
                        schedule   = entry.get("schedule", "")
                        prompt     = entry.get("prompt", "")
                        session_id = entry.get("session_id", f"cron-{agent_dir.name}")
                        e_agent    = entry.get("agent_id", agent_dir.name)
                        announce   = entry.get("announce", False)
                        if not schedule or not prompt:
                            continue
                        try:
                            next_ts = croniter(schedule, now - 60).get_next(float)
                        except Exception as exc:
                            logger.warning("Bad cron '%s': %s", schedule, exc)
                            continue
                        fire_key = f"{agent_dir.name}:{schedule}:{int(next_ts // 60)}"
                        if fire_key in fired or now < next_ts:
                            continue
                        fired[fire_key] = now
                        logger.info("Cron firing: agent=%s schedule=%s", agent_dir.name, schedule)
                        if announce:
                            await self.send(session_id, f"[cron] Starting: {prompt}", "web")
                        await self.ingest(session_id, prompt, "cron", e_agent)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Cron scheduler error: %s", exc, exc_info=True)

    async def _load_crons(self, path: Path) -> list[dict]:
        try:
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, path.read_text, "utf-8")
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
            return []

    # ------------------------------------------------------------------
    # Agent loop factory (lazy import avoids circular deps)
    # ------------------------------------------------------------------

    async def _ensure_agent_loop(self) -> None:
        """Lazily initialize the agent loop on first use.

        Uses an asyncio.Lock so concurrent messages don't double-initialize.
        The GIL-heavy sentence_transformers import runs in a thread via
        run_in_executor; since this only executes on first message (not at
        startup), the HTTP server is already fully responsive before we get here.
        """
        if self._agent_loop is not None:
            return
        async with self._agent_loop_lock:
            # Double-check after acquiring lock
            if self._agent_loop is not None:
                return
            logger.info("Initializing agent loop (first message) ...")
            try:
                self._agent_loop = await self._build_agent_loop()
                logger.info("Agent loop ready")
            except Exception as exc:
                logger.error("Agent loop init failed: %s", exc, exc_info=True)

    async def _init_agent_loop(self) -> None:
        """Legacy entry point kept for compatibility — delegates to _ensure_agent_loop."""
        await self._ensure_agent_loop()

    async def _build_agent_loop(self) -> Any:
        # Run in executor to avoid blocking the event loop during slow imports
        # (sentence_transformers, torch, etc. can take 10-30s to import)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._build_agent_loop_sync)

    def _build_agent_loop_sync(self) -> Any:
        from .agent_loop import AgentLoop
        from .core.context_builder import ContextBuilder
        from .core.memory import MemorySystem
        from .tools import register_all_tools
        memory = MemorySystem(agent_id=self._cfg.agent_name)
        ctx    = ContextBuilder(max_tokens=self._cfg.context_budget_tokens)
        loop = AgentLoop(
            config=self._cfg, budget=self._budget, vault=self._vault,
            memory=memory, context_builder=ctx,
        )
        register_all_tools(loop)
        return loop
