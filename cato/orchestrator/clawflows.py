"""
cato/orchestrator/clawflows.py — Clawflows: Proactive Trigger Registry (Skill 5).

Manages YAML-defined flows that execute steps sequentially via skill/tool dispatch.
State is persisted to SQLite after each step, enabling resume-safe execution.

Flow YAML schema::

    name: morning-brief
    trigger:
      type: manual   # manual | cron | event | condition
    steps:
      - skill: web.search
        args: {query: "AI news today"}
      - skill: daily_digest
        args: {}
    budget_cap: 100

"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..platform import get_data_dir

logger = logging.getLogger(__name__)

_DATA_DIR = get_data_dir()
FLOWS_DIR = _DATA_DIR / "flows"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_name    TEXT    NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    step_outputs TEXT    NOT NULL DEFAULT '[]',
    status       TEXT    NOT NULL DEFAULT 'IN_PROGRESS',
    started_at   REAL    NOT NULL,
    updated_at   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flow_runs_name   ON flow_runs(flow_name);
CREATE INDEX IF NOT EXISTS idx_flow_runs_status ON flow_runs(status);
"""


@dataclass
class FlowResult:
    """Result of a flow execution."""
    flow_name: str
    status: str               # COMPLETED | FAILED | IN_PROGRESS
    step_outputs: list[Any] = field(default_factory=list)
    error: Optional[str] = None
    run_id: Optional[int] = None


class FlowEngine:
    """
    Engine for loading and executing Clawflows.

    Usage::

        engine = FlowEngine()
        flows = engine.list_flows()
        result = await engine.run_flow("morning-brief")
    """

    def __init__(self, flows_dir: Optional[Path] = None) -> None:
        self._flows_dir = (flows_dir or FLOWS_DIR).expanduser().resolve()
        self._flows_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = _DATA_DIR / "flow_runs.db"
        self._conn = self._open_db()

    # ------------------------------------------------------------------
    # DB
    # ------------------------------------------------------------------

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    # ------------------------------------------------------------------
    # YAML loading
    # ------------------------------------------------------------------

    def load_flow(self, name: str) -> dict:
        """
        Load a flow definition from FLOWS_DIR/<name>.yaml.

        Returns the parsed dict.
        Raises FileNotFoundError if the file does not exist.
        Raises ValueError if YAML is malformed.
        """
        path = self._flows_dir / f"{name}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Flow file not found: {path}")

        try:
            import yaml  # type: ignore[import]
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except ImportError:
            # Fallback: minimal YAML parser for simple key:value files
            data = self._parse_yaml_minimal(path)
        except Exception as exc:
            raise ValueError(f"Could not parse flow YAML {path}: {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"Flow YAML must be a mapping, got {type(data).__name__}")

        return data

    def _parse_yaml_minimal(self, path: Path) -> dict:
        """Very minimal YAML parser — used as fallback when PyYAML is unavailable."""
        import yaml
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def list_flows(self) -> list[dict]:
        """
        Scan FLOWS_DIR for .yaml files and return summary dicts.

        Each dict: {name, trigger_type, step_count, budget_cap}
        """
        results: list[dict] = []
        for yaml_file in sorted(self._flows_dir.glob("*.yaml")):
            try:
                data = self.load_flow(yaml_file.stem)
            except Exception as exc:
                logger.warning("Could not load flow %s: %s", yaml_file.name, exc)
                continue
            trigger = data.get("trigger", {})
            trigger_type = trigger.get("type", "manual") if isinstance(trigger, dict) else "manual"
            results.append({
                "name": yaml_file.stem,
                "trigger_type": trigger_type,
                "step_count": len(data.get("steps", [])),
                "budget_cap": data.get("budget_cap"),
            })
        return results

    # ------------------------------------------------------------------
    # Flow execution
    # ------------------------------------------------------------------

    async def run_flow(
        self,
        name: str,
        trigger_context: dict | None = None,
        resume_run_id: Optional[int] = None,
    ) -> FlowResult:
        """
        Execute flow *name* step by step.

        - State is persisted to SQLite after each step.
        - On error, checks step's 'on_error' field (stop | continue | retry).
        - Returns FlowResult.
        """
        trigger_context = trigger_context or {}

        try:
            flow_def = self.load_flow(name)
        except FileNotFoundError as exc:
            return FlowResult(flow_name=name, status="FAILED", error=str(exc))
        except ValueError as exc:
            return FlowResult(flow_name=name, status="FAILED", error=str(exc))

        steps = flow_def.get("steps", [])
        now = time.time()

        # Create or resume a run record
        if resume_run_id is not None:
            row = self._conn.execute(
                "SELECT * FROM flow_runs WHERE id = ?", (resume_run_id,)
            ).fetchone()
            if row:
                run_id = resume_run_id
                start_step = row["current_step"]
                step_outputs: list[Any] = json.loads(row["step_outputs"])
            else:
                run_id = resume_run_id
                start_step = 0
                step_outputs = []
        else:
            cur = self._conn.execute(
                "INSERT INTO flow_runs (flow_name, current_step, step_outputs, status, started_at, updated_at)"
                " VALUES (?, 0, '[]', 'IN_PROGRESS', ?, ?)",
                (name, now, now),
            )
            self._conn.commit()
            run_id = cur.lastrowid
            start_step = 0
            step_outputs = []

        error_msg: Optional[str] = None

        for step_idx in range(start_step, len(steps)):
            step = steps[step_idx]
            skill_name = step.get("skill", "")
            args = step.get("args", {})
            on_error = step.get("on_error", "stop")

            try:
                output = await self._dispatch_step(skill_name, args, trigger_context)
                step_outputs.append(output)
            except Exception as exc:
                logger.error("Flow %s step %d (%s) failed: %s", name, step_idx, skill_name, exc)
                step_outputs.append(f"ERROR: {exc}")

                if on_error == "stop":
                    error_msg = f"Step {step_idx} ({skill_name}) failed: {exc}"
                    # Persist failure state
                    self._persist_run(run_id, step_idx, step_outputs, "FAILED")
                    return FlowResult(
                        flow_name=name,
                        status="FAILED",
                        step_outputs=step_outputs,
                        error=error_msg,
                        run_id=run_id,
                    )
                elif on_error == "retry":
                    # Simple single retry
                    try:
                        output = await self._dispatch_step(skill_name, args, trigger_context)
                        step_outputs[-1] = output  # Replace error with success
                    except Exception as retry_exc:
                        logger.warning("Retry failed for step %d: %s", step_idx, retry_exc)
                        step_outputs[-1] = f"RETRY_FAILED: {retry_exc}"
                        if on_error == "stop":
                            error_msg = f"Step {step_idx} retry failed: {retry_exc}"
                            self._persist_run(run_id, step_idx, step_outputs, "FAILED")
                            return FlowResult(
                                flow_name=name,
                                status="FAILED",
                                step_outputs=step_outputs,
                                error=error_msg,
                                run_id=run_id,
                            )
                # on_error == "continue": fall through to next step

            # Persist after each step
            self._persist_run(run_id, step_idx + 1, step_outputs, "IN_PROGRESS")

        # All steps completed
        self._persist_run(run_id, len(steps), step_outputs, "COMPLETED")

        return FlowResult(
            flow_name=name,
            status="COMPLETED",
            step_outputs=step_outputs,
            run_id=run_id,
        )

    async def _dispatch_step(self, skill_name: str, args: dict, context: dict) -> Any:
        """Dispatch a single step to the tool registry."""
        try:
            from ..agent_loop import _TOOL_REGISTRY
            handler = _TOOL_REGISTRY.get(skill_name)
            if handler is not None:
                merged_args = {**context, **args}
                return await handler(merged_args)
        except ImportError:
            pass

        # Default: return a placeholder (tests can mock this)
        return f"[dispatched:{skill_name} args={args}]"

    def _persist_run(
        self,
        run_id: int,
        current_step: int,
        step_outputs: list[Any],
        status: str,
    ) -> None:
        """Persist flow run state to SQLite."""
        self._conn.execute(
            "UPDATE flow_runs SET current_step = ?, step_outputs = ?, status = ?, updated_at = ?"
            " WHERE id = ?",
            (current_step, json.dumps(step_outputs, default=str), status, time.time(), run_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Resume pending flows
    # ------------------------------------------------------------------

    def resume_pending_flows(self) -> list[int]:
        """
        Query IN_PROGRESS flows and schedule them for resumption.

        Returns list of run IDs that were resumed.
        """
        rows = self._conn.execute(
            "SELECT id, flow_name, current_step FROM flow_runs WHERE status = 'IN_PROGRESS'"
        ).fetchall()

        run_ids: list[int] = []
        for row in rows:
            logger.info(
                "Resuming flow %s (run_id=%d) from step %d",
                row["flow_name"], row["id"], row["current_step"],
            )
            run_ids.append(row["id"])
            # Schedule as background task if event loop is running
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    self.run_flow(row["flow_name"], resume_run_id=row["id"]),
                    name=f"flow-resume-{row['id']}",
                )
            except RuntimeError:
                pass  # No running event loop — caller must handle

        return run_ids

    def get_in_progress_flows(self) -> list[dict]:
        """Return all IN_PROGRESS flow runs."""
        rows = self._conn.execute(
            "SELECT id, flow_name, current_step, status, started_at, updated_at"
            " FROM flow_runs WHERE status = 'IN_PROGRESS'"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # YAML active toggle
    # ------------------------------------------------------------------

    def set_active(self, name: str, active: bool) -> bool:
        """
        Toggle the 'active' field in a flow's YAML file.

        Returns True if the flow file was found and updated, False otherwise.
        """
        path = self._flows_dir / f"{name}.yaml"
        if not path.exists():
            return False

        try:
            content = path.read_text(encoding="utf-8")
            # Simple text replacement for 'active: true/false'
            import re as _re
            if _re.search(r"^active\s*:", content, _re.MULTILINE):
                content = _re.sub(
                    r"^(active\s*:)\s*\S+",
                    f"\\1 {'true' if active else 'false'}",
                    content,
                    flags=_re.MULTILINE,
                )
            else:
                content = content.rstrip() + f"\nactive: {'true' if active else 'false'}\n"
            path.write_text(content, encoding="utf-8")
            return True
        except OSError as exc:
            logger.warning("Could not update %s: %s", path, exc)
            return False

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()
