"""
cato/migrate.py — OpenClaw-to-Cato workspace migration.

Invoked by `cato migrate --from-openclaw` (stubbed in cli.py).

What gets copied:
  ~/.openclaw/agents/{agent_name}/ → ~/.cato/agents/{agent_name}/
    AGENTS.md, SOUL.md, USER.md, IDENTITY.md, MEMORY.md, TOOLS.md,
    HEARTBEAT.md, CRONS.json, sessions/*.jsonl, skills/*.md

What gets skipped (incompatible):
  config.json       — Cato uses YAML config; re-run `cato init`
  node_modules/     — not applicable to Cato
  *.env / .env.*    — re-enter API keys via `cato init` + `cato vault set`

Validation:
  SKILL.md          — must have a # Title and ## Instructions or ## Usage
  *.jsonl sessions  — every line must be valid JSON

After migration prints a summary table and next-step hints.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

console = Console()

# Files copied verbatim when present
_WORKSPACE_FILES = [
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "IDENTITY.md",
    "MEMORY.md",
    "TOOLS.md",
    "HEARTBEAT.md",
    "CRONS.json",
]

# Patterns for files that must never be copied
_SKIP_PATTERNS = re.compile(
    r"(config\.json|node_modules|\.env(\..+)?|\.env$)",
    re.IGNORECASE,
)

# Minimum SKILL.md: a level-1 heading and either ## Instructions or ## Usage
_SKILL_TITLE_RE = re.compile(r"^#\s+\S", re.MULTILINE)
_SKILL_SECTION_RE = re.compile(r"^##\s+(Instructions|Usage)\b", re.MULTILINE | re.IGNORECASE)


class OpenClawMigrator:
    """
    Migrates an OpenClaw workspace directory into a Cato workspace directory.

    Parameters
    ----------
    source_dir:
        Path to the OpenClaw root (default: ``~/.openclaw``).
    dest_dir:
        Path to the Cato root (default: ``~/.cato``).
    dry_run:
        When True, no files are written; only the report is printed.
    """

    def __init__(
        self,
        source_dir: Optional[Path] = None,
        dest_dir: Optional[Path] = None,
        dry_run: bool = False,
    ) -> None:
        self.source = source_dir or Path.home() / ".openclaw"
        self.dest = dest_dir or Path.home() / ".cato"
        self.dry_run = dry_run
        self.stats: dict = {
            "agents": 0,
            "skills": 0,
            "sessions": 0,
            "skipped": 0,
            "errors": [],
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> dict:
        """
        Run the full migration and return the stats dict.

        Keys: agents, skills, sessions, skipped, errors.
        """
        agents_src = self.source / "agents"
        if not agents_src.exists():
            console.print(f"[red]Source directory not found: {agents_src}[/red]")
            return self.stats

        agent_dirs = sorted(d for d in agents_src.iterdir() if d.is_dir())
        if not agent_dirs:
            console.print("[yellow]No agent directories found in OpenClaw workspace.[/yellow]")
            return self.stats

        prefix = "[dim]DRY RUN[/dim]" if self.dry_run else ""
        if self.dry_run:
            console.print(f"\n[bold cyan]Cato Migration — Dry Run[/bold cyan]  {prefix}")
        else:
            console.print("\n[bold cyan]Cato Migration[/bold cyan]")
        console.print("=" * 54)

        for agent_dir in agent_dirs:
            self._migrate_agent(agent_dir)

        self._print_summary()
        return self.stats

    # ------------------------------------------------------------------
    # Per-agent migration
    # ------------------------------------------------------------------

    def _migrate_agent(self, agent_dir: Path) -> None:
        """Migrate one agent's workspace directory."""
        agent_name = agent_dir.name
        dest_agent = self.dest / "agents" / agent_name

        if dest_agent.exists() and not self.dry_run:
            console.print(
                f"  [yellow]SKIP[/yellow]  {agent_name}  — destination already exists"
            )
            self.stats["skipped"] += 1
            return

        if not self.dry_run:
            dest_agent.mkdir(parents=True, exist_ok=True)

        # 1. Workspace markdown / JSON files
        for filename in _WORKSPACE_FILES:
            src_file = agent_dir / filename
            if src_file.exists():
                if _SKIP_PATTERNS.search(filename):
                    self.stats["skipped"] += 1
                    continue
                if not self.dry_run:
                    shutil.copy2(src_file, dest_agent / filename)

        # 2. Skills directory
        skills_src = agent_dir / "skills"
        if skills_src.exists():
            skills_dest = dest_agent / "skills"
            if not self.dry_run:
                skills_dest.mkdir(exist_ok=True)
            for skill_file in sorted(skills_src.glob("*.md")):
                if self._validate_skill(skill_file):
                    if not self.dry_run:
                        shutil.copy2(skill_file, skills_dest / skill_file.name)
                    self.stats["skills"] += 1
                else:
                    msg = f"{agent_name}/skills/{skill_file.name}: missing # Title or ## Instructions"
                    self.stats["errors"].append(msg)
                    console.print(f"    [yellow]WARN[/yellow]  {msg}")
                    self.stats["skipped"] += 1

        # 3. Sessions directory (JSONL files)
        sessions_src = agent_dir / "sessions"
        if sessions_src.exists():
            sessions_dest = dest_agent / "sessions"
            if not self.dry_run:
                sessions_dest.mkdir(exist_ok=True)
            for jsonl_file in sorted(sessions_src.glob("*.jsonl")):
                if self._validate_jsonl(jsonl_file):
                    if not self.dry_run:
                        shutil.copy2(jsonl_file, sessions_dest / jsonl_file.name)
                    self.stats["sessions"] += 1
                else:
                    msg = f"{agent_name}/sessions/{jsonl_file.name}: invalid JSONL"
                    self.stats["errors"].append(msg)
                    console.print(f"    [yellow]WARN[/yellow]  {msg}")
                    self.stats["skipped"] += 1

        label = "[dim]would migrate[/dim]" if self.dry_run else "[green]migrated[/green]"
        console.print(f"  {label}  {agent_name}")
        self.stats["agents"] += 1

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _validate_skill(self, skill_path: Path) -> bool:
        """Return True if SKILL.md has a # Title and ## Instructions/Usage."""
        try:
            text = skill_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return bool(_SKILL_TITLE_RE.search(text)) and bool(_SKILL_SECTION_RE.search(text))

    def _validate_jsonl(self, jsonl_path: Path) -> bool:
        """Return True if every non-empty line in the file is valid JSON."""
        try:
            for line in jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if stripped:
                    json.loads(stripped)
        except (OSError, json.JSONDecodeError):
            return False
        return True

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _print_summary(self) -> None:
        """Print a rich summary table and next-step hints."""
        table = Table(title="Migration Summary", show_lines=True)
        table.add_column("Item", style="cyan")
        table.add_column("Count", justify="right", style="bold")

        table.add_row("Agents migrated", str(self.stats["agents"]))
        table.add_row("Skills migrated", str(self.stats["skills"]))
        table.add_row("Sessions migrated", str(self.stats["sessions"]))
        table.add_row("Files skipped / errors", str(self.stats["skipped"]))

        console.print()
        console.print(table)

        if self.stats["errors"]:
            console.print("\n[yellow]Validation warnings:[/yellow]")
            for err in self.stats["errors"]:
                console.print(f"  - {err}")

        if self.dry_run:
            console.print(
                "\n[dim]Dry run complete — no files were written. "
                "Re-run without --dry-run to apply.[/dim]"
            )
        else:
            console.print(
                "\nRun [bold]cato doctor[/bold] to check your workspace token budget."
            )
            console.print(
                "Run [bold]cato init[/bold] to configure API keys for the new vault."
            )
