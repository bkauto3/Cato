"""
Async CLI invoker for Claude CLI, Codex CLI, and Gemini CLI.
Handles parallel invocation of multiple models with fallback support.

All three models are invoked via their installed CLIs (claude, codex, gemini)
using asyncio.create_subprocess_exec() for true non-blocking parallelism.

On Windows, .CMD batch wrappers (npm-installed CLIs like codex and gemini)
are resolved via shutil.which() and executed through cmd.exe /c.
"""

import asyncio
import logging
import shutil
import sys
import time
from typing import Dict, Optional, Tuple

from cato.orchestrator.confidence_extractor import extract_confidence

logger = logging.getLogger(__name__)


class SubprocessError(RuntimeError):
    """Raised when a CLI subprocess exits with a non-zero return code."""
    def __init__(self, cmd: str, returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{cmd} exited with code {returncode}: {stderr[:200]}")


def _resolve_cli(name: str) -> list:
    """
    Resolve a CLI command name to an executable args list.

    On Windows, npm-installed CLIs are .CMD batch wrappers that cannot be
    executed directly by asyncio.create_subprocess_exec(). This function
    uses shutil.which() to find the real path, and wraps .cmd/.bat files
    in 'cmd.exe /c' so they execute correctly.

    Args:
        name: CLI command name (e.g. "codex", "gemini", "claude").

    Returns:
        List of args suitable for create_subprocess_exec.

    Raises:
        FileNotFoundError: If the CLI is not installed.
    """
    resolved = shutil.which(name)
    if resolved is None:
        raise FileNotFoundError(f"{name} not found on PATH")

    # On Windows, .CMD/.BAT wrappers need cmd.exe /c to execute
    if sys.platform == "win32" and resolved.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", resolved]

    return [resolved]


async def _run_subprocess_async(
    args: list,
    timeout_sec: float = 60.0,
) -> str:
    """
    Run a subprocess asynchronously without blocking the event loop.

    Args:
        args: Command and arguments list.
        timeout_sec: Maximum time to wait for the process.

    Returns:
        stdout text on success.

    Raises:
        FileNotFoundError: If the executable is not found.
        asyncio.TimeoutError: If the process exceeds timeout_sec.
        SubprocessError: If the process exits with non-zero return code.
    """
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()  # reap the zombie
        raise

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        raise SubprocessError(args[0], proc.returncode, stderr_text)

    if not stdout_text and stderr_text:
        # Some CLIs write to stderr even on success (warnings, progress)
        logger.warning("CLI %s returned empty stdout, stderr: %s", args[0], stderr_text[:200])

    return stdout_text


async def invoke_claude_api(prompt: str, task: str) -> Dict:
    """
    Invoke Claude via the ``claude`` CLI (non-blocking).

    Uses asyncio.create_subprocess_exec() so the event loop is not blocked
    while waiting for the child process.  Falls back to a mock response with
    confidence 0.75 when the ``claude`` binary is not found.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.

    Returns:
        {
            "model": "claude",
            "response": str,
            "confidence": float,
            "latency_ms": float,
            "degraded": bool
        }
    """
    start_time = time.time()

    try:
        full_prompt = f"Task: {task}\n\nContext: {prompt}"

        # Pool-first: use persistent process if warm
        from cato.orchestrator.cli_process_pool import get_pool
        pool = get_pool()
        if pool.is_warm("claude"):
            response_text = await pool.send_to("claude", full_prompt)
            source = "pool"
        else:
            cli_args = _resolve_cli("claude")
            response_text = await _run_subprocess_async(
                cli_args + ["-p", full_prompt],
                timeout_sec=60.0,
            )
            source = "subprocess"
        confidence = extract_confidence(response_text)
        latency_ms = (time.time() - start_time) * 1000

        return {
            "model": "claude",
            "response": response_text,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "degraded": False,
            "source": source,
        }
    except FileNotFoundError:
        latency_ms = (time.time() - start_time) * 1000
        logger.warning("claude CLI not found, using mock response")
        return {
            "model": "claude",
            "response": f"[Claude Mock] CLI not installed. Task: {task}",
            "confidence": 0.75,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }
    except SubprocessError as e:
        # Note: SubprocessError only comes from _run_subprocess_async (cold path).
        # Pool errors surface as RuntimeError and fall through to the generic handler.
        latency_ms = (time.time() - start_time) * 1000
        logger.error("claude CLI failed (rc=%d): %s", e.returncode, e.stderr[:200])
        return {
            "model": "claude",
            "response": f"[Claude Error] {e.stderr[:500]}",
            "confidence": 0.5,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except asyncio.TimeoutError:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "claude",
            "response": "[Claude Error] Process timed out after 60s",
            "confidence": 0.5,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "claude",
            "response": f"[Claude Error] {str(e)}",
            "confidence": 0.5,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }


async def invoke_codex_cli(prompt: str, task: str) -> Dict:
    """
    Invoke Codex CLI asynchronously.

    Uses _resolve_cli() to handle Windows .CMD wrappers, then
    asyncio.create_subprocess_exec() for non-blocking execution.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.

    Returns:
        dict with model, response, confidence, latency_ms, degraded keys.
    """
    start_time = time.time()

    try:
        full_prompt = f"Task: {task}\n\nContext: {prompt}"

        # Pool-first: use persistent process if warm
        from cato.orchestrator.cli_process_pool import get_pool
        pool = get_pool()
        if pool.is_warm("codex"):
            response_text = await pool.send_to("codex", full_prompt)
            source = "pool"
        else:
            cli_args = _resolve_cli("codex")
            response_text = await _run_subprocess_async(
                cli_args + ["exec", full_prompt],
                timeout_sec=60.0,
            )
            source = "subprocess"
        confidence = extract_confidence(response_text)
        latency_ms = (time.time() - start_time) * 1000

        return {
            "model": "codex",
            "response": response_text,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "degraded": False,
            "source": source,
        }
    except FileNotFoundError:
        latency_ms = (time.time() - start_time) * 1000
        logger.warning("codex CLI not found, using mock response")
        return {
            "model": "codex",
            "response": f"[Codex Mock] CLI not installed. Task: {task}",
            "confidence": 0.72,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }
    except SubprocessError as e:
        # Note: SubprocessError only comes from _run_subprocess_async (cold path).
        # Pool errors surface as RuntimeError and fall through to the generic handler.
        latency_ms = (time.time() - start_time) * 1000
        logger.error("codex CLI failed (rc=%d): %s", e.returncode, e.stderr[:200])
        return {
            "model": "codex",
            "response": f"[Codex Error] {e.stderr[:500]}",
            "confidence": 0.6,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except asyncio.TimeoutError:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "codex",
            "response": "[Codex Error] Process timed out after 60s",
            "confidence": 0.6,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "codex",
            "response": f"[Codex Error] {str(e)}",
            "confidence": 0.6,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }


async def invoke_gemini_cli(prompt: str, task: str) -> Dict:
    """
    Invoke Gemini CLI asynchronously.

    Uses _resolve_cli() to handle Windows .CMD wrappers, then
    asyncio.create_subprocess_exec() for non-blocking execution.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.

    Returns:
        dict with model, response, confidence, latency_ms, degraded keys.
    """
    start_time = time.time()

    try:
        cli_args = _resolve_cli("gemini")
        full_prompt = f"Task: {task}\n\nContext: {prompt}"
        response_text = await _run_subprocess_async(
            cli_args + ["-p", full_prompt],
            timeout_sec=60.0,
        )
        confidence = extract_confidence(response_text)
        latency_ms = (time.time() - start_time) * 1000

        return {
            "model": "gemini",
            "response": response_text,
            "confidence": confidence,
            "latency_ms": latency_ms,
            "degraded": False,
            "source": "subprocess",
        }
    except FileNotFoundError:
        latency_ms = (time.time() - start_time) * 1000
        logger.warning("gemini CLI not found, using mock response")
        return {
            "model": "gemini",
            "response": f"[Gemini Mock] CLI not installed. Task: {task}",
            "confidence": 0.68,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }
    except SubprocessError as e:
        latency_ms = (time.time() - start_time) * 1000
        logger.error("gemini CLI failed (rc=%d): %s", e.returncode, e.stderr[:200])
        return {
            "model": "gemini",
            "response": f"[Gemini Error] {e.stderr[:500]}",
            "confidence": 0.6,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except asyncio.TimeoutError:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "gemini",
            "response": "[Gemini Error] Process timed out after 60s",
            "confidence": 0.55,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "subprocess",
        }
    except Exception as e:
        latency_ms = (time.time() - start_time) * 1000
        return {
            "model": "gemini",
            "response": f"[Gemini Error] {str(e)}",
            "confidence": 0.55,
            "latency_ms": latency_ms,
            "degraded": True,
            "source": "mock",
        }


async def invoke_all_parallel(
    prompt: str,
    task: str,
) -> Tuple[Dict, Dict, Dict]:
    """
    Invoke all 3 models concurrently using asyncio.

    All three coroutines are scheduled as tasks so the event loop can
    interleave their I/O wait times.  The function still awaits all three
    results before returning; for mid-flight cancellation driven by an early
    confidence threshold, use ``invoke_with_early_termination`` instead.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.

    Returns:
        (claude_result, codex_result, gemini_result) — all three dicts.
    """
    claude_task = asyncio.create_task(invoke_claude_api(prompt, task))
    codex_task = asyncio.create_task(invoke_codex_cli(prompt, task))
    gemini_task = asyncio.create_task(invoke_gemini_cli(prompt, task))

    results = await asyncio.gather(claude_task, codex_task, gemini_task)
    return tuple(results)


async def invoke_with_early_termination(
    prompt: str,
    task: str,
    results_queue: asyncio.Queue,
    threshold: float = 0.90,
) -> None:
    """
    Invoke all 3 models concurrently and push each result into *results_queue*
    as soon as it arrives.

    This is the correct companion to ``wait_for_threshold``.  Results land in
    the queue the moment each model finishes, so ``wait_for_threshold`` can
    act on the first high-confidence result and cancel the remaining work
    before the slower models finish.

    Args:
        prompt: Context or code to analyse.
        task: High-level task description.
        results_queue: Queue that ``wait_for_threshold`` is consuming.
        threshold: Passed through for documentation purposes; the actual
            termination decision is made by ``wait_for_threshold``.
    """
    async def _invoke_and_enqueue(coro):
        result = await coro
        await results_queue.put(result)

    await asyncio.gather(
        asyncio.create_task(_invoke_and_enqueue(invoke_claude_api(prompt, task))),
        asyncio.create_task(_invoke_and_enqueue(invoke_codex_cli(prompt, task))),
        asyncio.create_task(_invoke_and_enqueue(invoke_gemini_cli(prompt, task))),
    )
