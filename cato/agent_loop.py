"""
cato/agent_loop.py — Agentic message-processing loop for CATO.

Processes one inbound message per session:
  1. Build context (ContextBuilder)
  2. Retrieve memory chunks
  3. Check budget before every LLM call (BudgetManager.check_and_deduct)
  4. Call LLM via ModelRouter (SwarmSync if configured, else local)
  5. Parse tool calls and dispatch them
  6. Loop until final answer (max_planning_turns before forced answer)
  7. Persist JSONL transcript at ~/.cato/{agent_id}/sessions/{session_id}.jsonl
  8. Store final response in memory (memory.astore)
  9. Return (final_text, cost_footer)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .budget import BudgetExceeded, BudgetManager
from .config import CatoConfig
from .core.context_builder import ContextBuilder
from .core.memory import MemorySystem
from .router import ModelRouter
from .vault import Vault

logger = logging.getLogger(__name__)

_CATO_DIR       = Path.home() / ".cato"
_CHARS_PER_TOKEN = 4
_MAX_RETRIES    = 3
_RETRY_BASE_DELAY = 1.5  # seconds; doubles each retry


# ---------------------------------------------------------------------------
# Tool call model and registry
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


# Chunk 4 registers real handlers here via register_tool()
_TOOL_REGISTRY: dict[str, Callable] = {}


def register_tool(name: str, fn: Callable) -> None:
    """Register an async tool handler (called by Chunk 4 modules)."""
    _TOOL_REGISTRY[name] = fn


async def _dispatch_tool(call: ToolCall) -> str:
    handler = _TOOL_REGISTRY.get(call.name)
    if handler is None:
        return f"[tool:{call.name} not yet implemented — Chunk 4 pending]"
    try:
        return await handler(call.args)
    except Exception as exc:
        logger.error("Tool %s raised: %s", call.name, exc)
        return f"[tool:{call.name} error: {exc}]"


# ---------------------------------------------------------------------------
# Path sanitization helpers
# ---------------------------------------------------------------------------

def _sanitize_path_component(s: str) -> str:
    """Strip any character that isn't alphanumeric, dash, underscore, or dot."""
    return re.sub(r'[^a-zA-Z0-9_\-\.]', '_', s)[:64]


# ---------------------------------------------------------------------------
# JSONL transcript helpers
# ---------------------------------------------------------------------------

def _transcript_path(agent_id: str, session_id: str) -> Path:
    agent_id = _sanitize_path_component(agent_id)
    session_id = _sanitize_path_component(session_id)
    p = _CATO_DIR / agent_id / "sessions" / f"{session_id}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append_transcript(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=True) + "\n")


async def _aappend(path: Path, record: dict) -> None:
    await asyncio.get_running_loop().run_in_executor(None, _append_transcript, path, record)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Tool call parsing
# ---------------------------------------------------------------------------

_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)


def _parse_tool_calls_text(text: str) -> list[ToolCall]:
    """Extract <tool_call>{...}</tool_call> blocks embedded in streaming text."""
    calls: list[ToolCall] = []
    for m in _TOOL_CALL_RE.finditer(text):
        try:
            d = json.loads(m.group(1))
            calls.append(ToolCall(
                name=d.get("name", ""),
                args=d.get("args", d.get("arguments", {})),
            ))
        except json.JSONDecodeError:
            pass
    return calls


def _parse_tool_calls_openai(msg: dict) -> list[ToolCall]:
    """Parse OpenAI tool_calls / legacy function_call into ToolCall objects."""
    calls: list[ToolCall] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        calls.append(ToolCall(name=fn.get("name", ""), args=args, call_id=tc.get("id", "")))
    fc = msg.get("function_call")
    if fc:
        try:
            args = json.loads(fc.get("arguments", "{}"))
        except json.JSONDecodeError:
            args = {}
        calls.append(ToolCall(name=fc.get("name", ""), args=args))
    return calls


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------

class AgentLoop:
    """
    Processes one message per session.  Construct once; share across sessions.
    Each call to run() is isolated by session_id.
    """

    def __init__(
        self,
        config: CatoConfig,
        budget: BudgetManager,
        vault: Vault,
        memory: MemorySystem,
        context_builder: ContextBuilder,
    ) -> None:
        self._cfg = config
        self._budget = budget
        self._vault = vault
        self._memory = memory
        self._ctx = context_builder
        self._router = ModelRouter(
            vault=vault,
            preferred_model=config.default_model,
            swarmsync_api_url=config.swarmsync_api_url,
        )

    async def run(self, session_id: str, message: str, agent_id: str) -> tuple[str, str]:
        """
        Process *message* and return (final_text, cost_footer).

        Persists every turn to JSONL transcript.
        Raises BudgetExceeded if spend caps are breached.
        """
        tpath = _transcript_path(agent_id, session_id)
        workspace = _CATO_DIR / agent_id / "workspace"
        daily_log = _CATO_DIR / agent_id / "memory" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"

        memory_chunks = await self._memory.asearch(message, top_k=4)
        system_prompt = self._ctx.build_system_prompt(
            workspace_dir=workspace,
            memory_chunks=memory_chunks,
            daily_log_path=daily_log if daily_log.exists() else None,
        )

        ctx_tokens  = self._ctx.count_tokens(system_prompt)
        history_len = self._history_len(tpath)
        complexity  = self._router.score_task(message, ctx_tokens, history_len)

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(self._recent_turns(tpath, limit=20))
        messages.append({"role": "user", "content": message})

        # Model selection — delegate to SwarmSync when available
        swarm_key = (self._vault.get("SWARM_SYNC_API_KEY") or
                     self._vault.get("SWARMSYNC_API_KEY"))
        swarmsync_content: Optional[str] = None
        if self._cfg.swarmsync_enabled and swarm_key:
            model, swarmsync_content = await self._router._swarmsync_route(
                messages, swarm_key, complexity
            )
        else:
            model = self._router.select_model(complexity)

        logger.info("session=%s model=%s complexity=%.2f", session_id, model, complexity)

        await _aappend(tpath, {
            "ts": _now(), "role": "user",
            "content": message, "session_id": session_id,
        })

        # ---- Planning loop -----------------------------------------------
        planning_turns = 0
        final_text = ""
        total_cost = 0.0

        # If SwarmSync returned a full response, use it directly
        if swarmsync_content is not None:
            await _aappend(tpath, {
                "ts": _now(), "role": "assistant", "content": swarmsync_content,
                "tool_calls": [], "cost_usd": 0.0,
                "model": model, "session_id": session_id,
            })
            asyncio.create_task(
                self._memory.astore(f"Q: {message}\nA: {swarmsync_content}", source_file=session_id),
                name="memory-store",
            )
            return swarmsync_content, self._budget.format_footer()

        while True:
            if planning_turns >= self._cfg.max_planning_turns:
                messages.append({
                    "role": "system",
                    "content": "Provide your final answer now. No more tool calls.",
                })

            # Budget pre-flight using actual input token count
            try:
                input_tokens = self._ctx.count_tokens("\n".join(
                    m.get("content", "") for m in messages if isinstance(m.get("content"), str)
                ))
                await self._budget.check_and_deduct(model, input_tokens, input_tokens // 3)
                call_cost = self._budget._last_call_cost
            except BudgetExceeded:
                raise
            except Exception:
                call_cost = 0.0
            total_cost += call_cost

            force = planning_turns >= self._cfg.max_planning_turns
            text, tool_calls = await self._stream_collect(messages, model, force)

            await _aappend(tpath, {
                "ts": _now(), "role": "assistant", "content": text,
                "tool_calls": [{"name": tc.name, "args": tc.args} for tc in tool_calls],
                "cost_usd": call_cost, "model": model, "session_id": session_id,
            })
            messages.append({"role": "assistant", "content": text})

            if not tool_calls or force:
                final_text = text
                break

            planning_turns += 1
            for tc in tool_calls:
                result = await _dispatch_tool(tc)
                await _aappend(tpath, {
                    "ts": _now(), "role": "tool",
                    "tool_name": tc.name, "result": result, "session_id": session_id,
                })
                messages.append({"role": "user", "content": f"[tool result: {tc.name}]\n{result}"})

        asyncio.create_task(
            self._memory.astore(f"Q: {message}\nA: {final_text}", source_file=session_id),
            name="memory-store",
        )
        return final_text, self._budget.format_footer()

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def _stream_collect(
        self, messages: list[dict], model: str, force_text: bool = False
    ) -> tuple[str, list[ToolCall]]:
        """Stream from router; return (text, tool_calls) with retry on error."""
        delay = _RETRY_BASE_DELAY
        for attempt in range(_MAX_RETRIES):
            try:
                chunks: list[str] = []
                async for chunk in self._router.complete(messages, model, stream=True):
                    chunks.append(chunk)
                full = "".join(chunks)
                calls = [] if force_text else _parse_tool_calls_text(full)
                visible = _TOOL_CALL_RE.sub("", full).strip()
                return visible, calls
            except Exception as exc:
                if attempt < _MAX_RETRIES - 1:
                    logger.warning("LLM attempt %d failed: %s — retry in %.1fs", attempt + 1, exc, delay)
                    await asyncio.sleep(delay)
                    delay *= 2
                else:
                    logger.error("LLM failed after %d attempts: %s", _MAX_RETRIES, exc)
                    return f"[error: {exc}]", []
        return "", []

    # ------------------------------------------------------------------
    # Transcript helpers
    # ------------------------------------------------------------------

    def _history_len(self, tpath: Path) -> int:
        if not tpath.exists():
            return 0
        try:
            return sum(1 for _ in tpath.open("r", encoding="utf-8"))
        except OSError:
            return 0

    def _recent_turns(self, tpath: Path, limit: int = 20) -> list[dict]:
        if not tpath.exists():
            return []
        try:
            lines = tpath.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        msgs: list[dict] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("role") in ("user", "assistant"):
                msgs.append({"role": rec["role"], "content": rec.get("content", "")})
        return msgs
