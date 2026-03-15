"""
cato/safety.py — Pre-action reversibility gates for CATO.

Prevents the "agent ran amok" scenario (e.g. Meta inbox deletion).
Every tool call is classified into one of four risk tiers before execution.
IRREVERSIBLE and HIGH_STAKES actions require explicit user confirmation.

Checks for a STOP signal file (get_data_dir()/STOP) before every action.

Configuration:
    safety_mode: strict      — IRREVERSIBLE and HIGH_STAKES prompt user
    safety_mode: permissive  — HIGH_STAKES prompts, IRREVERSIBLE skips prompt
    safety_mode: desktop     — like strict, but confirmation deferred to frontend
                               via a callback instead of stdin (for non-TTY contexts)
    safety_mode: off         — all gates disabled (not recommended)
"""

from __future__ import annotations

import logging
from enum import IntEnum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Risk tiers
# ---------------------------------------------------------------------------

class RiskTier(IntEnum):
    READ             = 0   # No side effects: browser.navigate, browser.extract, browser.screenshot
    REVERSIBLE_WRITE = 1   # Easily undone: browser.click, browser.type
    IRREVERSIBLE     = 2   # Cannot be undone: shell rm/delete/drop
    HIGH_STAKES      = 3   # Financial/social consequence: mail/send/post/publish/payment


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

# Tool-name → base tier mapping (before checking inputs)
_TOOL_TIER: dict[str, RiskTier] = {
    "browser.navigate":   RiskTier.READ,
    "browser.extract":    RiskTier.READ,
    "browser.screenshot": RiskTier.READ,
    "browser.search":     RiskTier.READ,
    "browser.snapshot":   RiskTier.READ,
    "browser.click":      RiskTier.REVERSIBLE_WRITE,
    "browser.type":       RiskTier.REVERSIBLE_WRITE,
    "browser.pdf":        RiskTier.REVERSIBLE_WRITE,
    "file.read":          RiskTier.READ,
    "file.list":          RiskTier.READ,
    "memory.search":      RiskTier.READ,
    "memory.store":       RiskTier.REVERSIBLE_WRITE,
}

# Keywords in shell commands that escalate tier
_IRREVERSIBLE_SHELL_KEYWORDS = frozenset({
    "rm", "del", "delete", "drop", "format", "truncate", "rmdir",
    "remove", "unlink", "shred", "wipe",
})

_HIGH_STAKES_SHELL_KEYWORDS = frozenset({
    "mail", "send", "post", "publish", "payment", "pay", "transfer",
    "deploy", "push", "submit", "commit --amend",
})


def _classify_shell(inputs: dict) -> RiskTier:
    """Classify a shell tool call based on the command string."""
    cmd = str(inputs.get("command", inputs.get("cmd", ""))).lower()
    tokens = set(cmd.split())

    if tokens & _HIGH_STAKES_SHELL_KEYWORDS:
        return RiskTier.HIGH_STAKES
    if tokens & _IRREVERSIBLE_SHELL_KEYWORDS:
        return RiskTier.IRREVERSIBLE
    return RiskTier.REVERSIBLE_WRITE  # shell by default is a write


# ---------------------------------------------------------------------------
# SafetyGuard
# ---------------------------------------------------------------------------

class SafetyGuard:
    """
    Pre-action reversibility gate.

    Usage::

        guard = SafetyGuard(config={"safety_mode": "strict"})
        allowed = guard.check_and_confirm("browser.click", {"selector": "#delete-all"})
        if not allowed:
            raise RuntimeError("User denied action")

    Desktop mode::

        async def ask_frontend(tool_name, inputs, tier_label):
            # Send confirmation request over WebSocket, await user response
            return await ws_confirm(tool_name, inputs)

        guard = SafetyGuard(
            config={"safety_mode": "desktop"},
            confirmation_callback=ask_frontend,
        )
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        confirmation_callback: Optional[object] = None,
    ) -> None:
        cfg = config or {}
        self._mode: str = cfg.get("safety_mode", "strict").lower()
        self._stop_file: Path = self._stop_file_path()
        self._confirmation_callback = confirmation_callback

    @staticmethod
    def _stop_file_path() -> Path:
        from .platform import get_data_dir
        return get_data_dir() / "STOP"

    def is_stop_requested(self) -> bool:
        """
        Return True if the STOP signal file exists.

        Place a file at {data_dir}/STOP to request immediate halt.
        """
        return self._stop_file.exists()

    def classify_action(self, tool_name: str, inputs: dict) -> RiskTier:
        """
        Classify a tool call into a RiskTier.

        Special handling:
        - shell / shell.exec: analysed by keyword scanning of the command string.
        - All other tools: looked up in _TOOL_TIER; unknown tools default to REVERSIBLE_WRITE.
        """
        if tool_name in ("shell", "shell.exec", "shell.run"):
            return _classify_shell(inputs)

        return _TOOL_TIER.get(tool_name, RiskTier.REVERSIBLE_WRITE)

    def check_and_confirm(self, tool_name: str, inputs: dict) -> bool:
        """
        Check whether the action should proceed.

        Returns True if allowed, False if the user denied or a STOP was requested.

        Logic:
        - If safety_mode == "off": always True.
        - If STOP file exists: log warning and return False.
        - If tier < threshold for current mode: True.
        - Otherwise: print action summary and prompt "Proceed? [y/N]".
          Default answer is N (safe by default).
        """
        if self._mode == "off":
            return True

        # Emergency stop check
        if self.is_stop_requested():
            logger.warning(
                "STOP signal file detected — halting before tool_name=%s", tool_name
            )
            _safe_print(f"[CATO SAFETY] STOP file detected at {self._stop_file}. Halting.")
            return False

        tier = self.classify_action(tool_name, inputs)

        # Determine threshold based on mode
        if self._mode == "permissive":
            threshold = RiskTier.HIGH_STAKES       # only HIGH_STAKES prompts
        else:
            # strict (default)
            threshold = RiskTier.IRREVERSIBLE      # IRREVERSIBLE + HIGH_STAKES prompt

        if tier < threshold:
            return True

        # Needs confirmation
        tier_label = {
            RiskTier.IRREVERSIBLE: "IRREVERSIBLE",
            RiskTier.HIGH_STAKES:  "HIGH-STAKES",
        }.get(tier, tier.name)

        _safe_print(f"\n[CATO SAFETY] {tier_label} action requested:")
        _safe_print(f"  Tool:   {tool_name}")
        # Show a sanitised subset of inputs (skip long values)
        short_inputs = {
            k: (str(v)[:120] + "..." if len(str(v)) > 120 else v)
            for k, v in inputs.items()
        }
        _safe_print(f"  Inputs: {short_inputs}")

        # Desktop mode: delegate confirmation to a callback (e.g. WebSocket prompt)
        if self._mode == "desktop" and self._confirmation_callback is not None:
            import asyncio
            import inspect

            cb = self._confirmation_callback
            try:
                if inspect.iscoroutinefunction(cb):
                    # Run the async callback — get or create an event loop
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop and loop.is_running():
                        # Already in an async context — create a task and use
                        # a thread to block-wait for the result
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor(1) as pool:
                            result = pool.submit(
                                asyncio.run, cb(tool_name, inputs, tier_label)
                            ).result(timeout=120)
                    else:
                        result = asyncio.run(cb(tool_name, inputs, tier_label))
                else:
                    result = cb(tool_name, inputs, tier_label)

                if result:
                    logger.info("Desktop user approved %s action: %s", tier_label, tool_name)
                    return True
                logger.info("Desktop user denied %s action: %s", tier_label, tool_name)
                return False
            except Exception as exc:
                logger.error("Desktop confirmation callback failed: %s", exc)
                _safe_print(f"[CATO SAFETY] Confirmation callback error: {exc}. Denying action.")
                return False

        import sys
        if not sys.stdin.isatty():
            # Daemon mode without desktop callback — deny by default (fail-safe).
            logger.warning("Safety check: non-interactive context, denying %s by default.", tool_name)
            _safe_print("[CATO SAFETY] Non-interactive context: action denied by default.")
            return False
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            _safe_print("\nAborted.")
            return False

        if answer in ("y", "yes"):
            logger.info("User approved %s action: %s", tier_label, tool_name)
            return True

        logger.info("User denied %s action: %s", tier_label, tool_name)
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_print(text: str) -> None:
    """Print using platform-safe print if available, else fallback."""
    try:
        from .platform import safe_print
        safe_print(text)
    except Exception:
        print(text)
