"""
cato/ui/server.py — aiohttp server that serves the Cato dashboard.

Mounts:
  GET /                              → dashboard.html (SPA)
  GET /health                        → JSON health payload
  GET /ws                            → WebSocket upgrade (delegates to gateway)
  POST /config                       → Save config (stub; gateway wires real handler)
  GET /api/vault/keys                → List vault key names
  POST /api/vault/set                → Store a vault key
  DELETE /api/vault/delete           → Delete a vault key
  GET /api/sessions                  → List active sessions with metadata
  DELETE /api/sessions/{session_id}  → Kill a session
  GET /api/skills                    → List installed skills
  GET /api/skills/{name}/content     → Get SKILL.md content for a skill
  GET /api/cron/jobs                 → List cron jobs
  POST /api/cron/jobs                → Create or update a cron job
  DELETE /api/cron/jobs/{name}       → Delete a cron job
  POST /api/cron/jobs/{name}/toggle  → Enable/disable a cron job
  POST /api/cron/jobs/{name}/run     → Manually trigger a cron job now
  GET /api/budget/summary            → Budget status (spend, caps, pct remaining)
  GET /api/usage/summary             → Usage stats (calls, tokens, model breakdown)
  GET /api/logs                      → Recent daemon log entries
  GET /api/audit/entries             → Audit log entries (filterable)
  POST /api/audit/verify             → Verify audit chain integrity
  GET /api/config                    → Get current config (registered via register_all_routes)
  PATCH /api/config                  → Patch config fields (registered via register_all_routes)
  GET /coding-agent                  → coding_agent.html entry page
  GET /coding-agent/{task_id}        → coding_agent.html SPA (task view)
  POST /api/coding-agent/invoke      → Create task, returns task_id
  GET /api/coding-agent/{tid}        → Task metadata
  GET /ws/coding-agent/{tid}         → WebSocket streaming for task
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web, WSMsgType

logger = logging.getLogger(__name__)

_DASHBOARD      = Path(__file__).parent / "dashboard.html"
_CODING_AGENT   = Path(__file__).parent / "coding_agent.html"
_START_TIME     = time.monotonic()


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

    async def vault_list_keys(request: web.Request) -> web.Response:
        """GET /api/vault/keys — return list of key names stored in the vault (no values)."""
        try:
            vault = gateway._vault if gateway is not None else None
            if vault is None:
                return web.json_response([])
            keys = vault.list_keys() if hasattr(vault, "list_keys") else []
            return web.json_response(keys)
        except Exception as exc:
            logger.error("vault_list_keys error: %s", exc)
            return web.json_response([])

    async def vault_set_key(request: web.Request) -> web.Response:
        """POST /api/vault/set — store a key in the vault. Body: {key, value}."""
        try:
            body = await request.json()
            k = str(body.get("key", "")).strip()
            v = str(body.get("value", "")).strip()
            if not k or not v:
                return web.json_response({"status": "error", "message": "key and value required"}, status=400)
            vault = gateway._vault if gateway is not None else None
            if vault is None:
                return web.json_response({"status": "error", "message": "vault unavailable"}, status=503)
            vault.set(k, v)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("vault_set_key error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def vault_delete_key(request: web.Request) -> web.Response:
        """DELETE /api/vault/delete — remove a key from the vault. Body: {key}."""
        try:
            body = await request.json()
            k = str(body.get("key", "")).strip()
            if not k:
                return web.json_response({"status": "error", "message": "key required"}, status=400)
            vault = gateway._vault if gateway is not None else None
            if vault is None:
                return web.json_response({"status": "error", "message": "vault unavailable"}, status=503)
            if hasattr(vault, "delete"):
                vault.delete(k)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("vault_delete_key error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Sessions                                                             #
    # ------------------------------------------------------------------ #

    async def list_sessions(request: web.Request) -> web.Response:
        """GET /api/sessions — list active lane sessions."""
        try:
            if gateway is None:
                return web.json_response([])
            sessions = []
            for sid, lane in gateway._lanes.items():
                queue_depth = lane._queue.qsize() if hasattr(lane, "_queue") else 0
                running = lane._task is not None and not lane._task.done() if hasattr(lane, "_task") else False
                sessions.append({
                    "session_id": sid,
                    "queue_depth": queue_depth,
                    "running": running,
                })
            return web.json_response(sessions)
        except Exception as exc:
            logger.error("list_sessions error: %s", exc)
            return web.json_response([], status=500)

    async def kill_session(request: web.Request) -> web.Response:
        """DELETE /api/sessions/{session_id} — stop a session lane."""
        session_id = request.match_info.get("session_id", "")
        try:
            if gateway is None:
                return web.json_response({"status": "error", "message": "gateway unavailable"}, status=503)
            lane = gateway._lanes.get(session_id)
            if lane is None:
                return web.json_response({"status": "error", "message": "session not found"}, status=404)
            import asyncio as _asyncio
            _asyncio.create_task(lane.stop())
            gateway._lanes.pop(session_id, None)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("kill_session error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Skills                                                               #
    # ------------------------------------------------------------------ #

    async def list_skills(request: web.Request) -> web.Response:
        """GET /api/skills — list installed skills with metadata."""
        try:
            if gateway is None:
                return web.json_response([])
            skills = gateway._list_skills()
            # Don't include full content in list — only name, description, version, dir
            result = [
                {"name": s["name"], "description": s["description"],
                 "version": s["version"], "dir": s["dir"]}
                for s in skills
            ]
            return web.json_response(result)
        except Exception as exc:
            logger.error("list_skills error: %s", exc)
            return web.json_response([], status=500)

    async def get_skill_content(request: web.Request) -> web.Response:
        """GET /api/skills/{name}/content — return SKILL.md content for a skill."""
        name = request.match_info.get("name", "")
        try:
            if gateway is None:
                return web.json_response({"content": ""})
            skills = gateway._list_skills()
            for s in skills:
                if s["dir"] == name or s["name"] == name:
                    return web.json_response({"content": s.get("content", ""), "name": s["name"]})
            return web.json_response({"status": "error", "message": "skill not found"}, status=404)
        except Exception as exc:
            logger.error("get_skill_content error: %s", exc)
            return web.json_response({"content": ""}, status=500)

    # ------------------------------------------------------------------ #
    # Cron jobs                                                            #
    # ------------------------------------------------------------------ #

    async def list_cron_jobs(request: web.Request) -> web.Response:
        """GET /api/cron/jobs — list all cron schedules."""
        try:
            from cato.core.schedule_manager import load_all_schedules
            schedules = load_all_schedules()
            return web.json_response([s.to_dict() for s in schedules])
        except Exception as exc:
            logger.error("list_cron_jobs error: %s", exc)
            return web.json_response([], status=500)

    async def create_cron_job(request: web.Request) -> web.Response:
        """POST /api/cron/jobs — create or update a cron schedule."""
        try:
            from cato.core.schedule_manager import Schedule
            body = await request.json()
            sched = Schedule.from_dict(body)
            sched.save()
            return web.json_response({"status": "ok", "name": sched.name})
        except Exception as exc:
            logger.error("create_cron_job error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def delete_cron_job(request: web.Request) -> web.Response:
        """DELETE /api/cron/jobs/{name} — remove a schedule."""
        name = request.match_info.get("name", "")
        try:
            from cato.core.schedule_manager import delete_schedule
            ok = delete_schedule(name)
            if ok:
                return web.json_response({"status": "ok"})
            return web.json_response({"status": "error", "message": "not found"}, status=404)
        except Exception as exc:
            logger.error("delete_cron_job error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def toggle_cron_job(request: web.Request) -> web.Response:
        """POST /api/cron/jobs/{name}/toggle — enable or disable a schedule."""
        name = request.match_info.get("name", "")
        try:
            from cato.core.schedule_manager import toggle_schedule
            body = await request.json()
            enabled = bool(body.get("enabled", True))
            ok = toggle_schedule(name, enabled)
            if ok:
                return web.json_response({"status": "ok", "enabled": enabled})
            return web.json_response({"status": "error", "message": "not found"}, status=404)
        except Exception as exc:
            logger.error("toggle_cron_job error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def run_cron_job_now(request: web.Request) -> web.Response:
        """POST /api/cron/jobs/{name}/run — manually trigger a cron job.

        The cron scheduler runs as an inline coroutine (not a SchedulerDaemon),
        so manual trigger is implemented by reading the schedule from disk and
        injecting the prompt directly into the gateway lane queue.
        """
        name = request.match_info.get("name", "")
        try:
            if gateway is None:
                return web.json_response({"status": "error", "message": "gateway unavailable"}, status=503)
            from cato.core.schedule_manager import load_all_schedules
            schedules = load_all_schedules()
            sched = next((s for s in schedules if s.name == name), None)
            if sched is None:
                return web.json_response({"status": "error", "message": f"job '{name}' not found"}, status=404)
            session_id = f"cron-manual-{name}"
            prompt = sched.skill or name
            await gateway.ingest(session_id, str(prompt), "cron", "")
            return web.json_response({"status": "ok", "message": f"Job '{name}' triggered"})
        except Exception as exc:
            logger.error("run_cron_job_now error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Budget                                                               #
    # ------------------------------------------------------------------ #

    async def budget_summary(request: web.Request) -> web.Response:
        """GET /api/budget/summary — current spend, caps, pct remaining."""
        try:
            if gateway is None:
                return web.json_response({"session_spend": 0, "session_cap": 1.0,
                                          "monthly_spend": 0, "monthly_cap": 20.0,
                                          "session_pct_remaining": 100, "monthly_pct_remaining": 100,
                                          "monthly_calls": 0, "total_spend_all_time": 0})
            status = gateway._budget.get_status()
            return web.json_response(status)
        except Exception as exc:
            logger.error("budget_summary error: %s", exc)
            return web.json_response({}, status=500)

    # ------------------------------------------------------------------ #
    # Usage                                                                #
    # ------------------------------------------------------------------ #

    async def usage_summary(request: web.Request) -> web.Response:
        """GET /api/usage/summary — token usage and model distribution."""
        try:
            from cato.orchestrator.metrics import get_token_report
            report = get_token_report()
            return web.json_response(report)
        except Exception as exc:
            logger.error("usage_summary error: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Logs                                                                 #
    # ------------------------------------------------------------------ #

    _log_buffer: list[dict] = []

    class _BufferHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            _log_buffer.append({
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            })
            if len(_log_buffer) > 500:
                del _log_buffer[:-500]

    _buf_handler = _BufferHandler()
    _buf_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    logging.getLogger("cato").addHandler(_buf_handler)

    async def get_logs(request: web.Request) -> web.Response:
        """GET /api/logs?limit=100 — return recent log entries."""
        try:
            limit = int(request.rel_url.query.get("limit", "100"))
            level_filter = request.rel_url.query.get("level", "").upper()
            entries = _log_buffer[-limit:]
            if level_filter:
                entries = [e for e in entries if e["level"] == level_filter]
            return web.json_response(entries)
        except Exception as exc:
            logger.error("get_logs error: %s", exc)
            return web.json_response([], status=500)

    # ------------------------------------------------------------------ #
    # Audit log                                                            #
    # ------------------------------------------------------------------ #

    async def get_audit_entries(request: web.Request) -> web.Response:
        """GET /api/audit/entries — return audit log entries with optional filters.

        Runs synchronous SQLite I/O in a thread via run_in_executor to avoid
        blocking the aiohttp event loop.
        """
        try:
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog
            limit = int(request.rel_url.query.get("limit", "200"))
            session_filter = request.rel_url.query.get("session_id", "")
            action_filter = request.rel_url.query.get("action_type", "")

            def _fetch() -> list:
                audit = AuditLog()
                audit.connect()
                assert audit._conn is not None
                q = "SELECT id, session_id, action_type, tool_name, cost_cents, error, timestamp, prev_hash, row_hash FROM audit_log"
                params: list = []
                clauses: list[str] = []
                if session_filter:
                    clauses.append("session_id = ?")
                    params.append(session_filter)
                if action_filter:
                    clauses.append("action_type = ?")
                    params.append(action_filter)
                if clauses:
                    q += " WHERE " + " AND ".join(clauses)
                q += " ORDER BY id DESC LIMIT ?"
                params.append(limit)
                rows = audit._conn.execute(q, params).fetchall()
                result = [dict(r) for r in rows]
                audit.close()
                return result

            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _fetch)
            return web.json_response(result)
        except Exception as exc:
            logger.error("get_audit_entries error: %s", exc)
            return web.json_response([], status=500)

    async def verify_audit_chain(request: web.Request) -> web.Response:
        """POST /api/audit/verify — verify chain integrity for a session or all.

        Runs synchronous SQLite I/O in a thread via run_in_executor to avoid
        blocking the aiohttp event loop.
        """
        try:
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog
            body = await request.json()
            session_id = str(body.get("session_id", ""))

            def _verify() -> dict:
                audit = AuditLog()
                audit.connect()
                if session_id:
                    ok = audit.verify_chain(session_id)
                    audit.close()
                    return {"ok": ok, "session_id": session_id}
                assert audit._conn is not None
                sessions = [r[0] for r in audit._conn.execute(
                    "SELECT DISTINCT session_id FROM audit_log"
                ).fetchall()]
                results = {}
                for sid in sessions:
                    results[sid] = audit.verify_chain(sid)
                audit.close()
                return {"ok": all(results.values()) if results else True, "sessions": results}

            loop = _asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _verify)
            return web.json_response(data)
        except Exception as exc:
            logger.error("verify_audit_chain error: %s", exc)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

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
    # Coding Agent routes                                                  #
    # ------------------------------------------------------------------ #

    async def serve_coding_agent(request: web.Request) -> web.FileResponse:
        """Serve the coding agent SPA for /coding-agent and /coding-agent/{task_id}."""
        return web.FileResponse(_CODING_AGENT)

    # ------------------------------------------------------------------ #
    # Router                                                               #
    # ------------------------------------------------------------------ #

    app.router.add_get("/",                              serve_dashboard)
    app.router.add_get("/health",                        health)
    app.router.add_get("/ws",                            websocket_handler)
    app.router.add_post("/config",                       save_config)
    # Vault
    app.router.add_get("/api/vault/keys",                vault_list_keys)
    app.router.add_post("/api/vault/set",                vault_set_key)
    app.router.add_delete("/api/vault/delete",           vault_delete_key)
    # Sessions
    app.router.add_get("/api/sessions",                  list_sessions)
    app.router.add_delete("/api/sessions/{session_id}",  kill_session)
    # Skills
    app.router.add_get("/api/skills",                    list_skills)
    app.router.add_get("/api/skills/{name}/content",     get_skill_content)
    # Cron
    app.router.add_get("/api/cron/jobs",                 list_cron_jobs)
    app.router.add_post("/api/cron/jobs",                create_cron_job)
    app.router.add_delete("/api/cron/jobs/{name}",       delete_cron_job)
    app.router.add_post("/api/cron/jobs/{name}/toggle",  toggle_cron_job)
    app.router.add_post("/api/cron/jobs/{name}/run",     run_cron_job_now)
    # Budget
    app.router.add_get("/api/budget/summary",            budget_summary)
    # Usage
    app.router.add_get("/api/usage/summary",             usage_summary)
    # Logs
    app.router.add_get("/api/logs",                      get_logs)
    # Audit
    app.router.add_get("/api/audit/entries",             get_audit_entries)
    app.router.add_post("/api/audit/verify",             verify_audit_chain)

    # Coding agent UI routes
    app.router.add_get("/coding-agent",           serve_coding_agent)
    app.router.add_get("/coding-agent/{task_id}", serve_coding_agent)

    # Register coding agent API + WebSocket routes
    try:
        from cato.api.routes import register_all_routes
        register_all_routes(app)
        logger.info("Coding agent API routes registered")
    except ImportError as exc:
        logger.warning("Could not register coding agent routes: %s", exc)

    # ------------------------------------------------------------------ #
    # CLI process pool lifecycle                                          #
    # ------------------------------------------------------------------ #

    async def _start_cli_pool(app: web.Application) -> None:
        """Warm up persistent CLI processes on server start."""
        try:
            from cato.orchestrator.cli_process_pool import get_pool
            pool = get_pool()
            await pool.start_all()
            logger.info("CLI process pool started")
        except Exception as exc:
            logger.warning("CLI process pool failed to start: %s", exc)

    async def _stop_cli_pool(app: web.Application) -> None:
        """Shut down persistent CLI processes on server stop."""
        try:
            from cato.orchestrator.cli_process_pool import get_pool
            pool = get_pool()
            await pool.stop_all()
            logger.info("CLI process pool stopped")
        except Exception as exc:
            logger.warning("CLI process pool failed to stop: %s", exc)

    app.on_startup.append(_start_cli_pool)
    app.on_cleanup.append(_stop_cli_pool)

    logger.info("UI app created — dashboard: %s", _DASHBOARD)
    return app
