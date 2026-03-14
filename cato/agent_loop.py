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

Compaction:
  - Triggered when history tokens > COMPACT_TOKEN_THRESHOLD (2500) or
    total turns > COMPACT_TURN_THRESHOLD (30).
  - Old turns are distilled via Distiller (heuristic, no LLM call) and
    stored in the distilled_summaries SQLite table.
  - The transcript is then truncated to the last HISTORY_WINDOW turns.
  - The distilled summary is injected into the system prompt via
    ContextBuilder.build_system_prompt(distilled_summary=...).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .audit import AuditLog
from .budget import BudgetExceeded, BudgetManager
from .config import CatoConfig
from .core.context_builder import ContextBuilder
from .core.memory import MemorySystem
from .platform import get_data_dir
from .router import ModelRouter
from .safety import SafetyGuard
from .vault import Vault

logger = logging.getLogger(__name__)

_CATO_DIR       = get_data_dir()
_CHARS_PER_TOKEN = 4
_MAX_RETRIES    = 3
_RETRY_BASE_DELAY = 1.5  # seconds; doubles each retry

# ---------------------------------------------------------------------------
# Compaction constants
# ---------------------------------------------------------------------------

# Number of recent turns kept live after compaction
HISTORY_WINDOW: int = 12
# Trigger compaction when history token cost exceeds this threshold
COMPACT_TOKEN_THRESHOLD: int = 2500
# Trigger compaction when total turn count exceeds this threshold
COMPACT_TURN_THRESHOLD: int = 30


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


def register_all_tools(register_tool_fn: Callable[[str, Any], None], config: Optional[Any] = None) -> None:
    """Public API: register all available tools via a provided register_tool function."""
    _register_web_search_tools()
    _register_python_executor_tools()
    _register_clawflow_tools()


# ---------------------------------------------------------------------------
# Web-Search-Plus tool registrations (Skill 6)
# ---------------------------------------------------------------------------

def _register_web_search_tools(vault: Any = None) -> None:
    """Register web.search / web.code / web.news / academic.* tool actions."""
    try:
        from .tools.web_search import WebSearchTool
    except ImportError:
        return

    tool = WebSearchTool(vault=vault)

    async def _web_search(args: dict) -> str:
        results = await tool.search(query=args.get("query", ""), query_type="general")
        return "\n".join(f"[{r.rank+1}] {r.title}\n    {r.url}\n    {r.snippet}" for r in results[:5])

    async def _web_code(args: dict) -> str:
        results = await tool.search(query=args.get("query", ""), query_type="code")
        return "\n".join(f"[{r.rank+1}] {r.title}\n    {r.url}\n    {r.snippet}" for r in results[:5])

    async def _web_news(args: dict) -> str:
        results = await tool.search(query=args.get("query", ""), query_type="news")
        return "\n".join(f"[{r.rank+1}] {r.title}\n    {r.url}\n    {r.snippet}" for r in results[:5])

    async def _academic_arxiv(args: dict) -> str:
        results = await tool._search_arxiv(args.get("query", ""))
        return "\n".join(f"[{r.rank+1}] {r.title}\n    {r.url}\n    {r.snippet}" for r in results[:5])

    async def _academic_semantic_scholar(args: dict) -> str:
        results = await tool._search_semantic_scholar(args.get("query", ""))
        return "\n".join(f"[{r.rank+1}] {r.title}\n    {r.url}\n    {r.snippet}" for r in results[:5])

    async def _academic_pubmed(args: dict) -> str:
        results = await tool._search_pubmed(args.get("query", ""))
        return "\n".join(f"[{r.rank+1}] {r.title}\n    {r.url}\n    {r.snippet}" for r in results[:5])

    register_tool("web.search", _web_search)
    register_tool("web.code", _web_code)
    register_tool("web.news", _web_news)
    register_tool("academic.arxiv", _academic_arxiv)
    register_tool("academic.semantic_scholar", _academic_semantic_scholar)
    register_tool("academic.pubmed", _academic_pubmed)


def _register_shell_tools() -> None:
    """Register shell.exec tool action — PowerShell and general shell execution."""
    try:
        from .tools.shell import ShellTool
    except ImportError:
        return

    tool = ShellTool()

    async def _shell_exec(args: dict) -> str:
        return await tool.execute(args)

    register_tool("shell.exec", _shell_exec)


def _register_python_executor_tools() -> None:
    """Register python.execute tool action (Skill 7)."""
    try:
        from .tools.python_executor import PythonExecutor, SandboxViolationError
    except ImportError:
        return

    executor = PythonExecutor()

    async def _python_execute(args: dict) -> str:
        code = args.get("code", "")
        timeout = float(args.get("timeout_sec", 30.0))
        try:
            result = await executor.execute(code, timeout_sec=timeout)
            parts = []
            if result.stdout:
                parts.append(f"stdout:\n{result.stdout}")
            if result.stderr:
                parts.append(f"stderr:\n{result.stderr}")
            parts.append(f"returncode: {result.returncode}")
            return "\n".join(parts)
        except SandboxViolationError as exc:
            return f"[sandbox violation: {exc}]"

    register_tool("python.execute", _python_execute)


def _register_memory_tools(memory: Any) -> None:
    """Register memory.search and memory.federated tool actions (Skill 4 / QMD)."""
    try:
        from .core.retrieval import HybridRetriever
    except ImportError:
        return

    retriever = HybridRetriever(memory=memory)

    async def _memory_search(args: dict) -> str:
        query = args.get("query", "")
        top_k = int(args.get("top_k", 5))
        results = await retriever.search(query, top_k=top_k)
        return "\n".join(
            f"[{r.get('source', '?')}] {r.get('text', '')[:200]}" for r in results
        )

    async def _memory_federated(args: dict) -> str:
        query = args.get("query", "")
        top_k = int(args.get("top_k", 10))
        results = await retriever.federated_search(query, top_k=top_k)
        return "\n".join(
            f"[{r.get('source', '?')}] {r.get('text', '')[:200]}" for r in results
        )

    register_tool("memory.search", _memory_search)
    register_tool("memory.federated", _memory_federated)


def _register_clawflow_tools() -> None:
    """Register flow dispatch tool action (Skill 5)."""
    try:
        from .orchestrator.clawflows import FlowEngine
    except ImportError:
        return

    engine = FlowEngine()

    async def _flow_run(args: dict) -> str:
        name = args.get("flow", args.get("name", ""))
        if not name:
            return "[flow: name required]"
        result = await engine.run_flow(name, trigger_context=args)
        return f"flow={name} status={result.status} steps={len(result.step_outputs)}"

    register_tool("flow.run", _flow_run)


def _register_graph_tools(memory: Any) -> None:
    """Register graph.query and graph.related tool actions (Skill 9)."""

    async def _graph_query(args: dict) -> str:
        label = args.get("label", "")
        depth = int(args.get("depth", 3))
        if not label:
            return "[graph.query: label required]"
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, memory.query_graph, label, depth)
        if not results:
            return f"[graph.query: no nodes reachable from '{label}']"
        lines = [
            f"depth={r['depth']} {r['label']} ({r['type']}) via {r['relation_type']} w={r['weight']:.1f}"
            for r in results
        ]
        return "\n".join(lines)

    async def _graph_related(args: dict) -> str:
        label = args.get("label", "")
        max_hops = int(args.get("max_hops", 2))
        if not label:
            return "[graph.related: label required]"
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, memory.related_concepts, label, max_hops)
        if not results:
            return f"[graph.related: no neighbours found for '{label}']"
        lines = [
            f"{r['label']} ({r['type']}) w={r['weight']:.1f} depth={r['depth']}"
            for r in results
        ]
        return "\n".join(lines)

    register_tool("graph.query", _graph_query)
    register_tool("graph.related", _graph_related)


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


async def _check_for_correction(
    user_message: str,
    prior_output: str,
    session_id: str,
    memory: Any,
) -> None:
    """
    Post-response hook: detect corrections and store them (Skill 1).
    Non-blocking fire-and-forget — errors are logged but never propagate.
    """
    try:
        from .orchestrator.skill_improvement_cycle import classify_correction, store_correction
        correction = classify_correction(user_message, prior_output)
        if correction is not None:
            store_correction(correction, session_id, memory)
    except Exception as exc:
        logger.debug("Correction check failed (non-fatal): %s", exc)


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
        audit_log: Optional[AuditLog] = None,
        safety_guard: Optional[SafetyGuard] = None,
    ) -> None:
        self._cfg = config
        self._budget = budget
        self._vault = vault
        # Strong references to fire-and-forget tasks so they are not GC'd
        self._bg_tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()
        self._memory = memory
        self._ctx = context_builder
        self._router = ModelRouter(
            vault=vault,
            preferred_model=config.default_model,
            swarmsync_api_url=config.swarmsync_api_url,
        )
        # Audit log — initialise lazily if not provided and audit_enabled
        self._audit_log: Optional[AuditLog] = audit_log
        if self._audit_log is None and getattr(config, "audit_enabled", True):
            self._audit_log = AuditLog()
            self._audit_log.connect()

        # Safety guard
        self._safety = safety_guard or SafetyGuard(config={"safety_mode": getattr(config, "safety_mode", "strict")})

        # Register web-search tool actions (Skill 6)
        _register_web_search_tools(vault=vault)

        # Register Python executor tool action (Skill 7)
        _register_python_executor_tools()

        # Register shell execution tool action (shell.exec)
        _register_shell_tools()

        # Register memory fact tool actions (Skill 2)
        _register_memory_tools(memory=memory)

        # Register Clawflow tool action (Skill 5)
        _register_clawflow_tools()

        # Register Knowledge Graph tool actions (Skill 9)
        _register_graph_tools(memory=memory)

    def register_tool(self, name: str, fn: Callable) -> None:
        """Register a tool with the global registry."""
        register_tool(name, fn)

    async def run(self, session_id: str, message: str, agent_id: str) -> tuple[str, str, str]:
        """
        Process *message* and return (final_text, cost_footer, model_used).

        Persists every turn to JSONL transcript.
        Raises BudgetExceeded if spend caps are breached.
        """
        tpath = _transcript_path(agent_id, session_id)
        # Prefer the config-declared workspace_dir (e.g. ~/.cato/workspace).
        # Fall back to the legacy per-agent path so existing installs are not broken.
        _raw_ws = getattr(self._cfg, "workspace_dir", None)
        workspace = (
            Path(_raw_ws).expanduser().resolve()
            if _raw_ws
            else _CATO_DIR / agent_id / "workspace"
        )
        daily_log = _CATO_DIR / agent_id / "memory" / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
        skills_dir = Path.home() / ".cato" / "skills"  # ~/.cato/skills

        # Compact old turns before building context — fires when token or turn
        # thresholds are exceeded, storing distilled summary in SQLite.
        await self._maybe_compact(tpath, session_id)

        memory_chunks = await self._memory.asearch(message, top_k=4)

        # Load most recent distilled summary (if any) for this session so
        # ContextBuilder can inject it into the system prompt.
        distilled_summary = self._load_distilled_summary(session_id)

        system_prompt = self._ctx.build_system_prompt(
            workspace_dir=workspace,
            memory_chunks=memory_chunks,
            daily_log_path=daily_log if daily_log.exists() else None,
            skills_dir=skills_dir if skills_dir.exists() else None,
            distilled_summary=distilled_summary,
        )

        # DEBUG: Confirm skills are in system prompt
        has_skills = "Available Skills" in system_prompt
        has_conduit = "conduit" in system_prompt.lower()
        logger.info("SYSTEM_PROMPT: skills_section=%s conduit=%s", has_skills, has_conduit)

        ctx_tokens  = self._ctx.count_tokens(system_prompt)
        history_len = self._history_len(tpath)
        complexity  = self._router.score_task(message, ctx_tokens, history_len)

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(self._recent_turns(tpath, limit=HISTORY_WINDOW))
        messages.append({"role": "user", "content": message})

        # Model selection and optional SwarmSync completion.
        # SwarmSync always executes a full completion even when routing_only is
        # requested, so we reuse that response for the first planning turn to
        # avoid paying twice.  Skills are preserved because the system prompt is
        # included in the messages array sent to SwarmSync.
        swarm_key = (self._vault.get("SWARM_SYNC_API_KEY") or
                     self._vault.get("SWARMSYNC_API_KEY"))
        swarmsync_prefill: str = ""  # completion text from SwarmSync (if any)
        logger.info("session=%s swarmsync_enabled=%s swarm_key_present=%s",
                    session_id, self._cfg.swarmsync_enabled, bool(swarm_key))
        if self._cfg.swarmsync_enabled and swarm_key:
            model, swarmsync_prefill = await self._router._swarmsync_complete(
                messages, swarm_key, complexity
            )
        else:
            model = self._router.select_model(complexity)

        logger.info("session=%s model=%s complexity=%.2f swarmsync_prefill=%d",
                    session_id, model, complexity, len(swarmsync_prefill))

        await _aappend(tpath, {
            "ts": _now(), "role": "user",
            "content": message, "session_id": session_id,
        })

        # ---- Planning loop -----------------------------------------------
        planning_turns = 0
        final_text = ""
        total_cost = 0.0

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

            # On the very first planning turn, use the SwarmSync completion
            # response directly (it already ran the full call with our system
            # prompt).  Subsequent turns (tool-call follow-ups) go through the
            # normal local router.
            if swarmsync_prefill and planning_turns == 0:
                text = swarmsync_prefill
                swarmsync_prefill = ""  # consume once
                tool_calls = [] if force else _parse_tool_calls_text(text)
                text = _TOOL_CALL_RE.sub("", text).strip()
                if not text and not tool_calls:
                    text = (
                        "The model returned no readable content. "
                        "Try rephrasing your message."
                    )
            else:
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
                # Safety gate: check before every tool call
                if self._safety and not self._safety.check_and_confirm(tc.name, tc.args):
                    error_msg = f"[SAFETY] Action '{tc.name}' denied by safety guard."
                    logger.warning(error_msg)
                    if self._audit_log:
                        self._audit_log.log(
                            session_id, "error", tc.name, tc.args, {},
                            error=error_msg,
                        )
                    result = json.dumps({"error": error_msg, "safety_denied": True})
                else:
                    result = await _dispatch_tool(tc)
                    # Audit log: record every tool call
                    if self._audit_log:
                        try:
                            cost_cents = 0
                            # Try to determine cost from budget
                            try:
                                cost_cents = int(round(self._budget._last_call_cost * 100))
                            except Exception:
                                pass
                            self._audit_log.log(
                                session_id, "tool_call", tc.name,
                                tc.args,
                                result,
                                cost_cents=cost_cents,
                            )
                        except Exception as audit_exc:
                            logger.debug("Audit log failed: %s", audit_exc)

                await _aappend(tpath, {
                    "ts": _now(), "role": "tool",
                    "tool_name": tc.name, "result": result, "session_id": session_id,
                })
                messages.append({"role": "user", "content": f"[tool result: {tc.name}]\n{result}"})

        _t = asyncio.create_task(
            self._memory.astore(f"Q: {message}\nA: {final_text}", source_file=session_id),
            name="memory-store",
        )
        self._bg_tasks.add(_t)

        # Fire-and-forget correction detection (Skill 1)
        _correction_task = asyncio.create_task(
            _check_for_correction(message, final_text, session_id, self._memory),
            name="correction-check",
        )
        self._bg_tasks.add(_correction_task)

        return final_text, self._budget.format_footer(), model

    # ------------------------------------------------------------------
    # Compaction helpers
    # ------------------------------------------------------------------

    async def _maybe_compact(self, tpath: Path, session_id: str) -> None:
        """
        Compact the JSONL transcript when it exceeds token/turn thresholds.

        Reads all turns, checks if compaction is needed, distils the oldest
        turns (everything except the last HISTORY_WINDOW), stores the result
        in distilled_summaries, and rewrites the transcript to contain only
        the recent window.

        This is a no-op when the transcript does not exist or is within limits.
        """
        if not tpath.exists():
            return

        try:
            lines = tpath.read_text(encoding="utf-8").splitlines()
        except OSError:
            return

        # Parse all valid JSONL records
        records: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        total_turns = len(records)
        if total_turns <= HISTORY_WINDOW:
            return  # nothing to compact

        # Count tokens in the full history to decide whether compaction is needed
        history_text = "\n".join(r.get("content", "") for r in records if r.get("role") in ("user", "assistant"))
        history_tokens = self._ctx.count_tokens(history_text)

        needs_compact = (
            history_tokens > COMPACT_TOKEN_THRESHOLD
            or total_turns > COMPACT_TURN_THRESHOLD
        )
        if not needs_compact:
            return

        # Split: turns to distil vs turns to keep live
        keep_start = max(0, total_turns - HISTORY_WINDOW)
        old_records = records[:keep_start]
        recent_records = records[keep_start:]

        logger.info(
            "Compacting session=%s: %d total turns → distilling %d, keeping %d (history_tokens=%d)",
            session_id, total_turns, len(old_records), len(recent_records), history_tokens,
        )

        # Distil old turns using heuristic extractor (no LLM call)
        try:
            from .core.distiller import Distiller
            distiller = Distiller()
            distil_turns = [
                {"role": r.get("role", ""), "content": r.get("content", "")}
                for r in old_records
                if r.get("role") in ("user", "assistant")
            ]
            result = distiller.distill(
                session_id=session_id,
                turns=distil_turns,
                turn_start=0,
            )
            if result is not None:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._memory.store_distillation, result
                )
                logger.info(
                    "Distilled session=%s turns 0-%d: summary=%d chars, facts=%d, decisions=%d",
                    session_id, len(old_records) - 1,
                    len(result.summary), len(result.key_facts), len(result.decisions),
                )
        except Exception as exc:
            logger.warning("Distillation failed (non-fatal): %s", exc)

        # Rewrite transcript to only the recent window
        try:
            new_content = "\n".join(json.dumps(r, ensure_ascii=True) for r in recent_records) + "\n"
            await asyncio.get_running_loop().run_in_executor(
                None, tpath.write_text, new_content, "utf-8"
            )
        except OSError as exc:
            logger.warning("Transcript rewrite failed: %s", exc)

    def _load_distilled_summary(self, session_id: str) -> Optional[str]:
        """
        Load the most recent distilled summary for *session_id* and format it
        as a compact context block.  Returns None if no summaries exist.
        """
        try:
            rows = self._memory.load_recent_distillations(session_id=session_id, limit=3)
        except Exception:
            return None

        if not rows:
            return None

        parts: list[str] = ["## Conversation History Summary (compacted)"]
        for row in reversed(rows):  # oldest first
            summary = row.get("summary") or ""
            facts_raw = row.get("key_facts") or "[]"
            decisions_raw = row.get("decisions") or "[]"
            questions_raw = row.get("open_questions") or "[]"

            try:
                facts = json.loads(facts_raw) if isinstance(facts_raw, str) else facts_raw
            except (json.JSONDecodeError, TypeError):
                facts = []
            try:
                decisions = json.loads(decisions_raw) if isinstance(decisions_raw, str) else decisions_raw
            except (json.JSONDecodeError, TypeError):
                decisions = []
            try:
                questions = json.loads(questions_raw) if isinstance(questions_raw, str) else questions_raw
            except (json.JSONDecodeError, TypeError):
                questions = []

            if summary:
                parts.append(f"**Summary:** {summary}")
            if facts:
                parts.append("**Key facts:** " + "; ".join(str(f) for f in facts[:5]))
            if decisions:
                parts.append("**Decisions:** " + "; ".join(str(d) for d in decisions[:5]))
            if questions:
                parts.append("**Open questions:** " + "; ".join(str(q) for q in questions[:3]))

        return "\n".join(parts)

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
                # If we got no text and no tool calls (e.g. provider only sent model slug and we filtered it)
                if not visible and not calls:
                    visible = (
                        "The model returned no readable content. "
                        "Try rephrasing your message or check that the model is responding correctly."
                    )
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
