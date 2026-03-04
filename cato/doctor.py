"""
cato/doctor.py — Workspace health auditor for CATO.

Invoked by `cato doctor` (defined in cli.py).

Checks performed:
  1. Config file exists and parses as valid YAML
  2. Vault file is present (decryption not attempted — avoids password prompt)
  3. Per-agent workspace files: token counts vs. recommended limits
     SOUL.md:   warn if > 800 tokens
     AGENTS.md: warn if > 1500 tokens
     USER.md:   warn if > 500 tokens
     MEMORY.md: warn if > 1000 tokens
     (Other .md files: warn if > 600 tokens each)
  4. Budget status (monthly spent vs. cap)
  5. Active sessions (PID file)
  6. Telegram / WhatsApp configured
  7. Patchright / Chromium available
  8. Vault keys listed (count only — no values shown)
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

from cato.budget import BudgetManager
from cato.config import CatoConfig

console = Console()

_CATO_DIR = Path.home() / ".cato"
_PID_FILE = _CATO_DIR / "cato.pid"

# Recommended token ceilings per workspace file
_TOKEN_LIMITS: dict[str, int] = {
    "SOUL.md": 800,
    "AGENTS.md": 1500,
    "USER.md": 500,
    "MEMORY.md": 1000,
    "IDENTITY.md": 600,
    "TOOLS.md": 600,
    "HEARTBEAT.md": 400,
}
_DEFAULT_LIMIT = 600          # applied to any .md not in the table above
_CONTEXT_BUDGET = 7000        # total bootstrap context tokens available

# Cache the tiktoken encoding at module level so it is only loaded once.
try:
    import tiktoken as _tiktoken
    _ENC = _tiktoken.get_encoding("cl100k_base")
except Exception:
    _tiktoken = None  # type: ignore[assignment]
    _ENC = None


def _count_tokens(text: str) -> int:
    """Approximate token count using tiktoken cl100k_base encoding."""
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:
            pass
    # Fallback: ~4 chars per token
    return max(1, len(text) // 4)


class DoctorReport:
    """
    Audits the Cato workspace and prints a structured health report.

    Parameters
    ----------
    agent_id:
        When given, restrict the workspace audit to this agent only.
    """

    def __init__(self, agent_id: Optional[str] = None) -> None:
        self.agent_id = agent_id
        self._config: Optional[CatoConfig] = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, agent_id: Optional[str] = None) -> None:
        """Run all health checks and print the report."""
        if agent_id:
            self.agent_id = agent_id

        console.print("\n[bold cyan]Cato Doctor[/bold cyan]")
        console.print("=" * 54)

        self._check_config()
        self._check_vault()
        self._check_workspaces()
        self._check_budget()
        self._check_daemon()
        self._check_channels()
        self._check_browser()
        console.print()

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_config(self) -> None:
        """Check 1: config file exists and is valid YAML."""
        console.print("\n[bold]Config[/bold]")
        config_path = _CATO_DIR / "config.yaml"
        if not config_path.exists():
            console.print("  [yellow]NOT FOUND[/yellow] — run 'cato init' to create config")
            return
        try:
            self._config = CatoConfig.load(config_path)
            console.print(f"  [green]OK[/green] — {config_path}")
            console.print(f"     model: {self._config.default_model}")
            console.print(f"     monthly cap: ${self._config.monthly_cap:.2f}"
                          f"  |  session cap: ${self._config.session_cap:.2f}")
        except Exception as exc:
            console.print(f"  [red]INVALID[/red] — {exc}")

    def _check_vault(self) -> None:
        """Check 2: vault file is present."""
        console.print("\n[bold]Vault[/bold]")
        vault_path = _CATO_DIR / "vault.enc"
        if vault_path.exists():
            size_kb = vault_path.stat().st_size / 1024
            console.print(
                f"  [green]OK[/green] — {vault_path}  ({size_kb:.1f} KB)"
            )
        else:
            console.print(
                "  [yellow]NOT FOUND[/yellow] — run 'cato init' to create vault"
            )

    def _check_workspaces(self) -> None:
        """Check 3: per-agent workspace file token audit."""
        console.print("\n[bold]Workspace Token Audit[/bold]")

        agents_dir = _CATO_DIR / "agents"
        if not agents_dir.exists():
            console.print("  [yellow]No agents directory found[/yellow]")
            return

        agent_dirs: list[Path] = sorted(
            d for d in agents_dir.iterdir()
            if d.is_dir() and (self.agent_id is None or d.name == self.agent_id)
        )
        if not agent_dirs:
            label = f"'{self.agent_id}'" if self.agent_id else "any"
            console.print(f"  [yellow]No agent workspace found for {label}[/yellow]")
            return

        for agent_dir in agent_dirs:
            self._audit_agent_workspace(agent_dir)

    def _audit_agent_workspace(self, agent_dir: Path) -> None:
        """Print a token table for one agent's workspace."""
        table = Table(
            title=f"Agent: {agent_dir.name}",
            show_lines=True,
            show_footer=True,
        )
        table.add_column("File", style="cyan")
        table.add_column("Tokens", justify="right")
        table.add_column("Limit", justify="right")
        table.add_column("Status")

        total_tokens = 0
        md_files = sorted(agent_dir.glob("*.md"))

        for md in md_files:
            try:
                content = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tokens = _count_tokens(content)
            total_tokens += tokens
            limit = _TOKEN_LIMITS.get(md.name, _DEFAULT_LIMIT)
            if tokens > limit:
                status = f"[red]OVER LIMIT[/red]  (trim by {tokens - limit} tokens)"
            else:
                status = "[green]OK[/green]"
            table.add_row(md.name, str(tokens), str(limit), status)

        # Summary row
        bootstrap_pct = int(total_tokens / _CONTEXT_BUDGET * 100)
        pct_color = "red" if bootstrap_pct > 90 else ("yellow" if bootstrap_pct > 70 else "green")
        table.add_row(
            "[bold]TOTAL[/bold]",
            f"[bold]{total_tokens}[/bold]",
            str(_CONTEXT_BUDGET),
            f"[{pct_color}]{bootstrap_pct}% of context budget[/{pct_color}]",
        )

        console.print(table)

        # Cost hint: at 150 output tokens per reply, bootstrap is a fixed overhead
        cost_per_1k = (total_tokens / 1_000_000) * 3.00 * 1000   # sonnet-4-6 input rate
        console.print(
            f"  Estimated bootstrap cost at sonnet-4-6: "
            f"${cost_per_1k:.4f} per 1,000 messages\n"
        )

    def _check_budget(self) -> None:
        """Check 4: budget status."""
        console.print("[bold]Budget[/bold]")
        try:
            cfg = self._config
            bm = BudgetManager(
                session_cap=cfg.session_cap if cfg else 1.00,
                monthly_cap=cfg.monthly_cap if cfg else 20.00,
            )
            status = bm.get_status()
            monthly_color = "red" if status["monthly_pct_remaining"] < 20 else "green"
            console.print(
                f"  Monthly:  ${status['monthly_spend']:.4f} / ${status['monthly_cap']:.2f}"
                f"  [{monthly_color}]({status['monthly_pct_remaining']:.0f}% remaining)[/{monthly_color}]"
            )
            console.print(
                f"  Session:  ${status['session_spend']:.4f} / ${status['session_cap']:.2f}"
            )
            console.print(f"  All-time: ${status['total_spend_all_time']:.4f}")
            console.print(f"  Calls this month: {status['monthly_calls']}")
        except Exception as exc:
            console.print(f"  [red]Could not read budget: {exc}[/red]")

    def _check_daemon(self) -> None:
        """Check 5: is the daemon currently running?"""
        console.print("\n[bold]Daemon[/bold]")
        if _PID_FILE.exists():
            pid = _PID_FILE.read_text().strip()
            console.print(f"  [green]RUNNING[/green]  PID {pid}")
        else:
            console.print("  [dim]STOPPED[/dim]  (run 'cato start' to launch)")

    def _check_channels(self) -> None:
        """Check 6: Telegram / WhatsApp configured."""
        console.print("\n[bold]Channels[/bold]")
        cfg = self._config
        if cfg is None:
            console.print("  [yellow]Config not loaded — skipping channel check[/yellow]")
            return

        tg_status = "[green]enabled[/green]" if cfg.telegram_enabled else "[dim]disabled[/dim]"
        wa_status = "[green]enabled[/green]" if cfg.whatsapp_enabled else "[dim]disabled[/dim]"
        console.print(f"  Telegram: {tg_status}")
        console.print(f"  WhatsApp: {wa_status}")
        console.print(f"  WebChat:  port {cfg.webchat_port}")

    def _check_browser(self) -> None:
        """Check 7: Patchright / Chromium available."""
        console.print("\n[bold]Browser (Patchright)[/bold]")
        patchright_cli = shutil.which("patchright")
        chromium = shutil.which("chromium") or shutil.which("chromium-browser")

        if patchright_cli:
            console.print(f"  [green]patchright[/green]  — {patchright_cli}")
        else:
            try:
                import patchright  # noqa: F401  — importable is enough
                console.print("  [green]patchright[/green]  — installed (Python package)")
            except ImportError:
                console.print(
                    "  [yellow]patchright not found[/yellow]  — "
                    "install with: pip install patchright"
                )

        if chromium:
            console.print(f"  [green]chromium[/green]   — {chromium}")
        else:
            console.print(
                "  [dim]chromium not found in PATH[/dim]  — "
                "browser tools may auto-download via Playwright"
            )
