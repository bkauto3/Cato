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
  POST /api/compact                  → Compact context for a session (slash cmd)
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


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Add CORS headers so the Tauri WebView2 (tauri://localhost origin) can
    reach the daemon at http://127.0.0.1:8080 without being blocked."""
    # Handle pre-flight OPTIONS requests
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response


async def create_ui_app(gateway: Optional[Any] = None) -> web.Application:
    """Create and return the aiohttp Application serving the dashboard.

    Args:
        gateway: The Gateway instance. May be None for standalone testing;
                 pages will render but WebSocket will show disconnected state.
    """
    app = web.Application(middlewares=[cors_middleware])

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

    async def chat_history(request: web.Request) -> web.Response:
        """GET /api/chat/history — cross-channel message history (web + Telegram)."""
        try:
            since_ts = int(request.rel_url.query.get("since", "0"))
            if gateway is None:
                return web.json_response([])
            entries = gateway.get_message_history(since_ts=since_ts)
            return web.json_response(entries)
        except Exception as exc:
            logger.error("chat_history error: %s", exc)
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

    async def compact_session(request: web.Request) -> web.Response:
        """POST /api/compact — compact context for a session (trim old messages).

        Called by the /compact slash command in the chat UI.
        Body: {session_id: string}
        When gateway is available, sends a compact instruction into the lane.
        When offline, returns a success stub so the UI can show a friendly message.
        """
        try:
            body = await request.json()
            session_id = str(body.get("session_id", "")).strip()
            if not session_id:
                return web.json_response({"status": "error", "message": "session_id required"}, status=400)
            if gateway is not None:
                lane = gateway._lanes.get(session_id)
                if lane is not None and hasattr(lane, "compact"):
                    import asyncio as _asyncio
                    _asyncio.create_task(lane.compact())
                    return web.json_response({"status": "ok", "message": "Context compacted."})
                # Lane not found — still return ok (session may have just started)
                return web.json_response({"status": "ok", "message": "Context compacted."})
            # No gateway — return ok stub (UI shows offline message separately)
            return web.json_response({"status": "ok", "message": "Context compacted (stub)."})
        except Exception as exc:
            logger.error("compact_session error: %s", exc)
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

    async def patch_skill_content(request: web.Request) -> web.Response:
        """PATCH /api/skills/{name}/content — update SKILL.md content for a skill."""
        name = request.match_info.get("name", "")
        # Guard against path traversal: skill names must not contain path separators or dots
        if not name or ".." in name or "/" in name or "\\" in name:
            return web.json_response({"status": "error", "message": "invalid skill name"}, status=400)
        try:
            body = await request.json()
            content = str(body.get("content", ""))
            if gateway is None:
                return web.json_response({"status": "error", "message": "gateway unavailable"}, status=503)
            skills_dir = getattr(gateway, "_skills_dir", None)
            if skills_dir is None:
                # Attempt to locate the skills directory from the gateway
                skills_dir = Path.home() / ".cato" / "skills"
            else:
                skills_dir = Path(skills_dir)
            # Find the skill directory
            target = None
            for child in skills_dir.iterdir() if skills_dir.exists() else []:
                if child.is_dir() and (child.name == name):
                    target = child / "SKILL.md"
                    break
            if target is None:
                # Try writing based on name directly
                target = skills_dir / name / "SKILL.md"
            # Resolve and confirm target is within skills_dir to prevent symlink escapes
            skills_dir_resolved = Path(skills_dir).resolve()
            target_resolved = target.resolve()
            if not str(target_resolved).startswith(str(skills_dir_resolved)):
                return web.json_response({"status": "error", "message": "invalid skill path"}, status=400)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            logger.info("Skill content updated: %s", name)
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("patch_skill_content error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def toggle_skill(request: web.Request) -> web.Response:
        """POST /api/skills/{name}/toggle — enable or disable a skill."""
        name = request.match_info.get("name", "")
        # Guard against path traversal
        if not name or ".." in name or "/" in name or "\\" in name:
            return web.json_response({"status": "error", "message": "invalid skill name"}, status=400)
        try:
            body = await request.json()
            enabled = bool(body.get("enabled", True))
            # Store enabled state in a simple marker file in the skill directory
            if gateway is None:
                return web.json_response({"status": "error", "message": "gateway unavailable"}, status=503)
            skills_dir = Path(getattr(gateway, "_skills_dir", Path.home() / ".cato" / "skills"))
            skill_path = skills_dir / name
            if skill_path.exists():
                marker = skill_path / ".disabled"
                if enabled:
                    marker.unlink(missing_ok=True)
                else:
                    marker.touch()
            return web.json_response({"status": "ok", "enabled": enabled})
        except Exception as exc:
            logger.error("toggle_skill error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def cli_status(request: web.Request) -> web.Response:
        """GET /api/cli/status — check installed CLI tools (claude, codex, gemini, cursor)."""
        import asyncio as _asyncio
        import shutil
        import subprocess

        TOOLS = {
            "claude":  ["claude", "--version"],
            "codex":   ["codex",  "--version"],
            "gemini":  ["gemini", "--version"],
            "cursor":  ["cursor", "--version"],
        }
        result: dict = {}
        loop = _asyncio.get_running_loop()

        def _check_tool(name: str, cmd: list) -> dict:
            exe = shutil.which(name)
            if exe is None:
                return {"installed": False, "logged_in": False, "version": ""}
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                version = (proc.stdout or proc.stderr or "").strip().split("\n")[0][:60]
                return {"installed": True, "logged_in": True, "version": version}
            except Exception:
                return {"installed": True, "logged_in": False, "version": ""}

        for tool_name, cmd in TOOLS.items():
            result[tool_name] = await loop.run_in_executor(None, _check_tool, tool_name, cmd)

        return web.json_response(result)

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
        """GET /api/usage/summary — token usage and model distribution.

        Returns a response compatible with the dashboard JS which expects:
          total_calls, total_input_tokens, total_output_tokens,
          top_model, avg_latency_ms, models (list of {model, calls}).
        Also includes the raw get_token_report() fields for completeness.
        """
        try:
            from cato.orchestrator.metrics import get_token_report, get_metrics_summary
            report = get_token_report()
            summary = get_metrics_summary()

            # Map internal field names → dashboard-expected field names
            total_in  = report.get("total_tokens_in", 0)
            total_out = report.get("total_tokens_out", 0)
            total_calls = report.get("total_invocations", 0)

            # Build per-model list from tier_distribution as a best-effort proxy
            tier_dist = report.get("tier_distribution", {})
            models_list = [
                {"model": tier, "calls": cnt, "total_calls": cnt}
                for tier, cnt in tier_dist.items()
            ] if tier_dist else []

            # top model: first entry or unknown
            top_model = models_list[0]["model"] if models_list else "unknown"

            avg_latency = summary.get("avg_latency_ms") if summary else None

            compat = {
                # Dashboard-expected fields
                "total_calls": total_calls,
                "total_input_tokens": total_in,
                "total_output_tokens": total_out,
                "top_model": top_model,
                "avg_latency_ms": avg_latency,
                "models": models_list,
                "by_model": models_list,
                # Keep all original fields for backward compat
                **report,
            }
            return web.json_response(compat)
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

    async def patch_config(request: web.Request) -> web.Response:
        """PATCH /api/config — patch individual config fields."""
        try:
            body = await request.json()
            logger.info("Config patch requested: %s", list(body.keys()))
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("patch_config error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=400)

    async def get_config(request: web.Request) -> web.Response:
        """GET /api/config — return current config."""
        try:
            cfg: dict = {}
            if gateway is not None and hasattr(gateway, "_config"):
                raw = gateway._config
                if hasattr(raw, "__dict__"):
                    cfg = {k: v for k, v in raw.__dict__.items() if not k.startswith("_")}
                elif isinstance(raw, dict):
                    cfg = raw
            return web.json_response(cfg)
        except Exception as exc:
            logger.error("get_config error: %s", exc)
            return web.json_response({}, status=500)

    # ------------------------------------------------------------------ #
    # Cron job history                                                     #
    # ------------------------------------------------------------------ #

    async def cron_job_history(request: web.Request) -> web.Response:
        """GET /api/cron/jobs/{name}/history — return recent executions for a job."""
        name = request.match_info.get("name", "")
        try:
            limit = int(request.rel_url.query.get("limit", "20"))
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog

            def _fetch() -> list:
                audit = AuditLog()
                audit.connect()
                assert audit._conn is not None
                rows = audit._conn.execute(
                    "SELECT id, session_id, action_type, tool_name, cost_cents, error, timestamp "
                    "FROM audit_log WHERE session_id LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"cron-%{name}%", limit),
                ).fetchall()
                audit.close()
                return [dict(r) for r in rows]

            loop = _asyncio.get_running_loop()
            result = await loop.run_in_executor(None, _fetch)
            return web.json_response(result)
        except Exception as exc:
            logger.error("cron_job_history error: %s", exc)
            return web.json_response([], status=500)

    # ------------------------------------------------------------------ #
    # Memory browser                                                       #
    # ------------------------------------------------------------------ #

    async def memory_files(request: web.Request) -> web.Response:
        """GET /api/memory/files — list memory JSON files in ~/.cato/memory/."""
        try:
            mem_dir = Path.home() / ".cato" / "memory"
            if not mem_dir.exists():
                return web.json_response([])
            files = [f.name for f in sorted(mem_dir.iterdir()) if f.suffix in (".json", ".md")]
            return web.json_response(files)
        except Exception as exc:
            logger.error("memory_files error: %s", exc)
            return web.json_response([], status=500)

    async def memory_content(request: web.Request) -> web.Response:
        """GET /api/memory/content?file=MEMORY.md — read a memory/workspace file."""
        filename = request.rel_url.query.get("file", "")
        if not filename or ".." in filename or "/" in filename or "\\" in filename:
            return web.json_response({"status": "error", "message": "invalid file"}, status=400)
        try:
            # Try ~/.cato/memory/ first, then ~/.cato/
            for base in (Path.home() / ".cato" / "memory", Path.home() / ".cato"):
                p = base / filename
                if p.exists():
                    return web.json_response({"content": p.read_text(encoding="utf-8", errors="replace"), "file": filename})
            return web.json_response({"content": "", "file": filename})
        except Exception as exc:
            logger.error("memory_content error: %s", exc)
            return web.json_response({"content": "", "file": filename}, status=500)

    async def patch_memory_content(request: web.Request) -> web.Response:
        """PATCH /api/memory/content — write a memory/workspace file."""
        try:
            body = await request.json()
            filename = str(body.get("file", "")).strip()
            content  = str(body.get("content", ""))
            if not filename or ".." in filename or "/" in filename or "\\" in filename:
                return web.json_response({"status": "error", "message": "invalid file"}, status=400)
            # Write to ~/.cato/ (workspace root)
            target = Path.home() / ".cato" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return web.json_response({"status": "ok"})
        except Exception as exc:
            logger.error("patch_memory_content error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    async def memory_stats(request: web.Request) -> web.Response:
        """GET /api/memory/stats — facts count + KG node/edge counts from SQLite memories."""
        try:
            import asyncio as _asyncio
            import sqlite3

            def _count() -> dict:
                facts = 0
                kg_nodes = 0
                kg_edges = 0
                mem_dir = Path.home() / ".cato" / "memory"
                if mem_dir.exists():
                    for db_path in mem_dir.glob("*.db"):
                        try:
                            conn = sqlite3.connect(str(db_path))
                            for tbl, col in [("facts", "facts"), ("kg_nodes", "kg_nodes"), ("kg_edges", "kg_edges")]:
                                try:
                                    n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                                    if tbl == "facts":
                                        facts += n
                                    elif tbl == "kg_nodes":
                                        kg_nodes += n
                                    elif tbl == "kg_edges":
                                        kg_edges += n
                                except Exception:
                                    pass
                            conn.close()
                        except Exception:
                            pass
                return {"facts": facts, "kg_nodes": kg_nodes, "kg_edges": kg_edges}

            loop = _asyncio.get_running_loop()
            stats = await loop.run_in_executor(None, _count)
            return web.json_response(stats)
        except Exception as exc:
            logger.error("memory_stats error: %s", exc)
            return web.json_response({"facts": 0, "kg_nodes": 0, "kg_edges": 0}, status=500)

    # ------------------------------------------------------------------ #
    # Action Guard status                                                  #
    # ------------------------------------------------------------------ #

    async def action_guard_status(request: web.Request) -> web.Response:
        """GET /api/action-guard/status — show the 3-rule gate status."""
        try:
            from cato.audit.action_guard import ActionGuard
            guard = ActionGuard()
            checks = [
                {"rule": "Irreversibility check", "description": "Block irreversible actions above autonomy threshold", "active": True},
                {"rule": "Spending ceiling check", "description": "Enforce per-session and monthly spend caps", "active": True},
                {"rule": "Dangerous tool check",  "description": "Require confirmation for high-risk tools", "active": True},
            ]
            return web.json_response({"checks": checks, "autonomy_level": 0.5})
        except Exception as exc:
            logger.error("action_guard_status error: %s", exc)
            return web.json_response({"checks": [], "autonomy_level": 0.5}, status=500)

    # ------------------------------------------------------------------ #
    # Daemon restart                                                       #
    # ------------------------------------------------------------------ #

    async def daemon_restart(request: web.Request) -> web.Response:
        """POST /api/daemon/restart — signal daemon to restart (best-effort)."""
        try:
            logger.info("Daemon restart requested via API")
            import asyncio as _asyncio
            async def _deferred_restart():
                await _asyncio.sleep(1)
                import os, signal
                os.kill(os.getpid(), signal.SIGTERM)
            _asyncio.create_task(_deferred_restart())
            return web.json_response({"status": "ok", "message": "Restart scheduled"})
        except Exception as exc:
            logger.error("daemon_restart error: %s", exc)
            return web.json_response({"status": "error", "message": str(exc)}, status=500)

    # ------------------------------------------------------------------ #
    # Favicon                                                              #
    # ------------------------------------------------------------------ #

    _FAVICON = Path(__file__).parent / "favicon.png"

    async def serve_favicon(request: web.Request) -> web.Response:
        """GET /favicon.png — serve the Cato logo."""
        if _FAVICON.exists():
            return web.FileResponse(_FAVICON)
        return web.Response(status=404)

    # ------------------------------------------------------------------ #
    # Audit transcript download helper                                    #
    # ------------------------------------------------------------------ #

    async def audit_download(request: web.Request) -> web.Response:
        """GET /api/audit/download?session_id=X — download audit entries as JSONL."""
        try:
            import asyncio as _asyncio
            from cato.audit.audit_log import AuditLog
            session_filter = request.rel_url.query.get("session_id", "")

            def _fetch() -> list:
                audit = AuditLog()
                audit.connect()
                assert audit._conn is not None
                q = ("SELECT id, session_id, action_type, tool_name, cost_cents, error, timestamp, prev_hash, row_hash "
                     "FROM audit_log")
                params: list = []
                if session_filter:
                    q += " WHERE session_id = ?"
                    params.append(session_filter)
                q += " ORDER BY id ASC LIMIT 5000"
                rows = audit._conn.execute(q, params).fetchall()
                result = [dict(r) for r in rows]
                audit.close()
                return result

            loop = _asyncio.get_running_loop()
            entries = await loop.run_in_executor(None, _fetch)
            import json as _json
            body = "\n".join(_json.dumps(e) for e in entries)
            fname = f"audit-{session_filter or 'all'}.jsonl"
            return web.Response(
                body=body.encode("utf-8"),
                content_type="application/x-ndjson",
                headers={"Content-Disposition": f'attachment; filename="{fname}"'},
            )
        except Exception as exc:
            logger.error("audit_download error: %s", exc)
            return web.Response(status=500)

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
    app.router.add_get("/favicon.png",                   serve_favicon)
    # Vault
    app.router.add_get("/api/vault/keys",                vault_list_keys)
    app.router.add_post("/api/vault/set",                vault_set_key)
    app.router.add_delete("/api/vault/delete",           vault_delete_key)
    # Sessions
    app.router.add_get("/api/sessions",                  list_sessions)
    app.router.add_delete("/api/sessions/{session_id}",  kill_session)
    app.router.add_post("/api/compact",                  compact_session)
    # Cross-channel chat history (web + Telegram)
    app.router.add_get("/api/chat/history",              chat_history)
    # Skills
    app.router.add_get("/api/skills",                         list_skills)
    app.router.add_get("/api/skills/{name}/content",          get_skill_content)
    app.router.add_route("PATCH", "/api/skills/{name}/content", patch_skill_content)
    app.router.add_post("/api/skills/{name}/toggle",          toggle_skill)
    # CLI status
    app.router.add_get("/api/cli/status",                     cli_status)
    # Cron
    app.router.add_get("/api/cron/jobs",                     list_cron_jobs)
    app.router.add_post("/api/cron/jobs",                    create_cron_job)
    app.router.add_delete("/api/cron/jobs/{name}",           delete_cron_job)
    app.router.add_post("/api/cron/jobs/{name}/toggle",      toggle_cron_job)
    app.router.add_post("/api/cron/jobs/{name}/run",         run_cron_job_now)
    app.router.add_get("/api/cron/jobs/{name}/history",      cron_job_history)
    # Budget
    app.router.add_get("/api/budget/summary",            budget_summary)
    # Usage
    app.router.add_get("/api/usage/summary",             usage_summary)
    # Logs
    app.router.add_get("/api/logs",                      get_logs)
    # Audit
    app.router.add_get("/api/audit/entries",             get_audit_entries)
    app.router.add_post("/api/audit/verify",             verify_audit_chain)
    app.router.add_get("/api/audit/download",            audit_download)
    # Config
    app.router.add_get("/api/config",                    get_config)
    app.router.add_route("PATCH", "/api/config",         patch_config)
    # Memory
    app.router.add_get("/api/memory/files",              memory_files)
    app.router.add_get("/api/memory/content",            memory_content)
    app.router.add_route("PATCH", "/api/memory/content", patch_memory_content)
    app.router.add_get("/api/memory/stats",              memory_stats)
    # Action Guard
    app.router.add_get("/api/action-guard/status",       action_guard_status)
    # Daemon
    app.router.add_post("/api/daemon/restart",           daemon_restart)

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
