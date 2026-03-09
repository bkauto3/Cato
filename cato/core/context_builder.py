"""
cato/core/context_builder.py — Token budget and context injection for CATO.

Assembles the system prompt from workspace files respecting a hard token
ceiling of MAX_CONTEXT_TOKENS.  Files are injected in priority order so
the most important content survives when the budget is tight.

Phase C — Step 2: Per-slot token ceilings via SlotBudget dataclass.
Phase C — Step 3: HOT/COLD skill split via <!-- COLD --> delimiter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import tiktoken

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_CONTEXT_TOKENS = 12000  # Raised from 7000 — Step 2.3

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
    ("TOOLS.md",      True),
    ("HEARTBEAT.md",  True),   # Periodic check checklist — loaded when present
    # MEMORY.md removed from static stack — content now served via semantic
    # memory retrieval (asearch top_k=4) to save ~5,500 tokens per turn.
    # Daily log and retrieved chunks are injected programmatically below
]

_ENCODING_NAME = "cl100k_base"

# Slot-to-filename mapping for ceiling enforcement
_SLOT_MAP: dict[str, str] = {
    "SOUL.md":      "tier0_identity",
    "IDENTITY.md":  "tier0_identity",
    "AGENTS.md":    "tier0_agents",
    "USER.md":      "tier0_agents",
    "TOOLS.md":     "tier1_tools",
    "HEARTBEAT.md": "tier1_tools",
    "SKILL.md":     "tier1_skill",
}

# HOT/COLD delimiter — everything before this line is the HOT section
_COLD_DELIMITER = "<!-- COLD -->"

# Sentinel appended when a slot's content is truncated
_SLOT_TRUNCATION_NOTICE = "\n[truncated — full content retrievable via memory search]"


# ---------------------------------------------------------------------------
# SlotBudget
# ---------------------------------------------------------------------------

@dataclass
class SlotBudget:
    """
    Per-slot token ceilings for context assembly.

    Slot assignments:
      tier0_identity : SOUL.md + IDENTITY.md
      tier0_agents   : AGENTS.md + USER.md
      tier1_skill    : active skill HOT section (and fallback for unknown files)
      tier1_memory   : semantic search results
      tier1_tools    : TOOLS.md / HEARTBEAT.md
      tier1_history  : conversation history (managed by agent_loop)
      headroom       : overflow safety margin
      total          : global ceiling (== MAX_CONTEXT_TOKENS)

    Invariant: tier0_identity + tier0_agents + tier1_skill + tier1_memory
               + tier1_tools + tier1_history + headroom == total
    """
    tier0_identity: int = 1500   # SOUL.md + IDENTITY.md
    tier0_agents:   int = 800    # AGENTS.md + USER.md
    tier1_skill:    int = 600    # active skill HOT section
    tier1_memory:   int = 2000   # semantic search results
    tier1_tools:    int = 500    # TOOLS.md / HEARTBEAT.md
    tier1_history:  int = 4000   # conversation history (managed by agent_loop)
    headroom:       int = 2600   # overflow safety margin
    total:          int = 12000  # global ceiling


DEFAULT_SLOT_BUDGET = SlotBudget()


# ---------------------------------------------------------------------------
# HOT/COLD section loader
# ---------------------------------------------------------------------------

def list_available_skills(skills_dir: Path) -> list[str]:
    """
    Scan *skills_dir* for all available skill directories (containing SKILL.md).
    Return list of skill names in alphabetical order.

    A valid skill is a directory containing a SKILL.md file.
    Skips directories ending with .DISABLED.
    """
    skills = []
    if not skills_dir.exists():
        return skills

    for item in sorted(skills_dir.iterdir()):
        if item.is_dir() and not item.name.endswith(".DISABLED"):
            skill_file = item / "SKILL.md"
            if skill_file.exists():
                skills.append(item.name)

    return skills


def load_hot_section(skill_path: Path, slot_ceiling: int = DEFAULT_SLOT_BUDGET.tier1_skill) -> str:
    """
    Load only the HOT section of a skill file.

    Convention:
      - Everything *above* the ``<!-- COLD -->`` delimiter is HOT.
      - Everything *below* is COLD (never auto-injected into context).
      - If no delimiter is present the entire file is returned (backward compat).

    The HOT section is truncated to *slot_ceiling* tokens if necessary, with a
    sentinel notice appended so the agent knows more is available.

    Returns the (possibly truncated) HOT section as a string.
    """
    if not skill_path.exists():
        return ""

    raw = skill_path.read_text(encoding="utf-8", errors="replace")

    if _COLD_DELIMITER in raw:
        hot = raw.split(_COLD_DELIMITER, 1)[0].rstrip()
    else:
        hot = raw.rstrip()

    # Enforce slot ceiling
    try:
        enc = tiktoken.get_encoding(_ENCODING_NAME)
        tokens = len(enc.encode(hot, disallowed_special=()))
    except Exception:
        tokens = max(1, len(hot) // 4)

    if tokens <= slot_ceiling:
        return hot

    # Truncate to ceiling
    notice = _SLOT_TRUNCATION_NOTICE
    try:
        enc = tiktoken.get_encoding(_ENCODING_NAME)
        notice_tokens = len(enc.encode(notice, disallowed_special=()))
        content_budget = slot_ceiling - notice_tokens
        if content_budget <= 0:
            # Ceiling too small to fit any content — return the notice alone
            return notice.lstrip()
        ids = enc.encode(hot, disallowed_special=())
        hot = enc.decode(ids[:content_budget])
    except Exception:
        char_limit = slot_ceiling * 4
        if char_limit <= 0:
            return notice.lstrip()
        hot = hot[:char_limit]

    return hot + notice


def retrieve_cold_section(skill_path: Path) -> str:
    """
    Return the COLD section of a skill file (everything after ``<!-- COLD -->``).

    This is NOT auto-injected into context.  Call explicitly when the agent
    requests deep documentation for a skill.

    Returns empty string if the file has no COLD section or does not exist.
    """
    if not skill_path.exists():
        return ""

    raw = skill_path.read_text(encoding="utf-8", errors="replace")
    if _COLD_DELIMITER not in raw:
        return ""

    return raw.split(_COLD_DELIMITER, 1)[1].lstrip()


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------

class ContextBuilder:
    """
    Assembles a system prompt from workspace files within a token budget.

    Priority order is fixed:
        1. SKILL.md  (active skill instructions — HOT section only)
        2. SOUL.md   (always wins on identity)
        3. IDENTITY.md
        4. AGENTS.md
        5. USER.md
        6. TOOLS.md
        7. HEARTBEAT.md (periodic check checklist)
        8. Today's daily log (trimmed if needed)
        9. Retrieved memory chunks via asearch() top_k=4 (trimmed if needed)

    Each file is assigned to a slot in SlotBudget and truncated to that slot's
    ceiling before the global ceiling is checked.  This prevents any single file
    from consuming the entire budget and starving other slots.

    Note: MEMORY.md is no longer injected from the static stack.  Its content
    is served via semantic retrieval (MemorySystem.asearch) to avoid the
    ~5,500 token per-turn cost of loading the full file.

    Usage::

        cb = ContextBuilder()
        prompt = cb.build_system_prompt(
            workspace_dir=Path("~/.cato/workspace/my-agent"),
            memory_chunks=["chunk A ...", "chunk B ..."],
            daily_log_path=Path("~/.cato/memory/2026-03-03.md"),
        )

        # Custom slot budgets:
        budget = SlotBudget(tier0_identity=2000, total=14000)
        prompt = cb.build_system_prompt(workspace_dir=..., slot_budget=budget)
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
        slot_budget: Optional[SlotBudget] = None,
        skills_dir: Optional[Path] = None,
    ) -> str:
        """
        Assemble and return the system prompt string.

        Files that do not exist are skipped silently.
        Token usage per file is logged at DEBUG level.

        Args:
            workspace_dir: Directory containing SOUL.md, SKILL.md, etc.
            memory_chunks: Pre-retrieved semantic memory chunks to append.
            daily_log_path: Path to today's daily log file (optional).
            slot_budget: Per-slot token ceilings.  Defaults to DEFAULT_SLOT_BUDGET.
            skills_dir: Directory containing available skills. If provided, injects a list.
        """
        workspace_dir = workspace_dir.expanduser().resolve()
        memory_chunks = memory_chunks or []
        budget = slot_budget or DEFAULT_SLOT_BUDGET

        # Use the budget's total as the effective global ceiling (caller can raise it)
        effective_max = max(self._max_tokens, budget.total)

        sections: list[str] = []
        used_tokens = 0
        remaining = effective_max

        # Track tokens used per slot to enforce per-slot ceilings across files
        slot_used: dict[str, int] = {}

        # ---- Available skills injection (before priority stack) -----
        if skills_dir:
            available = list_available_skills(Path(skills_dir).expanduser().resolve())
            if available:
                skills_list = "# Available Skills\n\nYou have access to the following skills:\n\n" + \
                             "\n".join(f"- {s}" for s in available)
                tok = self.count_tokens(skills_list)
                if tok <= remaining:
                    sections.append(self._wrap("AVAILABLE_SKILLS", skills_list))
                    used_tokens += tok
                    remaining -= tok
                    logger.debug("Included available skills list: %d tokens (%d skills)", tok, len(available))
                else:
                    logger.debug("Skipped skills list: %d tokens, only %d remaining", tok, remaining)

        # ---- Priority stack: static files --------------------------------
        for filename, must_full in _PRIORITY_STACK:
            filepath = workspace_dir / filename
            if not filepath.exists():
                logger.debug("Skipping %s (not found)", filename)
                continue

            # Determine this file's slot and ceiling
            slot_name = _SLOT_MAP.get(filename, "tier1_skill")
            slot_ceiling: int = getattr(budget, slot_name, budget.tier1_skill)
            already_used_in_slot = slot_used.get(slot_name, 0)
            slot_remaining = slot_ceiling - already_used_in_slot

            # Load content — use HOT section loader for skill files
            if filename == "SKILL.md":
                content = load_hot_section(filepath, slot_ceiling=slot_remaining if slot_remaining > 0 else slot_ceiling)
            else:
                content = filepath.read_text(encoding="utf-8", errors="replace")

            tokens = self.count_tokens(content)

            # Warn if a Tier 0 file (identity-critical) exceeds its slot ceiling
            if filename in ("SOUL.md", "IDENTITY.md") and tokens > slot_ceiling:
                logger.warning(
                    "Tier 0 file %s (%d tokens) exceeds slot ceiling %d — truncating. "
                    "Consider trimming this file.",
                    filename, tokens, slot_ceiling,
                )

            # Apply per-slot ceiling: truncate if content exceeds what this slot can afford
            if tokens > slot_remaining and slot_remaining > 0:
                content, tokens = self._truncate_to_slot(content, slot_remaining)
                logger.debug(
                    "Slot-truncated %s to %d tokens (slot=%s, slot_remaining=%d)",
                    filename, tokens, slot_name, slot_remaining,
                )
            elif slot_remaining <= 0:
                logger.debug(
                    "Omitted %s: slot %s exhausted", filename, slot_name,
                )
                continue

            if must_full:
                if tokens <= remaining:
                    sections.append(self._wrap(filename, content))
                    used_tokens += tokens
                    remaining -= tokens
                    slot_used[slot_name] = already_used_in_slot + tokens
                    logger.debug("Included %s: %d tokens (slot=%s)", filename, tokens, slot_name)
                else:
                    logger.debug(
                        "Omitted %s: needs %d tokens, only %d remaining globally",
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
            slot_used[slot_name] = already_used_in_slot + actual_tokens
            logger.debug(
                "Included %s: %d tokens (trimmed=%s, slot=%s)",
                filename, actual_tokens, trimmed != content, slot_name,
            )

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
            memory_ceiling = budget.tier1_memory
            memory_used = 0
            chunk_lines: list[str] = []
            for chunk in memory_chunks:
                tok = self.count_tokens(chunk)
                chunk_fits_in_slot = (memory_used + tok) <= memory_ceiling
                if tok <= remaining and chunk_fits_in_slot:
                    chunk_lines.append(chunk)
                    used_tokens += tok
                    remaining -= tok
                    memory_used += tok
                    logger.debug("Included memory chunk: %d tokens", tok)
                else:
                    # Trim this chunk to the smaller of remaining global budget
                    # and what the memory slot can still absorb
                    effective_budget = min(remaining, memory_ceiling - memory_used)
                    if effective_budget <= 0:
                        break
                    trimmed, tok = self._trim_to_budget(chunk, effective_budget)
                    if trimmed:
                        chunk_lines.append(trimmed)
                        used_tokens += tok
                        remaining -= tok
                    break  # no budget left

            if chunk_lines:
                sections.append(self._wrap("RETRIEVED_MEMORY", "\n\n---\n\n".join(chunk_lines)))

        logger.debug(
            "Context assembled: %d/%d tokens used (%d remaining)",
            used_tokens, effective_max, remaining,
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

    def _truncate_to_slot(self, text: str, slot_ceiling: int) -> tuple[str, int]:
        """
        Truncate *text* to *slot_ceiling* tokens with a slot-specific sentinel.

        Returns (truncated_text, token_count).
        """
        tokens = self.count_tokens(text)
        if tokens <= slot_ceiling:
            return text, tokens

        notice = _SLOT_TRUNCATION_NOTICE
        notice_tokens = self.count_tokens(notice)
        content_budget = slot_ceiling - notice_tokens

        if content_budget <= 0:
            return "", 0

        if self._enc is not None:
            encoded = self._enc.encode(text, disallowed_special=())
            trimmed = self._enc.decode(encoded[:content_budget])
        else:
            char_limit = content_budget * 4
            trimmed = text[:char_limit]

        result = trimmed + notice
        return result, self.count_tokens(result)

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
