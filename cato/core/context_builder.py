"""
cato/core/context_builder.py — Token budget and context injection for CATO.

Assembles the system prompt from workspace files respecting a hard token
ceiling of MAX_CONTEXT_TOKENS.  Files are injected in priority order so
the most important content survives when the budget is tight.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import tiktoken

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONTEXT_TOKENS = 7000

# Priority-ordered list of workspace files.
# Each entry: (filename, must_include_fully)
# must_include_fully=True  → include whole file or omit entirely (no trimming)
# must_include_fully=False → trim to fit remaining budget
_PRIORITY_STACK: list[tuple[str, bool]] = [
    ("SKILL.md",    True),   # Active skill instructions — always load if present
    ("SOUL.md",     True),
    ("IDENTITY.md", True),
    ("AGENTS.md",   True),
    ("USER.md",     True),
    ("TOOLS.md",    True),
    ("MEMORY.md",   False),
    # Daily log and retrieved chunks are injected programmatically below
]

_ENCODING_NAME = "cl100k_base"


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------

class ContextBuilder:
    """
    Assembles a system prompt from workspace files within a token budget.

    Priority order is fixed:
        1. SOUL.md (always wins)
        2. IDENTITY.md
        3. AGENTS.md
        4. USER.md
        5. TOOLS.md
        6. MEMORY.md (trimmed if needed)
        7. Today's daily log (trimmed if needed)
        8. Retrieved memory chunks (trimmed if needed)

    Usage::

        cb = ContextBuilder()
        prompt = cb.build_system_prompt(
            workspace_dir=Path("~/.cato/workspace/my-agent"),
            memory_chunks=["chunk A ...", "chunk B ..."],
            daily_log_path=Path("~/.cato/memory/2026-03-03.md"),
        )
    """

    def __init__(self, max_tokens: int = MAX_CONTEXT_TOKENS) -> None:
        self._max_tokens = max_tokens
        try:
            self._enc = tiktoken.get_encoding(_ENCODING_NAME)
        except Exception:
            self._enc = None  # fall back to character approximation

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_system_prompt(
        self,
        workspace_dir: Path,
        memory_chunks: Optional[list[str]] = None,
        daily_log_path: Optional[Path] = None,
    ) -> str:
        """
        Assemble and return the system prompt string.

        Files that do not exist are skipped silently.
        Token usage per file is logged at DEBUG level.
        """
        workspace_dir = workspace_dir.expanduser().resolve()
        memory_chunks = memory_chunks or []

        sections: list[str] = []
        used_tokens = 0
        remaining = self._max_tokens

        # ---- Priority stack: static files --------------------------------
        for filename, must_full in _PRIORITY_STACK:
            filepath = workspace_dir / filename
            if not filepath.exists():
                logger.debug("Skipping %s (not found)", filename)
                continue

            content = filepath.read_text(encoding="utf-8", errors="replace")
            tokens = self.count_tokens(content)

            if must_full:
                if tokens <= remaining:
                    sections.append(self._wrap(filename, content))
                    used_tokens += tokens
                    remaining -= tokens
                    logger.debug("Included %s: %d tokens", filename, tokens)
                else:
                    logger.debug(
                        "Omitted %s: needs %d tokens, only %d remaining",
                        filename, tokens, remaining,
                    )
                continue

            # Trimmable file
            if remaining <= 0:
                logger.debug("Budget exhausted before %s", filename)
                continue

            trimmed, actual_tokens = self._trim_to_budget(content, remaining)
            sections.append(self._wrap(filename, trimmed))
            used_tokens += actual_tokens
            remaining -= actual_tokens
            logger.debug("Included %s: %d tokens (trimmed=%s)", filename, actual_tokens, trimmed != content)

        # ---- Daily log ---------------------------------------------------
        if daily_log_path and daily_log_path.exists() and remaining > 0:
            log_content = daily_log_path.read_text(encoding="utf-8", errors="replace")
            trimmed, tok = self._trim_to_budget(log_content, remaining)
            sections.append(self._wrap(daily_log_path.name, trimmed))
            used_tokens += tok
            remaining -= tok
            logger.debug("Included daily log %s: %d tokens", daily_log_path.name, tok)

        # ---- Retrieved memory chunks -------------------------------------
        if memory_chunks and remaining > 0:
            chunk_lines: list[str] = []
            for chunk in memory_chunks:
                tok = self.count_tokens(chunk)
                if tok <= remaining:
                    chunk_lines.append(chunk)
                    used_tokens += tok
                    remaining -= tok
                    logger.debug("Included memory chunk: %d tokens", tok)
                else:
                    # Trim this chunk to fit
                    trimmed, tok = self._trim_to_budget(chunk, remaining)
                    if trimmed:
                        chunk_lines.append(trimmed)
                        used_tokens += tok
                        remaining -= tok
                    break  # no budget left

            if chunk_lines:
                sections.append(self._wrap("RETRIEVED_MEMORY", "\n\n---\n\n".join(chunk_lines)))

        logger.debug(
            "Context assembled: %d/%d tokens used (%d remaining)",
            used_tokens, self._max_tokens, remaining,
        )
        return "\n\n".join(sections)

    def count_tokens(self, text: str) -> int:
        """
        Return an approximate token count for *text*.

        Uses tiktoken cl100k_base if available, otherwise falls back to
        len(text) // 4 (a reasonable heuristic for English prose).
        """
        if self._enc is not None:
            return len(self._enc.encode(text, disallowed_special=()))
        return max(1, len(text) // 4)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _trim_to_budget(self, text: str, budget: int) -> tuple[str, int]:
        """
        Return (trimmed_text, token_count) where token_count <= budget.

        If the text already fits, it is returned unchanged.
        Trimming preserves whole lines and appends a truncation notice.
        """
        tokens = self.count_tokens(text)
        if tokens <= budget:
            return text, tokens

        notice = "\n\n[...truncated to fit context budget...]"
        notice_tokens = self.count_tokens(notice)
        content_budget = budget - notice_tokens

        if content_budget <= 0:
            return "", 0

        if self._enc is not None:
            encoded = self._enc.encode(text, disallowed_special=())
            trimmed_ids = encoded[:content_budget]
            trimmed = self._enc.decode(trimmed_ids)
        else:
            # Character fallback: 4 chars per token
            char_limit = content_budget * 4
            trimmed = text[:char_limit]

        result = trimmed + notice
        return result, self.count_tokens(result)

    @staticmethod
    def _wrap(filename: str, content: str) -> str:
        """Wrap file content in a labelled markdown block."""
        separator = "=" * 60
        return f"<!-- {filename} -->\n{separator}\n{content.strip()}\n{separator}"
