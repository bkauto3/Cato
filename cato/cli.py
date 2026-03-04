"""
cato/cli.py — Command-line interface for CATO.

Commands:
    cato init       Interactive first-run setup wizard
    cato start      Start the CATO daemon
    cato stop       Stop the running CATO daemon
    cato migrate    Migrate workspace from OpenClaw
    cato doctor     Audit token budget and workspace health
    cato status     Show running state and budget summary
"""

from __future__ import annotations

import getpass
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from cato.budget import BudgetManager
from cato.config import CatoConfig
from cato.vault import Vault

console = Console()

_CATO_DIR = Path.home() / ".cato"
_PID_FILE = _CATO_DIR / "cato.pid"


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="0.1.0", prog_name="cato")
def main() -> None:
    """Cato — The AI agent daemon you can audit in a coffee break."""


# ---------------------------------------------------------------------------
# cato init
# ---------------------------------------------------------------------------

@main.command("init")
def cmd_init() -> None:
    """Interactive first-run setup wizard."""
    console.print("\n[bold cyan]Cato Setup Wizard[/bold cyan]")
    console.print("=" * 50)

    config = CatoConfig.load()

    if not config.is_first_run():
        if not click.confirm("Config already exists. Reinitialise?", default=False):
            console.print("[yellow]Aborted.[/yellow]")
            return

    # 1. Monthly budget cap
    raw_cap = click.prompt(
        "Monthly budget cap (USD)",
        default="20.00",
        show_default=True,
    )
    try:
        monthly_cap = float(raw_cap.replace("$", "").strip())
    except ValueError:
        monthly_cap = 20.00
    config.monthly_cap = monthly_cap

    # 2. Session cap
    raw_session = click.prompt(
        "Session budget cap (USD)",
        default="1.00",
        show_default=True,
    )
    try:
        session_cap = float(raw_session.replace("$", "").strip())
    except ValueError:
        session_cap = 1.00
    config.session_cap = session_cap

    # 3. Vault master password
    console.print("\n[bold]Vault master password[/bold] (encrypts all stored API keys)")
    pw = getpass.getpass("Set a vault master password: ")
    pw_confirm = getpass.getpass("Confirm master password: ")
    if pw != pw_confirm:
        console.print("[red]Passwords do not match. Aborted.[/red]")
        sys.exit(1)

    vault_path = _CATO_DIR / "vault.enc"
    vault = Vault.create(pw, vault_path=vault_path)
    console.print("[green]Vault created.[/green]")

    # 4. SwarmSync
    swarmync = click.confirm(
        "\nEnable SwarmSync intelligent routing?",
        default=True,
    )
    config.swarmsync_enabled = swarmync
    if swarmync:
        config.swarmsync_api_url = click.prompt(
            "SwarmSync API URL",
            default="https://api.swarmsync.ai/v1/chat/completions",
            show_default=True,
        )
        ss_key = click.prompt("SwarmSync API key (starts with sk-ss-)", hide_input=True)
        vault.set("SWARMSYNC_API_KEY", ss_key)
        click.echo("  SwarmSync API key stored in vault.")

    # 5. Telegram
    telegram = click.confirm("\nEnable Telegram?", default=False)
    config.telegram_enabled = telegram
    if telegram:
        bot_token = click.prompt("Telegram bot token")
        vault.set("TELEGRAM_BOT_TOKEN", bot_token)
        console.print("[green]Telegram token stored in vault.[/green]")

    # 6. WhatsApp
    whatsapp = click.confirm("Enable WhatsApp?", default=False)
    config.whatsapp_enabled = whatsapp

    # 7. Create directory structure
    dirs = [
        _CATO_DIR / "workspace",
        _CATO_DIR / "memory",
        _CATO_DIR / "logs",
        _CATO_DIR / "agents",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    # 8. Save config
    config.save()

    # 9. Initialise budget manager with chosen caps
    bm = BudgetManager(session_cap=session_cap, monthly_cap=monthly_cap)
    bm.set_monthly_cap(monthly_cap)
    bm.set_session_cap(session_cap)

    console.print(
        f"\n[bold green]Cato initialised.[/bold green]  "
        f"Monthly cap: ${monthly_cap:.2f}  |  Session cap: ${session_cap:.2f}"
    )
    console.print("Run [bold]cato start[/bold] to begin.\n")


def _init_vault(vault: Vault, password: str) -> None:
    """Bootstrap a new vault with a pre-supplied password (bypasses getpass)."""
    import secrets as _secrets
    from argon2.low_level import hash_secret_raw, Type
    from cato.vault import _SALT_SIZE, _ARGON2_TIME_COST, _ARGON2_MEMORY_COST, _ARGON2_PARALLELISM, _KEY_SIZE, _encrypt
    import base64, json as _json

    salt = _secrets.token_bytes(_SALT_SIZE)
    key = hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_COST,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_KEY_SIZE,
        type=Type.ID,
    )
    vault._key = key  # type: ignore[attr-defined]
    vault._data = {}  # type: ignore[attr-defined]
    plaintext = _json.dumps({}).encode("utf-8")
    blob = _encrypt(plaintext, key)
    vault._path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[attr-defined]
    vault._path.write_bytes(base64.b64encode(salt + blob))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# cato vault  (key management)
# ---------------------------------------------------------------------------

@main.group("vault")
def vault_cmd() -> None:
    """Manage vault credentials."""
    pass


@vault_cmd.command("set")
@click.argument("key")
@click.option("--value", prompt=True, hide_input=True, help="Secret value")
def vault_set(key: str, value: str) -> None:
    """Store a secret in the vault. Example: cato vault set ANTHROPIC_API_KEY"""
    vault_path = _CATO_DIR / "vault.enc"
    if not vault_path.exists():
        console.print("[red]Vault not initialised — run 'cato init' first.[/red]")
        return
    vault = Vault(vault_path=vault_path)
    vault.set(key, value)
    console.print(f"[green]Key '{key}' stored in vault.[/green]")


@vault_cmd.command("list")
def vault_list() -> None:
    """List all keys stored in the vault (values hidden)."""
    vault_path = _CATO_DIR / "vault.enc"
    if not vault_path.exists():
        console.print("[yellow]Vault not initialised — run 'cato init' first.[/yellow]")
        return
    vault = Vault(vault_path=vault_path)
    keys = vault.list_keys()
    if not keys:
        console.print("[yellow]No keys stored in vault.[/yellow]")
        return
    console.print("[bold]Vault keys:[/bold]")
    for k in sorted(keys):
        console.print(f"  {k}")


@vault_cmd.command("delete")
@click.argument("key")
def vault_delete(key: str) -> None:
    """Delete a key from the vault."""
    vault_path = _CATO_DIR / "vault.enc"
    if not vault_path.exists():
        console.print("[red]Vault not initialised — run 'cato init' first.[/red]")
        return
    vault = Vault(vault_path=vault_path)
    vault.delete(key)
    console.print(f"[green]Key '{key}' deleted from vault.[/green]")


# ---------------------------------------------------------------------------
# cato start
# ---------------------------------------------------------------------------

@main.command("start")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
@click.option("--channel", default="webchat", show_default=True,
              type=click.Choice(["webchat", "telegram", "whatsapp", "all"]),
              help="Messaging channel to enable.")
def cmd_start(agent: str, channel: str) -> None:
    """Start the CATO daemon."""
    config = CatoConfig.load()

    if _PID_FILE.exists():
        pid = _PID_FILE.read_text().strip()
        console.print(f"[yellow]Cato already running (PID {pid}). Use 'cato stop' first.[/yellow]")
        return

    console.print(f"[bold cyan]Starting Cato[/bold cyan] — agent=[{agent}] channel=[{channel}]")
    console.print(f"  Model:     {config.default_model}")
    console.print(f"  Workspace: {config.workspace_dir}")
    console.print(f"  Log level: {config.log_level}")

    # Write PID file
    import os
    _PID_FILE.write_text(str(os.getpid()))

    try:
        _run_daemon(config, agent, channel)
    finally:
        if _PID_FILE.exists():
            _PID_FILE.unlink()


def _run_daemon(config: CatoConfig, agent: str, channel: str) -> None:
    """Import and launch the Gateway with configured adapters."""
    # Deferred import so 'cato doctor' / 'cato status' don't require all deps
    import asyncio
    import logging

    vault_path = _CATO_DIR / "vault.enc"
    vault = Vault(vault_path=vault_path) if vault_path.exists() else None
    budget = BudgetManager(
        session_cap=config.session_cap,
        monthly_cap=config.monthly_cap,
    )

    async def _main(cfg: CatoConfig, vlt: "Vault", bdg: BudgetManager) -> None:
        from .gateway import Gateway
        from .adapters.telegram import TelegramAdapter
        from .adapters.whatsapp import WhatsAppAdapter
        from .ui.server import create_ui_app
        from aiohttp import web

        log = logging.getLogger("cato")

        gateway = Gateway(cfg, bdg, vlt)

        # Register configured adapters
        if cfg.telegram_enabled:
            try:
                tg = TelegramAdapter(gateway, vlt, cfg)
                gateway.register_adapter(tg)
                log.info("Telegram adapter registered")
            except Exception as e:
                log.warning(f"Telegram adapter failed to register: {e}")

        if cfg.whatsapp_enabled:
            try:
                wa = WhatsAppAdapter(gateway, vlt, cfg)
                gateway.register_adapter(wa)
                log.info("WhatsApp adapter registered")
            except Exception as e:
                log.warning(f"WhatsApp adapter failed to register: {e}")

        # Start web UI
        app = await create_ui_app(gateway)
        runner = web.AppRunner(app)
        await runner.setup()
        port = getattr(cfg, "port", None) or 18789
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        log.info(f"Web UI at http://127.0.0.1:{port}")
        console.print(f"[green]Cato daemon running on http://127.0.0.1:{port}. Press Ctrl-C to stop.[/green]")

        try:
            await gateway.start()
        finally:
            await runner.cleanup()
            await gateway.stop()

    try:
        if vault is None:
            console.print("[yellow]Warning: vault not initialised — run 'cato init' first.[/yellow]")
        asyncio.run(_main(config, vault, budget))
    except KeyboardInterrupt:
        console.print("\n[yellow]Cato daemon stopped.[/yellow]")


# ---------------------------------------------------------------------------
# cato stop
# ---------------------------------------------------------------------------

@main.command("stop")
def cmd_stop() -> None:
    """Stop the running CATO daemon."""
    if not _PID_FILE.exists():
        console.print("[yellow]Cato is not running.[/yellow]")
        return

    import os, signal
    pid_str = _PID_FILE.read_text().strip()
    try:
        pid = int(pid_str)
        os.kill(pid, signal.SIGTERM)
        _PID_FILE.unlink(missing_ok=True)
        console.print(f"[green]Cato (PID {pid}) stopped.[/green]")
    except (ValueError, ProcessLookupError, OSError) as exc:
        console.print(f"[red]Could not stop process {pid_str}: {exc}[/red]")
        _PID_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# cato migrate
# ---------------------------------------------------------------------------

@main.command("migrate")
@click.option("--from-openclaw", "from_openclaw", is_flag=True, default=False,
              help="Migrate agent workspaces from OpenClaw.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would be migrated without making changes.")
def cmd_migrate(from_openclaw: bool, dry_run: bool) -> None:
    """Migrate workspaces from another agent system."""
    if not from_openclaw:
        console.print("[red]Specify a migration source, e.g. --from-openclaw[/red]")
        return

    openclaw_agents = Path.home() / ".openclaw" / "agents"
    cato_agents = _CATO_DIR / "agents"

    if not openclaw_agents.exists():
        console.print(f"[red]OpenClaw agents directory not found: {openclaw_agents}[/red]")
        return

    agent_dirs = [d for d in openclaw_agents.iterdir() if d.is_dir()]
    if not agent_dirs:
        console.print("[yellow]No agent workspaces found in OpenClaw directory.[/yellow]")
        return

    table = Table(title="OpenClaw Migration Report", show_lines=True)
    table.add_column("Agent", style="cyan")
    table.add_column("SKILL.md", style="green")
    table.add_column("Files", justify="right")
    table.add_column("Status", style="bold")

    migrated = 0
    skipped = 0

    for agent_dir in sorted(agent_dirs):
        skill_md = agent_dir / "SKILL.md"
        has_skill = skill_md.exists()
        file_count = sum(1 for _ in agent_dir.rglob("*") if _.is_file())
        dest = cato_agents / agent_dir.name

        if dest.exists() and not dry_run:
            table.add_row(agent_dir.name, str(has_skill), str(file_count), "[yellow]SKIPPED (exists)[/yellow]")
            skipped += 1
            continue

        status = "[dim]DRY RUN[/dim]" if dry_run else "[green]MIGRATED[/green]"

        if not dry_run:
            shutil.copytree(str(agent_dir), str(dest), dirs_exist_ok=True)
            migrated += 1
        else:
            migrated += 1

        compat = "[green]OK[/green]" if has_skill else "[yellow]NO SKILL.md[/yellow]"
        table.add_row(agent_dir.name, compat, str(file_count), status)

    console.print(table)
    if dry_run:
        console.print(f"\n[dim]Dry run: {migrated} agents would be migrated, {skipped} skipped.[/dim]")
    else:
        console.print(f"\n[green]Migration complete: {migrated} migrated, {skipped} skipped.[/green]")
        console.print(f"Agents available at: {cato_agents}")


# ---------------------------------------------------------------------------
# cato doctor
# ---------------------------------------------------------------------------

@main.command("doctor")
def cmd_doctor() -> None:
    """Audit token budget, workspace health, and flag potential savings."""
    from cato.core.context_builder import ContextBuilder

    console.print("\n[bold cyan]Cato Doctor[/bold cyan]")
    console.print("=" * 50)

    cb = ContextBuilder()
    agents_dir = _CATO_DIR / "agents"

    if not agents_dir.exists() or not any(agents_dir.iterdir()):
        console.print("[yellow]No agent workspaces found in ~/.cato/agents/[/yellow]")
    else:
        table = Table(title="Agent Workspace Token Audit", show_lines=True)
        table.add_column("Agent", style="cyan")
        table.add_column("Files", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Budget %", justify="right")
        table.add_column("Flags", style="yellow")

        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue

            md_files = list(agent_dir.glob("*.md"))
            total_tokens = 0
            flags: list[str] = []

            for md in md_files:
                try:
                    content = md.read_text(encoding="utf-8", errors="replace")
                    total_tokens += cb.count_tokens(content)
                except OSError:
                    pass

            if not any((agent_dir / f).exists() for f in ["SKILL.md", "SOUL.md", "IDENTITY.md"]):
                flags.append("no SKILL.md/SOUL.md")

            budget_pct = min(999, int(total_tokens / 7000 * 100))
            flag_str = ", ".join(flags) if flags else "[green]OK[/green]"

            table.add_row(
                agent_dir.name,
                str(len(md_files)),
                str(total_tokens),
                f"{budget_pct}%",
                flag_str,
            )

        console.print(table)

    # Budget status
    console.print("\n[bold]Budget Status[/bold]")
    bm = BudgetManager()
    status = bm.get_status()
    console.print(f"  Monthly:  ${status['monthly_spend']:.4f} / ${status['monthly_cap']:.2f}"
                  f"  ({status['monthly_pct_remaining']:.0f}% remaining)")
    console.print(f"  Session:  ${status['session_spend']:.4f} / ${status['session_cap']:.2f}")
    console.print(f"  All-time: ${status['total_spend_all_time']:.4f}")

    # Vault check
    console.print("\n[bold]Vault[/bold]")
    vault_file = _CATO_DIR / "vault.enc"
    if vault_file.exists():
        console.print(f"  [green]OK[/green] — {vault_file}")
    else:
        console.print("  [yellow]Not initialised — run 'cato init'[/yellow]")

    console.print()


# ---------------------------------------------------------------------------
# cato status
# ---------------------------------------------------------------------------

@main.command("status")
def cmd_status() -> None:
    """Show running state, budget summary, and active channels."""
    config = CatoConfig.load()
    is_running = _PID_FILE.exists()

    console.print("\n[bold cyan]Cato Status[/bold cyan]")
    console.print("=" * 50)

    state_label = "[green]RUNNING[/green]" if is_running else "[red]STOPPED[/red]"
    if is_running:
        pid = _PID_FILE.read_text().strip()
        console.print(f"  Daemon:  {state_label}  (PID {pid})")
    else:
        console.print(f"  Daemon:  {state_label}")

    console.print(f"  Model:   {config.default_model}")
    console.print(f"  SwarmSync: {'enabled' if config.swarmsync_enabled else 'disabled'}")

    console.print("\n[bold]Channels[/bold]")
    console.print(f"  Telegram: {'enabled' if config.telegram_enabled else 'disabled'}")
    console.print(f"  WhatsApp: {'enabled' if config.whatsapp_enabled else 'disabled'}")
    console.print(f"  WebChat:  port {config.webchat_port}")

    console.print("\n[bold]Budget[/bold]")
    try:
        bm = BudgetManager(
            session_cap=config.session_cap,
            monthly_cap=config.monthly_cap,
        )
        status = bm.get_status()
        console.print(
            f"  {bm.format_footer()}"
        )
        console.print(f"  Calls this month: {status['monthly_calls']}")
    except Exception as exc:
        console.print(f"  [red]Could not load budget: {exc}[/red]")

    console.print()
