"""
cato/auth/token_checker.py — Pre-action scope check for delegation tokens.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .token_store import TokenStore, ACTION_CATEGORIES

# Tool-name → action category mapping (extends reversibility registry)
_TOOL_CATEGORY_MAP: dict[str, str] = {
    "conduit_navigate": "web.navigate",
    "conduit_extract":  "web.extract",
    "conduit_click":    "web.navigate",
    "conduit_type":     "web.navigate",
    "web_search":       "web.extract",
    "read_file":        "file.read",
    "write_file":       "file.write",
    "edit_file":        "file.write",
    "delete_file":      "file.delete",
    "git_commit":       "git.write",
    "git_push":         "git.write",
    "email_send":       "email.send",
    "api_payment":      "payment.*",
    "shell_execute":    "shell.execute",
}


@dataclass
class AuthResult:
    authorized: bool
    token_id: Optional[str]
    reason: str
    requires_user_confirmation: bool


class TokenChecker:
    """Check delegation tokens before executing tool actions."""

    def __init__(
        self,
        token_store: Optional[TokenStore] = None,
        db_path: Optional[Path] = None,
    ) -> None:
        self._store = token_store or TokenStore(db_path=db_path)

    def check_authorization(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        agent_session_id: str,
        estimated_cost: float = 0.0,
    ) -> AuthResult:
        # Deactivate expired tokens first
        self._store.deactivate_expired()

        category = _TOOL_CATEGORY_MAP.get(tool_name)
        if category is None:
            # Unknown tool category — allow but note
            return AuthResult(
                authorized=True,
                token_id=None,
                reason=(
                    f"Tool '{tool_name}' has no mapped category; "
                    "proceeding without token check."
                ),
                requires_user_confirmation=False,
            )

        active_tokens = self._store.list_active()
        if not active_tokens:
            return AuthResult(
                authorized=True,
                token_id=None,
                reason="No active delegation tokens; proceeding in default mode.",
                requires_user_confirmation=False,
            )

        for token in active_tokens:
            # Check category — support wildcard suffix (e.g. "payment.*")
            cats = token.allowed_action_categories
            matched = False
            for c in cats:
                if c == category:
                    matched = True
                    break
                if c.endswith(".*") and category.startswith(c[:-2]):
                    matched = True
                    break
            if not matched:
                continue  # This token doesn't cover this category

            # Check spending ceiling
            remaining = token.spending_ceiling - token.spending_used
            if estimated_cost > 0 and estimated_cost > remaining:
                return AuthResult(
                    authorized=False,
                    token_id=token.token_id,
                    reason=(
                        f"Spending ceiling exceeded: need {estimated_cost:.2f}, "
                        f"have {remaining:.2f} remaining."
                    ),
                    requires_user_confirmation=True,
                )

            # Authorized — deduct cost
            if estimated_cost > 0:
                self._store.deduct_spending(token.token_id, estimated_cost)

            return AuthResult(
                authorized=True,
                token_id=token.token_id,
                reason=(
                    f"Authorized by token {token.token_id[:8]}\u2026 "
                    f"(category={category})"
                ),
                requires_user_confirmation=False,
            )

        return AuthResult(
            authorized=False,
            token_id=None,
            reason=(
                f"No active token covers category '{category}' "
                f"for tool '{tool_name}'."
            ),
            requires_user_confirmation=True,
        )
