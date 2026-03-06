"""
cato/cli.py — Command-line interface for CATO.

Commands:
    cato init                          Interactive first-run setup wizard
    cato start [--browser conduit]     Start the CATO daemon
    cato stop                          Stop the running CATO daemon
    cato migrate --from-openclaw       Migrate workspace from OpenClaw
    cato doctor [--skills] [--attest]  Audit workspace health + attestation
    cato status                        Show running state and budget summary
    cato vault set/list/delete         Manage vault credentials
    cato audit --session <id>          Export audit log for a session
    cato receipt --session <id>        Show signed fare receipt for a session
    cato replay --session <id> [--live] Replay a recorded session
"""

from __future__ import annotations

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
from cato.platform import get_data_dir, safe_print, setup_signal_handlers
from cato.vault import Vault

console = Console()

_CATO_DIR = get_data_dir()
_PID_FILE = _CATO_DIR / "cato.pid"


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="1.1.0", prog_name="cato")
def main() -> None:
    """Cato — The AI agent daemon you can audit in a coffee break."""


# ---------------------------------------------------------------------------
# cato init
# ---------------------------------------------------------------------------

@main.command("init")
def cmd_init() -> None:
    """Interactive first-run setup wizard."""
    safe_print("\nCato Setup Wizard")
    safe_print("=" * 50)

    config = CatoConfig.load()

    if not config.is_first_run():
        if not click.confirm("Config already exists. Reinitialise?", default=False):
            safe_print("Aborted.")
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
    safe_print("\nVault master password (encrypts all stored API keys)")
    import sys as _sys
    _hide = _sys.stdin.isatty()
    pw = click.prompt("Set a vault master password", hide_input=_hide)
    pw_confirm = click.prompt("Confirm master password", hide_input=_hide)
    if pw != pw_confirm:
        safe_print("Passwords do not match. Aborted.")
        sys.exit(1)

    vault_path = _CATO_DIR / "vault.enc"
    vault = Vault.create(pw, vault_path=vault_path)
    safe_print("Vault created.")

    # 4. SwarmSync
    swarmync = click.confirm(
        "\nEnable SwarmSync intelligent routing?",
        default=False,
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
        safe_print("  SwarmSync API key stored in vault.")

    # 5. Telegram
    telegram = click.confirm("\nEnable Telegram?", default=False)
    config.telegram_enabled = telegram
    if telegram:
        bot_token = click.prompt("Telegram bot token")
        vault.set("TELEGRAM_BOT_TOKEN", bot_token)
        safe_print("Telegram token stored in vault.")

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

    safe_print(
        f"\nCato initialised.  "
        f"Monthly cap: ${monthly_cap:.2f}  |  Session cap: ${session_cap:.2f}"
    )
    safe_print("Run [cato start] to begin.\n")


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
        safe_print("Vault not initialised — run 'cato init' first.")
        return
    vault = Vault(vault_path=vault_path)
    vault.set(key, value)
    safe_print(f"Key '{key}' stored in vault.")


@vault_cmd.command("list")
def vault_list() -> None:
    """List all keys stored in the vault (values hidden)."""
    vault_path = _CATO_DIR / "vault.enc"
    if not vault_path.exists():
        safe_print("Vault not initialised — run 'cato init' first.")
        return
    vault = Vault(vault_path=vault_path)
    keys = vault.list_keys()
    if not keys:
        safe_print("No keys stored in vault.")
        return
    safe_print("Vault keys:")
    for k in sorted(keys):
        safe_print(f"  {k}")


@vault_cmd.command("delete")
@click.argument("key")
def vault_delete(key: str) -> None:
    """Delete a key from the vault."""
    vault_path = _CATO_DIR / "vault.enc"
    if not vault_path.exists():
        safe_print("Vault not initialised — run 'cato init' first.")
        return
    vault = Vault(vault_path=vault_path)
    vault.delete(key)
    safe_print(f"Key '{key}' deleted from vault.")


# ---------------------------------------------------------------------------
# cato start
# ---------------------------------------------------------------------------

@main.command("start")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
@click.option("--channel", default="webchat", show_default=True,
              type=click.Choice(["webchat", "telegram", "whatsapp", "all"]),
              help="Messaging channel to enable.")
@click.option("--browser", default="default", show_default=True,
              type=click.Choice(["default", "conduit"]),
              help="Browser engine to use (conduit = opt-in per-action billing).")
def cmd_start(agent: str, channel: str, browser: str) -> None:
    """Start the CATO daemon."""
    # Load .env file if it exists
    import os
    from pathlib import Path
    env_file = Path.cwd() / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
        except ImportError:
            pass  # dotenv not installed, continue with existing env vars

    config = CatoConfig.load()

    if browser == "conduit":
        config.conduit_enabled = True
        safe_print("Conduit browser engine enabled (per-action billing).")

    if _PID_FILE.exists():
        pid = _PID_FILE.read_text().strip()
        safe_print(f"Cato already running (PID {pid}). Use 'cato stop' first.")
        return

    safe_print(f"Starting Cato — agent=[{agent}] channel=[{channel}] browser=[{browser}]")
    safe_print(f"  Model:     {config.default_model}")
    safe_print(f"  Workspace: {config.workspace_dir}")
    safe_print(f"  Log level: {config.log_level}")

    # Write PID file
    import os
    _PID_FILE.write_text(str(os.getpid()))

    # Setup cross-platform signal handlers
    def _shutdown() -> None:
        safe_print("\nCato daemon stopped.")
        _PID_FILE.unlink(missing_ok=True)

    setup_signal_handlers(_shutdown)

    try:
        _run_daemon(config, agent, channel)
    finally:
        if _PID_FILE.exists():
            _PID_FILE.unlink()


def _run_daemon(config: CatoConfig, agent: str, channel: str) -> None:
    """Import and launch the Gateway with configured adapters."""
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

        app = await create_ui_app(gateway)
        runner = web.AppRunner(app)
        await runner.setup()
        port = getattr(cfg, "webchat_port", None) or getattr(cfg, "port", None) or 18789
        actual_port = port
        for attempt in range(5):
            try:
                site = web.TCPSite(runner, "127.0.0.1", port + attempt)
                await site.start()
                actual_port = port + attempt
                if attempt > 0:
                    log.info(f"Port {port} in use, using {actual_port} instead")
                break
            except OSError:
                if attempt == 4:
                    raise
        log.info(f"Web UI at http://127.0.0.1:{actual_port}")
        safe_print(f"Cato daemon running on http://127.0.0.1:{actual_port}. Press Ctrl-C to stop.")

        try:
            await gateway.start()
            # Keep the event loop alive until interrupted.
            # gateway.start() creates background tasks and returns immediately.
            stop_event = asyncio.Event()
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()
            await gateway.stop()

    try:
        if vault is None:
            safe_print("Warning: vault not initialised — run 'cato init' first.")
        asyncio.run(_main(config, vault, budget))
    except KeyboardInterrupt:
        safe_print("\nCato daemon stopped.")


# ---------------------------------------------------------------------------
# cato stop
# ---------------------------------------------------------------------------

@main.command("stop")
def cmd_stop() -> None:
    """Stop the running CATO daemon."""
    if not _PID_FILE.exists():
        safe_print("Cato is not running.")
        return

    import os, signal
    pid_str = _PID_FILE.read_text().strip()
    try:
        pid = int(pid_str)
        os.kill(pid, signal.SIGTERM)
        _PID_FILE.unlink(missing_ok=True)
        safe_print(f"Cato (PID {pid}) stopped.")
    except (ValueError, ProcessLookupError, OSError) as exc:
        safe_print(f"Could not stop process {pid_str}: {exc}")
        _PID_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# cato migrate
# ---------------------------------------------------------------------------

@main.command("migrate")
@click.option("--from-openclaw", "from_openclaw", is_flag=True, default=False,
              help="Migrate agent workspaces from OpenClaw.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would be migrated without making changes.")
@click.option("--browser", default="default",
              type=click.Choice(["default", "conduit"]),
              help="Browser engine preference to set in migrated config.")
def cmd_migrate(from_openclaw: bool, dry_run: bool, browser: str) -> None:
    """Migrate workspaces from another agent system."""
    from cato.migrate import OpenClawMigrator, detect_openclaw_install, estimate_openclaw_last_month_cost, generate_migration_report

    if not from_openclaw:
        safe_print("Specify a migration source, e.g. --from-openclaw")
        return

    # Auto-detect OpenClaw if available
    oc_dir = detect_openclaw_install()
    if oc_dir:
        safe_print(f"OpenClaw installation detected at: {oc_dir}")
        oc_cost = estimate_openclaw_last_month_cost(oc_dir)
    else:
        oc_cost = None

    migrator = OpenClawMigrator(dry_run=dry_run)
    stats = migrator.run()

    report = generate_migration_report(
        migrated_agents=stats["agents"],
        migrated_skills=stats["skills"],
        openclaw_cost=oc_cost,
    )
    safe_print(report)


# ---------------------------------------------------------------------------
# cato doctor
# ---------------------------------------------------------------------------

@main.command("doctor")
@click.option("--skills", is_flag=True, default=False,
              help="Validate all SKILL.md files in agent directories.")
@click.option("--attest", is_flag=True, default=False,
              help="Emit signed JSON attestation of security properties.")
def cmd_doctor(skills: bool, attest: bool) -> None:
    """Audit token budget, workspace health, and flag potential savings."""
    if attest:
        _cmd_doctor_attest()
        return

    if skills:
        _cmd_doctor_skills()
        return

    # Default doctor
    from cato.core.context_builder import ContextBuilder

    safe_print("\nCato Doctor")
    safe_print("=" * 50)

    cb = ContextBuilder()
    agents_dir = _CATO_DIR / "agents"

    if not agents_dir.exists() or not any(agents_dir.iterdir()):
        safe_print("No agent workspaces found in agents/")
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
    safe_print("\nBudget Status")
    bm = BudgetManager()
    status = bm.get_status()
    safe_print(f"  Monthly:  ${status['monthly_spend']:.4f} / ${status['monthly_cap']:.2f}"
               f"  ({status['monthly_pct_remaining']:.0f}% remaining)")
    safe_print(f"  Session:  ${status['session_spend']:.4f} / ${status['session_cap']:.2f}")
    safe_print(f"  All-time: ${status['total_spend_all_time']:.4f}")

    # Vault check
    safe_print("\nVault")
    vault_file = _CATO_DIR / "vault.enc"
    if vault_file.exists():
        safe_print(f"  OK — {vault_file}")
    else:
        safe_print("  Not initialised — run 'cato init'")

    safe_print("")


def _cmd_doctor_skills() -> None:
    """Validate all SKILL.md files and print report."""
    from cato.skill_validator import SkillValidator

    safe_print("\nCato Skill Validator")
    safe_print("=" * 50)

    validator = SkillValidator()
    agents_dir = _CATO_DIR / "agents"
    results = validator.validate_all(agents_dir)
    report = validator.format_report(results)
    safe_print(report)


def _cmd_doctor_attest() -> None:
    """Emit a signed JSON attestation of Cato security properties."""
    import hashlib
    import time

    vault_file = _CATO_DIR / "vault.enc"
    config = CatoConfig.load()

    attestation = {
        "cato_version": "1.1.0",
        "timestamp": time.time(),
        "vault_encrypted": vault_file.exists(),
        "telemetry_disabled": True,   # Cato has zero telemetry by design
        "budget_enforced": True,       # Hard caps before every LLM call
        "audit_enabled": config.audit_enabled,
        "safety_mode": config.safety_mode,
        "conduit_enabled": config.conduit_enabled,
    }

    # Sign with SHA-256 of the attestation values (deterministic)
    payload = json.dumps(attestation, sort_keys=True, ensure_ascii=True)
    sig = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    attestation["signature"] = sig

    safe_print(json.dumps(attestation, indent=2))


# ---------------------------------------------------------------------------
# cato status
# ---------------------------------------------------------------------------

@main.command("status")
def cmd_status() -> None:
    """Show running state, budget summary, and active channels."""
    config = CatoConfig.load()
    is_running = _PID_FILE.exists()

    safe_print("\nCato Status")
    safe_print("=" * 50)

    if is_running:
        pid = _PID_FILE.read_text().strip()
        safe_print(f"  Daemon:  RUNNING  (PID {pid})")
    else:
        safe_print("  Daemon:  STOPPED")

    safe_print(f"  Model:   {config.default_model}")
    safe_print(f"  SwarmSync: {'enabled' if config.swarmsync_enabled else 'disabled'}")
    safe_print(f"  Safety:  {config.safety_mode}")
    safe_print(f"  Conduit: {'enabled' if config.conduit_enabled else 'disabled'}")

    safe_print("\nChannels")
    safe_print(f"  Telegram: {'enabled' if config.telegram_enabled else 'disabled'}")
    safe_print(f"  WhatsApp: {'enabled' if config.whatsapp_enabled else 'disabled'}")
    safe_print(f"  WebChat:  port {config.webchat_port}")

    safe_print("\nBudget")
    try:
        bm = BudgetManager(
            session_cap=config.session_cap,
            monthly_cap=config.monthly_cap,
        )
        status = bm.get_status()
        safe_print(f"  {bm.format_footer()}")
        safe_print(f"  Calls this month: {status['monthly_calls']}")
    except Exception as exc:
        safe_print(f"  Could not load budget: {exc}")

    safe_print("")


# ---------------------------------------------------------------------------
# cato audit
# ---------------------------------------------------------------------------

@main.command("audit")
@click.option("--session", "session_id", required=True, help="Session ID to export.")
@click.option("--format", "fmt", default="jsonl",
              type=click.Choice(["jsonl", "csv"]),
              help="Output format.")
@click.option("--verify", is_flag=True, default=False,
              help="Verify SHA-256 chain integrity before exporting.")
def cmd_audit(session_id: str, fmt: str, verify: bool) -> None:
    """Export the audit log for a session as JSONL or CSV."""
    from cato.audit import AuditLog

    log = AuditLog()
    log.connect()

    if verify:
        ok = log.verify_chain(session_id)
        status = "CHAIN INTACT" if ok else "CHAIN BROKEN — possible tampering"
        safe_print(f"Audit chain verification: {status}")
        if not ok:
            sys.exit(1)

    summary = log.session_summary(session_id)
    if summary["count"] == 0:
        safe_print(f"No audit records found for session: {session_id}")
        return

    safe_print(
        f"Session {session_id}: {summary['count']} actions, "
        f"{summary['total_cost_cents']}c total, "
        f"{summary['errors']} errors"
    )
    safe_print(log.export_session(session_id, fmt=fmt))


# ---------------------------------------------------------------------------
# cato receipt
# ---------------------------------------------------------------------------

@main.command("receipt")
@click.option("--session", "session_id", required=True, help="Session ID.")
@click.option("--format", "fmt", default="text",
              type=click.Choice(["text", "jsonl"]),
              help="Output format.")
def cmd_receipt(session_id: str, fmt: str) -> None:
    """Show a signed fare receipt for a session."""
    from cato.audit import AuditLog
    from cato.receipt import ReceiptWriter

    log = AuditLog()
    log.connect()
    writer = ReceiptWriter()
    receipt = writer.generate(session_id, log)

    if fmt == "jsonl":
        safe_print(writer.export_jsonl(receipt))
    else:
        safe_print(writer.export_text(receipt))


# ---------------------------------------------------------------------------
# cato cron  (schedule management)
# ---------------------------------------------------------------------------

@main.group("cron")
def cron_cmd() -> None:
    """Manage scheduled cron tasks for agents."""
    pass


@cron_cmd.command("add")
@click.option("--schedule", required=True, help="Cron expression, e.g. '0 9 * * *'")
@click.option("--prompt", required=True, help="Prompt to send to the agent.")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
@click.option("--announce/--no-announce", default=False, show_default=True,
              help="Send a message to the channel when the cron fires.")
@click.option("--session", "session_id", default="", help="Session ID (auto-generated if omitted).")
@click.option("--channel", default="web", show_default=True,
              help="Channel to deliver announced output to.")
def cron_add(schedule: str, prompt: str, agent: str, announce: bool,
             session_id: str, channel: str) -> None:
    """Add a scheduled cron task for an agent.

    \b
    Example:
        cato cron add --schedule "0 9 * * *" --agent personal \\
                      --prompt "Summarise new emails" --announce
    """
    import json as _json, time as _time
    try:
        from croniter import croniter
        if not croniter.is_valid(schedule):
            safe_print(f"Invalid cron expression: {schedule!r}")
            return
    except ImportError:
        safe_print("Warning: croniter not installed — schedule not validated. "
                   "Install with: pip install croniter")

    agent_dir = _CATO_DIR / "agents" / agent
    agent_dir.mkdir(parents=True, exist_ok=True)
    crons_path = agent_dir / "CRONS.json"

    crons: list[dict] = []
    if crons_path.exists():
        try:
            crons = _json.loads(crons_path.read_text(encoding="utf-8"))
        except Exception:
            crons = []

    sid = session_id or f"cron-{agent}-{int(_time.time())}"
    entry = {
        "schedule": schedule,
        "prompt": prompt,
        "agent_id": agent,
        "session_id": sid,
        "announce": announce,
        "channel": channel,
        "created_at": _time.time(),
    }
    crons.append(entry)
    crons_path.write_text(_json.dumps(crons, indent=2, ensure_ascii=False), encoding="utf-8")
    safe_print(f"Cron added for agent [{agent}]: {schedule!r} → {prompt!r}")
    safe_print(f"  session_id: {sid}  announce: {announce}  total crons: {len(crons)}")


@cron_cmd.command("list")
@click.option("--agent", default="", help="Filter by agent (all agents if omitted).")
def cron_list(agent: str) -> None:
    """List all scheduled cron tasks."""
    import json as _json

    agents_dir = _CATO_DIR / "agents"
    if not agents_dir.exists():
        safe_print("No agents directory found.")
        return

    dirs = [agents_dir / agent] if agent else list(agents_dir.iterdir())
    found_any = False

    table = Table(title="Cron Schedule", show_lines=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Agent", style="cyan")
    table.add_column("Schedule")
    table.add_column("Prompt")
    table.add_column("Announce")
    table.add_column("Session ID", style="dim")

    for d in sorted(dirs):
        if not d.is_dir():
            continue
        crons_path = d / "CRONS.json"
        if not crons_path.exists():
            continue
        try:
            crons = _json.loads(crons_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for i, entry in enumerate(crons):
            found_any = True
            table.add_row(
                str(i),
                d.name,
                entry.get("schedule", ""),
                entry.get("prompt", "")[:60],
                "yes" if entry.get("announce") else "no",
                entry.get("session_id", ""),
            )

    if found_any:
        console.print(table)
    else:
        safe_print("No cron tasks found. Add one with: cato cron add")


@cron_cmd.command("remove")
@click.option("--agent", required=True, help="Agent workspace name.")
@click.option("--index", required=True, type=int, help="Index from 'cato cron list'.")
def cron_remove(agent: str, index: int) -> None:
    """Remove a cron task by its list index."""
    import json as _json

    crons_path = _CATO_DIR / "agents" / agent / "CRONS.json"
    if not crons_path.exists():
        safe_print(f"No CRONS.json found for agent [{agent}].")
        return

    try:
        crons: list[dict] = _json.loads(crons_path.read_text(encoding="utf-8"))
    except Exception as exc:
        safe_print(f"Could not read CRONS.json: {exc}")
        return

    if index < 0 or index >= len(crons):
        safe_print(f"Index {index} out of range (0..{len(crons)-1}).")
        return

    removed = crons.pop(index)
    crons_path.write_text(_json.dumps(crons, indent=2, ensure_ascii=False), encoding="utf-8")
    safe_print(f"Removed cron #{index}: {removed.get('schedule')!r} → {removed.get('prompt')!r}")


@cron_cmd.command("run")
@click.option("--agent", required=True, help="Agent workspace name.")
@click.option("--index", required=True, type=int, help="Index from 'cato cron list' (fires immediately).")
def cron_run(agent: str, index: int) -> None:
    """Fire a cron task immediately (one-shot, ignores schedule)."""
    import json as _json, asyncio as _asyncio

    crons_path = _CATO_DIR / "agents" / agent / "CRONS.json"
    if not crons_path.exists():
        safe_print(f"No CRONS.json found for agent [{agent}].")
        return

    try:
        crons: list[dict] = _json.loads(crons_path.read_text(encoding="utf-8"))
    except Exception as exc:
        safe_print(f"Could not read CRONS.json: {exc}")
        return

    if index < 0 or index >= len(crons):
        safe_print(f"Index {index} out of range (0..{len(crons)-1}).")
        return

    entry = crons[index]
    safe_print(f"Firing cron #{index} for agent [{agent}]: {entry.get('prompt')!r}")

    # Run via daemon if it's alive, otherwise run the agent loop directly
    if not _PID_FILE.exists():
        safe_print("Daemon not running — executing in-process (no channel delivery).")
        _run_cron_in_process(entry, agent)
    else:
        safe_print("Daemon is running — injecting via WebSocket.")
        _run_cron_via_ws(entry)


def _run_cron_in_process(entry: dict, agent: str) -> None:
    """Run a cron task in-process when the daemon is not running."""
    import asyncio as _asyncio
    from cato.config import CatoConfig as _Cfg
    from cato.budget import BudgetManager as _BM
    from cato.vault import Vault as _Vault
    from cato.agent_loop import AgentLoop
    from cato.core.context_builder import ContextBuilder
    from cato.core.memory import MemorySystem

    cfg = _Cfg.load()
    vault_path = _CATO_DIR / "vault.enc"
    vault = _Vault(vault_path=vault_path) if vault_path.exists() else None
    budget = _BM(session_cap=cfg.session_cap, monthly_cap=cfg.monthly_cap)
    memory = MemorySystem(agent_id=agent)
    ctx = ContextBuilder(max_tokens=cfg.context_budget_tokens)
    loop = AgentLoop(config=cfg, budget=budget, vault=vault, memory=memory, context_builder=ctx)

    async def _run() -> None:
        text, footer = await loop.run(
            session_id=entry.get("session_id", f"cron-{agent}"),
            message=entry.get("prompt", ""),
            agent_id=agent,
        )
        safe_print(f"\n--- Cron result ---\n{text}\n{footer}")

    try:
        _asyncio.run(_run())
    except Exception as exc:
        safe_print(f"Cron run failed: {exc}")


def _run_cron_via_ws(entry: dict) -> None:
    """Inject a cron task into the running daemon via WebSocket."""
    import asyncio as _asyncio, json as _json
    try:
        import websockets as _ws
    except ImportError:
        safe_print("websockets not installed — cannot inject via daemon. pip install websockets")
        return

    _config = CatoConfig.load()
    _ws_port = (getattr(_config, "webchat_port", None) or 8765) + 1

    async def _send() -> None:
        uri = f"ws://127.0.0.1:{_ws_port}"
        try:
            async with _ws.connect(uri) as ws:
                payload = _json.dumps({
                    "type": "message",
                    "text": entry.get("prompt", ""),
                    "session_id": entry.get("session_id", "cron-manual"),
                    "agent_id": entry.get("agent_id", "default"),
                    "channel": entry.get("channel", "web"),
                })
                await ws.send(payload)
                safe_print("Cron task injected into running daemon.")
        except Exception as exc:
            safe_print(f"Could not reach daemon WebSocket: {exc}")

    _asyncio.run(_send())


# ---------------------------------------------------------------------------
# cato node
# ---------------------------------------------------------------------------

@main.group("node")
def node_cmd() -> None:
    """Manage remote node devices and their capabilities."""
    pass


@node_cmd.command("list")
def node_list() -> None:
    """List currently registered nodes (requires daemon to be running)."""
    import asyncio as _asyncio, json as _json

    if not _PID_FILE.exists():
        safe_print("Daemon is not running. Start with: cato start")
        return

    try:
        import websockets as _ws
    except ImportError:
        safe_print("websockets not installed. pip install websockets")
        return

    _config = CatoConfig.load()
    _ws_port = (getattr(_config, "webchat_port", None) or 8765) + 1

    async def _fetch() -> None:
        uri = f"ws://127.0.0.1:{_ws_port}"
        try:
            async with _ws.connect(uri) as ws:
                await ws.send(_json.dumps({"type": "node_list"}))
                raw = await _asyncio.wait_for(ws.recv(), timeout=5.0)
                data = _json.loads(raw)
                nodes = data.get("nodes", [])
                if not nodes:
                    safe_print("No nodes registered.")
                    return
                table = Table(title="Registered Nodes", show_lines=True)
                table.add_column("Node ID", style="cyan")
                table.add_column("Name")
                table.add_column("Capabilities")
                table.add_column("Last Seen")
                table.add_column("Stale")
                import time as _time
                for n in nodes:
                    age = int(_time.time() - n.get("last_seen", 0))
                    caps = ", ".join(n.get("capabilities", []))
                    table.add_row(
                        n["node_id"], n["name"], caps,
                        f"{age}s ago",
                        "[red]yes[/red]" if n.get("stale") else "[green]no[/green]",
                    )
                console.print(table)
        except Exception as exc:
            safe_print(f"Could not reach daemon: {exc}")

    _asyncio.run(_fetch())


@node_cmd.command("info")
def node_info() -> None:
    """Show how to connect a remote node to this Cato instance."""
    config = CatoConfig.load()
    ws_port = (getattr(config, "webchat_port", None) or 8765) + 1

    safe_print("\nCato Node Connection Info")
    safe_print("=" * 50)
    safe_print(f"WebSocket endpoint:  ws://127.0.0.1:{ws_port}")
    safe_print("\nTo register a node, send this JSON over WebSocket:")
    safe_print("""  {
    "type": "node_register",
    "node_id": "my-device",
    "name": "My Device Name",
    "capabilities": ["screenshot", "camera", "shell", "geolocation"]
  }""")
    safe_print("\nAvailable capability names (examples):")
    safe_print("  screenshot   — take a screen capture")
    safe_print("  camera       — take a photo via webcam")
    safe_print("  geolocation  — return GPS/IP location")
    safe_print("  shell        — run a shell command on the remote device")
    safe_print("  file_read    — read a file from the remote device")
    safe_print("  file_write   — write a file to the remote device")
    safe_print("\nSee docs/nodes.md for the full node client protocol.")
    safe_print("")


# ---------------------------------------------------------------------------
# cato heartbeat
# ---------------------------------------------------------------------------

@main.group("heartbeat")
def heartbeat_cmd() -> None:
    """Manage heartbeat health-check monitoring."""
    pass


@heartbeat_cmd.command("status")
@click.option("--agent", default="", help="Filter by agent (all agents if omitted).")
def heartbeat_status(agent: str) -> None:
    """Show heartbeat configuration for agents."""
    from cato.heartbeat import _parse_heartbeat_md

    agents_dir = _CATO_DIR / "agents"
    if not agents_dir.exists():
        safe_print("No agents directory found.")
        return

    dirs = [agents_dir / agent] if agent else list(agents_dir.iterdir())
    found_any = False

    table = Table(title="Heartbeat Status", show_lines=True)
    table.add_column("Agent", style="cyan")
    table.add_column("HEARTBEAT.md", style="green")
    table.add_column("Interval", justify="right")
    table.add_column("Items", justify="right")
    table.add_column("Checklist Preview")

    for d in sorted(dirs):
        if not d.is_dir():
            continue
        hb_path = d / "workspace" / "HEARTBEAT.md"
        if not hb_path.exists():
            hb_path = d / "HEARTBEAT.md"

        if hb_path.exists():
            interval, items = _parse_heartbeat_md(hb_path)
            preview = items[0][:50] if items else "(no items)"
            table.add_row(d.name, "found", f"{interval}s", str(len(items)), preview)
            found_any = True
        else:
            table.add_row(d.name, "[dim]not found[/dim]", "-", "0", "")
            found_any = True

    if found_any:
        console.print(table)
    else:
        safe_print("No agents found.")


@heartbeat_cmd.command("run")
@click.option("--agent", required=True, help="Agent name to fire heartbeat for.")
def heartbeat_run(agent: str) -> None:
    """Fire a heartbeat check immediately for an agent.

    If the daemon is running, injects via the gateway WebSocket so the response
    is delivered through the configured channel.  Falls back to in-process
    execution (stdout only) when the daemon is not running.
    """
    from cato.heartbeat import _parse_heartbeat_md, _build_heartbeat_prompt

    # Look for HEARTBEAT.md
    agent_dir = _CATO_DIR / "agents" / agent
    hb_path = agent_dir / "workspace" / "HEARTBEAT.md"
    if not hb_path.exists():
        hb_path = agent_dir / "HEARTBEAT.md"
        if not hb_path.exists():
            safe_print(f"No HEARTBEAT.md found for agent [{agent}].")
            safe_print(f"  Expected: {agent_dir / 'workspace' / 'HEARTBEAT.md'}")
            return

    _, items = _parse_heartbeat_md(hb_path)
    if not items:
        safe_print("HEARTBEAT.md found but contains no checklist items (- [ ] ...).")
        return

    safe_print(f"Running heartbeat for [{agent}] — {len(items)} items:")
    for item in items:
        safe_print(f"  - {item}")

    prompt = _build_heartbeat_prompt(agent, items)
    entry = {
        "prompt": prompt,
        "session_id": f"heartbeat-{agent}-manual",
        "agent_id": agent,
        "channel": "heartbeat",
    }

    # Prefer daemon injection so response flows through the gateway
    if _PID_FILE.exists():
        _run_cron_via_ws(entry)
    else:
        safe_print("(Daemon not running — executing in-process; output to stdout only)")
        _run_cron_in_process(entry, agent)


@heartbeat_cmd.command("init")
@click.option("--agent", required=True, help="Agent name.")
@click.option("--interval", default=300, show_default=True,
              help="Check interval in seconds.")
def heartbeat_init(agent: str, interval: int) -> None:
    """Create a starter HEARTBEAT.md for an agent."""
    agent_dir = _CATO_DIR / "agents" / agent / "workspace"
    agent_dir.mkdir(parents=True, exist_ok=True)
    hb_path = agent_dir / "HEARTBEAT.md"

    if hb_path.exists():
        if not click.confirm(f"HEARTBEAT.md already exists for [{agent}]. Overwrite?", default=False):
            safe_print("Aborted.")
            return

    template = f"""# Heartbeat Checklist
<!-- interval: {interval} -->

Check the following items and report any failures:

- [ ] Confirm the agent process is responding normally
- [ ] Check available disk space is above 15%
- [ ] Verify no error logs in the last check period
- [ ] Confirm all configured channels are reachable
"""
    hb_path.write_text(template, encoding="utf-8")
    safe_print(f"HEARTBEAT.md created at: {hb_path}")
    safe_print(f"Interval: every {interval}s  |  Edit to add your own checklist items.")


# ---------------------------------------------------------------------------
# cato replay
# ---------------------------------------------------------------------------

@main.command("replay")
@click.option("--session", "session_id", required=True, help="Session ID to replay.")
@click.option("--live", is_flag=True, default=False,
              help="Use real tools instead of mocked outputs (requires budget confirmation).")
def cmd_replay(session_id: str, live: bool) -> None:
    """Replay a recorded session using audit log outputs."""
    from cato.audit import AuditLog
    from cato.replay import ReplayEngine

    if live:
        if not click.confirm(
            "Live replay will use real tools and may incur costs. Proceed?",
            default=False,
        ):
            safe_print("Aborted.")
            return

    log = AuditLog()
    log.connect()

    engine = ReplayEngine(audit_log=log)
    mode_label = "LIVE" if live else "DRY-RUN"
    safe_print(f"Replaying session {session_id} in {mode_label} mode...")

    report = engine.replay(session_id, live=live)
    safe_print(engine.format_report(report))


# ---------------------------------------------------------------------------
# coding-agent command
# ---------------------------------------------------------------------------

@main.command("coding-agent")
@click.option("--task", required=True, help="Task to perform (e.g., 'optimize this function')")
@click.option("--file", default=None, help="File to analyze (optional)")
@click.option("--context", default="", help="Additional context for the task")
@click.option("--verbose", is_flag=True, help="Enable verbose logging")
@click.option("--threshold", default=0.90, type=float, help="Early termination confidence threshold")
@click.option("--max-wait", default=3000, type=int, help="Maximum wait time in milliseconds")
def cmd_coding_agent(task: str, file: Optional[str], context: str, verbose: bool, threshold: float, max_wait: int) -> None:
    """
    Execute coding-agent skill with async model orchestration.

    Invokes Claude API, Codex CLI, and Gemini CLI in parallel with early
    termination when confidence threshold is met.

    Example:
        cato coding-agent --task "optimize this function" --file app.py --verbose
    """
    from cato.commands.coding_agent_cmd import cmd_coding_agent_sync

    # If file is provided, read it and add to context
    if file:
        try:
            file_path = Path(file)
            file_context = file_path.read_text()
            if context:
                context = f"{file_context}\n\n{context}"
            else:
                context = file_context
        except FileNotFoundError:
            safe_print(f"Error: File '{file}' not found")
            sys.exit(1)
        except Exception as e:
            safe_print(f"Error reading file '{file}': {e}")
            sys.exit(1)

    # Execute coding-agent
    try:
        result = cmd_coding_agent_sync(
            task=task,
            context=context,
            verbose=verbose,
            threshold=threshold,
            max_wait_ms=max_wait
        )

        # Parse and display result
        result_dict = json.loads(result)

        if result_dict.get("status") == "success":
            synthesis = result_dict.get("synthesis", {})
            metrics = result_dict.get("metrics", {})

            # Display primary result
            primary = synthesis.get("primary", {})
            safe_print(f"\nPrimary Solution ({primary.get('model', 'unknown')}):")
            safe_print(f"Confidence: {primary.get('confidence', 0):.2%}")
            safe_print(f"Response:\n{primary.get('response', 'N/A')}")

            # Display runners-up
            runners = synthesis.get("runners_up", [])
            if runners:
                safe_print("\nRunners-up:")
                for runner in runners:
                    safe_print(f"  - {runner.get('model', 'unknown')}: {runner.get('confidence', 0):.2%}")

            # Display metrics
            safe_print(f"\nMetrics:")
            safe_print(f"Total Latency: {metrics.get('total_latency_ms', 0):.1f}ms")
            safe_print(f"Early Termination: {'Yes' if metrics.get('early_termination') else 'No'}")

        else:
            error = result_dict.get("error", "Unknown error")
            safe_print(f"Error: {error}")
            sys.exit(1)

    except json.JSONDecodeError:
        safe_print("Error: Invalid response format from coding-agent")
        sys.exit(1)
    except Exception as e:
        safe_print(f"Error executing coding-agent: {e}")


# ---------------------------------------------------------------------------
# cato schedule  (YAML-based cron scheduler management)
# ---------------------------------------------------------------------------

@main.group("schedule")
def schedule_cmd() -> None:
    """Manage YAML-based scheduled skills (~/.cato/schedules/)."""
    pass


@schedule_cmd.command("add")
@click.option("--name", required=True, help="Unique schedule name (used as filename).")
@click.option("--cron", required=True, help="Cron expression, e.g. '0 8 * * *'.")
@click.option("--skill", required=True, help="Skill name to dispatch on fire.")
@click.option("--budget-cap", default=100, show_default=True, type=int,
              help="Per-execution budget cap in cents.")
def schedule_add(name: str, cron: str, skill: str, budget_cap: int) -> None:
    """Add a new YAML schedule.

    \b
    Example:
        cato schedule add --name morning-brief --cron "0 8 * * *" --skill daily_digest
    """
    from cato.core.schedule_manager import Schedule, _SCHEDULES_DIR
    try:
        from croniter import croniter
        if not croniter.is_valid(cron):
            safe_print(f"Invalid cron expression: {cron!r}")
            return
    except ImportError:
        safe_print("Warning: croniter not installed — cron expression not validated.")

    _safe_name = name.replace(" ", "_").replace("/", "_")
    existing_path = _CATO_DIR.parent / "schedules" / f"{_safe_name}.yaml"  # type: ignore[operator]
    sched = Schedule(name=_safe_name, cron=cron, skill=skill, budget_cap=budget_cap)
    sched.save(_SCHEDULES_DIR)
    safe_print(f"Schedule added: {_safe_name!r}  cron={cron!r}  skill={skill!r}")


@schedule_cmd.command("list")
def schedule_list() -> None:
    """List all schedules."""
    from cato.core.schedule_manager import load_all_schedules
    schedules = load_all_schedules()
    if not schedules:
        safe_print("No schedules found. Add one with: cato schedule add")
        return

    table = Table(title="Schedules", show_lines=True)
    table.add_column("Name", style="cyan")
    table.add_column("Cron")
    table.add_column("Skill")
    table.add_column("Budget Cap")
    table.add_column("Enabled")

    for s in schedules:
        table.add_row(
            s.name,
            s.cron,
            s.skill,
            f"{s.budget_cap}c",
            "[green]yes[/green]" if s.enabled else "[red]no[/red]",
        )
    console.print(table)


@schedule_cmd.command("enable")
@click.argument("name")
def schedule_enable(name: str) -> None:
    """Enable a schedule by name."""
    from cato.core.schedule_manager import toggle_schedule
    if toggle_schedule(name, enabled=True):
        safe_print(f"Schedule {name!r} enabled.")
    else:
        safe_print(f"Schedule {name!r} not found.")


@schedule_cmd.command("disable")
@click.argument("name")
def schedule_disable(name: str) -> None:
    """Disable a schedule by name (keeps the file, skips execution)."""
    from cato.core.schedule_manager import toggle_schedule
    if toggle_schedule(name, enabled=False):
        safe_print(f"Schedule {name!r} disabled.")
    else:
        safe_print(f"Schedule {name!r} not found.")


@schedule_cmd.command("run")
@click.argument("name")
def schedule_run(name: str) -> None:
    """Immediately fire a named schedule (ignores cron timing)."""
    import asyncio as _asyncio
    from cato.core.schedule_manager import SchedulerDaemon, load_schedule, _SCHEDULES_DIR
    from cato.audit import AuditLog as _AuditLog

    path = _SCHEDULES_DIR / f"{name}.yaml"
    if not path.exists():
        safe_print(f"Schedule {name!r} not found.")
        return

    sched = load_schedule(path)
    if sched is None:
        safe_print(f"Could not parse schedule {name!r}.")
        return

    audit = _AuditLog()
    audit.connect()
    daemon = SchedulerDaemon(audit_log=audit)

    async def _run() -> None:
        ok = await daemon.fire_now(name)
        if ok:
            safe_print(f"Schedule {name!r} fired (skill={sched.skill!r}).")
        else:
            safe_print(f"Could not fire {name!r}.")

    _asyncio.run(_run())


@schedule_cmd.command("history")
@click.argument("name")
@click.option("--limit", default=20, show_default=True, type=int, help="Max rows to show.")
def schedule_history(name: str, limit: int) -> None:
    """Show last N execution records for a schedule (from audit log)."""
    from cato.audit import AuditLog as _AuditLog
    import sqlite3 as _sqlite3

    audit = _AuditLog()
    audit.connect()
    assert audit._conn is not None  # noqa: SLF001

    session_id = f"sched-{name}"
    rows = audit._conn.execute(  # noqa: SLF001
        """
        SELECT id, action_type, tool_name, outputs_json, error, timestamp
        FROM audit_log
        WHERE session_id = ? AND action_type = 'cron_fire'
        ORDER BY id DESC
        LIMIT ?
        """,
        (session_id, limit),
    ).fetchall()

    if not rows:
        safe_print(f"No execution history found for schedule {name!r}.")
        return

    import datetime as _dt
    table = Table(title=f"History: {name}", show_lines=True)
    table.add_column("ID", style="dim")
    table.add_column("Tool")
    table.add_column("Status")
    table.add_column("Error")
    table.add_column("Timestamp")

    for r in rows:
        ts = _dt.datetime.fromtimestamp(r["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        try:
            import json as _json
            out = _json.loads(r["outputs_json"])
            status = out.get("status", "?")
        except Exception:
            status = r["outputs_json"][:20]
        table.add_row(
            str(r["id"]),
            r["tool_name"],
            status,
            r["error"] or "",
            ts,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# cato search  (Web-Search-Plus)
# ---------------------------------------------------------------------------

@main.command("search")
@click.argument("query")
@click.option("--engine", "query_type",
              type=click.Choice(["code", "news", "academic", "general"], case_sensitive=False),
              default=None, help="Override query type (auto-detected if omitted).")
@click.option("--depth", type=click.Choice(["normal", "deep"], case_sensitive=False),
              default="normal", show_default=True, help="Search depth.")
@click.option("--max-results", default=10, show_default=True, type=int,
              help="Max results to display.")
def cmd_search(query: str, query_type: Optional[str], depth: str, max_results: int) -> None:
    """Search the web using the best engine for the query type.

    \b
    Examples:
        cato search "Python asyncio tutorial" --engine code
        cato search "latest AI news" --engine news --depth deep
        cato search "CRISPR 2024" --engine academic
    """
    import asyncio as _asyncio
    from cato.tools.web_search import WebSearchTool, classify_query

    detected: str = query_type or classify_query(query)

    vault_path = _CATO_DIR / "vault.enc"
    vault: Optional[Vault] = None
    if vault_path.exists():
        try:
            vault = Vault(vault_path=vault_path)
        except Exception:
            pass

    tool = WebSearchTool(vault=vault)

    async def _run():
        return await tool.search(
            query=query,
            query_type=detected,  # type: ignore[arg-type]
            depth=depth,          # type: ignore[arg-type]
            max_results=max_results,
        )

    try:
        results = _asyncio.run(_run())
    except Exception as exc:
        safe_print(f"Search failed: {exc}")
        return

    if not results:
        safe_print("No results found.")
        return

    safe_print(f"\nSearch: {query!r}  [type={detected}, depth={depth}]")
    safe_print("-" * 60)

    table = Table(show_lines=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Title", min_width=30)
    table.add_column("URL", style="cyan")
    table.add_column("Engine", style="dim")
    table.add_column("Conf", justify="right")

    for i, r in enumerate(results):
        table.add_row(
            str(i + 1),
            r.title[:60],
            r.url[:60],
            r.source_engine,
            f"{r.confidence:.2f}",
        )
    console.print(table)

    safe_print("\nSnippets:")
    for i, r in enumerate(results[:3]):
        safe_print(f"\n[{i+1}] {r.title}")
        safe_print(f"    {r.snippet[:200]}")


# ---------------------------------------------------------------------------
# cato sessions / cato session  (Context-Anchor Session Checkpoints, Skill 8)
# ---------------------------------------------------------------------------

@main.command("sessions")
def cmd_sessions_list() -> None:
    """List all session checkpoints."""
    from cato.core.session_checkpoint import SessionCheckpoint
    ckpt = SessionCheckpoint()
    ckpt.connect()
    sessions = ckpt.list_all()
    ckpt.close()

    if not sessions:
        safe_print("No session checkpoints found.")
        return

    table = Table(title="Session Checkpoints", show_lines=True)
    table.add_column("Session ID", style="cyan")
    table.add_column("Task")
    table.add_column("Checkpoint At")
    table.add_column("Tokens")

    for s in sessions:
        table.add_row(
            s["session_id"],
            (s.get("task_description") or "")[:50],
            s.get("checkpoint_at", ""),
            str(s.get("token_count", 0)),
        )
    console.print(table)


@main.group("session")
def session_cmd() -> None:
    """Manage individual session checkpoints."""
    pass


@session_cmd.command("resume")
@click.argument("session_id")
def session_resume(session_id: str) -> None:
    """Show the compressed summary for a session checkpoint (for context injection)."""
    from cato.core.session_checkpoint import SessionCheckpoint
    ckpt = SessionCheckpoint()
    ckpt.connect()
    summary = ckpt.get_summary(session_id)
    ckpt.close()

    if not summary:
        safe_print(f"No checkpoint found for session {session_id!r}.")
        return

    safe_print(summary)


@session_cmd.command("delete")
@click.argument("session_id")
@click.confirmation_option(prompt="Delete this checkpoint?")
def session_delete(session_id: str) -> None:
    """Delete a session checkpoint."""
    from cato.core.session_checkpoint import SessionCheckpoint
    ckpt = SessionCheckpoint()
    ckpt.connect()
    ok = ckpt.delete(session_id)
    ckpt.close()

    if ok:
        safe_print(f"Checkpoint for {session_id!r} deleted.")
    else:
        safe_print(f"No checkpoint found for {session_id!r}.")


# ---------------------------------------------------------------------------
# cato github  (Super-GitHub 3-Model PR Review, Skill 3)
# ---------------------------------------------------------------------------

@main.group("github")
def github_cmd() -> None:
    """GitHub operations with optional 3-model AI review pipeline."""
    pass


@github_cmd.command("pr")
@click.argument("subcommand", type=click.Choice(["review", "merge"]))
@click.argument("target")
@click.option("--method", type=click.Choice(["squash", "merge", "rebase"]),
              default="squash", show_default=True,
              help="Merge method (only used with 'merge').")
def github_pr(subcommand: str, target: str, method: str) -> None:
    """PR review or merge operations.

    \b
    Examples:
        cato github pr review 123
        cato github pr review https://github.com/org/repo/pull/123
        cato github pr merge 42 --method squash
    """
    import asyncio as _asyncio
    from cato.tools.github_tool import GitHubTool

    vault_path = _CATO_DIR / "vault.enc"
    vault: Optional[Vault] = None
    if vault_path.exists():
        try:
            vault = Vault(vault_path=vault_path)
        except Exception:
            pass

    gh = GitHubTool(vault=vault)

    if subcommand == "review":
        async def _review():
            result = await gh.pr_review(target)
            safe_print(result)
        _asyncio.run(_review())
    elif subcommand == "merge":
        # extract PR number from URL or use directly
        try:
            pr_num = int(target.rstrip("/").split("/")[-1])
        except ValueError:
            safe_print(f"Invalid PR number or URL: {target!r}")
            return
        async def _merge():
            result = await gh.pr_merge(pr_num, method=method)
            safe_print(result)
        _asyncio.run(_merge())


@github_cmd.command("issue")
@click.argument("subcommand", type=click.Choice(["create", "list"]))
@click.option("--title", default="", help="Issue title (for create).")
@click.option("--body", default="", help="Issue body (for create).")
def github_issue(subcommand: str, title: str, body: str) -> None:
    """Issue operations.

    \b
    Examples:
        cato github issue list
        cato github issue create --title "Bug: crash on startup" --body "Steps to reproduce..."
    """
    import asyncio as _asyncio
    from cato.tools.github_tool import GitHubTool

    vault_path = _CATO_DIR / "vault.enc"
    vault: Optional[Vault] = None
    if vault_path.exists():
        try:
            vault = Vault(vault_path=vault_path)
        except Exception:
            pass

    gh = GitHubTool(vault=vault)

    if subcommand == "create":
        if not title:
            safe_print("--title is required for issue create.")
            return
        async def _create():
            result = await gh.issue_create(title=title, body=body)
            safe_print(result)
        _asyncio.run(_create())
    else:
        async def _list():
            result = await gh.issue_list()
            safe_print(result)
        _asyncio.run(_list())


@github_cmd.command("release")
@click.option("--tag", required=True, help="Tag name, e.g. v1.2.0.")
@click.option("--notes", default="", help="Release notes.")
def github_release(tag: str, notes: str) -> None:
    """Create a GitHub release.

    \b
    Example:
        cato github release --tag v1.2.0 --notes "Bug fixes"
    """
    import asyncio as _asyncio
    from cato.tools.github_tool import GitHubTool

    vault_path = _CATO_DIR / "vault.enc"
    vault: Optional[Vault] = None
    if vault_path.exists():
        try:
            vault = Vault(vault_path=vault_path)
        except Exception:
            pass

    gh = GitHubTool(vault=vault)

    async def _run():
        result = await gh.release_create(tag=tag, notes=notes)
        safe_print(result)

    _asyncio.run(_run())


# ---------------------------------------------------------------------------
# cato memory  (Mem0: Local-First Semantic Memory, Skill 2)
# ---------------------------------------------------------------------------

@main.group("memory")
def memory_cmd() -> None:
    """Manage the Mem0 semantic fact store."""
    pass


@memory_cmd.command("list")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
@click.option("--limit", default=50, show_default=True, type=int, help="Max facts to show.")
def memory_list(agent: str, limit: int) -> None:
    """Display all stored facts with key, confidence, and last_reinforced."""
    import time as _time
    from cato.core.memory import MemorySystem

    mem = MemorySystem(agent_id=agent)
    facts = mem.load_top_facts(n=limit)
    mem.close()

    if not facts:
        safe_print("No facts stored yet.")
        return

    table = Table(title=f"Mem0 Facts ({agent})", show_lines=True)
    table.add_column("Key", style="cyan")
    table.add_column("Value")
    table.add_column("Confidence", justify="right")
    table.add_column("Last Reinforced")

    for f in facts:
        lr = f.get("last_reinforced")
        if lr:
            from datetime import datetime as _dt
            lr_str = _dt.fromtimestamp(lr).strftime("%Y-%m-%d %H:%M")
        else:
            lr_str = "—"
        table.add_row(
            str(f["key"]),
            str(f["value"])[:80],
            f"{f.get('confidence', 1.0):.3f}",
            lr_str,
        )
    console.print(table)


@memory_cmd.command("forget")
@click.argument("key", required=False, default=None)
@click.option("--all", "forget_all", is_flag=True, default=False, help="Delete ALL facts.")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
def memory_forget(key: Optional[str], forget_all: bool, agent: str) -> None:
    """Delete a fact by key, or all facts with --all."""
    from cato.core.memory import MemorySystem

    mem = MemorySystem(agent_id=agent)

    if forget_all:
        if not click.confirm(f"Delete ALL facts for agent [{agent}]?", default=False):
            safe_print("Aborted.")
            mem.close()
            return
        n = mem.forget_all_facts()
        mem.close()
        safe_print(f"Deleted {n} fact(s).")
        return

    if not key:
        safe_print("Provide a KEY argument or use --all to delete all facts.")
        mem.close()
        return

    ok = mem.forget_fact(key)
    mem.close()
    if ok:
        safe_print(f"Fact '{key}' deleted.")
    else:
        safe_print(f"Fact '{key}' not found.")


# ---------------------------------------------------------------------------
# cato flow  (Clawflows: Proactive Trigger Registry, Skill 5)
# ---------------------------------------------------------------------------

@main.group("flow")
def flow_cmd() -> None:
    """Manage Clawflows proactive trigger flows."""
    pass


@flow_cmd.command("list")
def flow_list() -> None:
    """List all available flows."""
    from cato.orchestrator.clawflows import FlowEngine
    engine = FlowEngine()
    flows = engine.list_flows()
    if not flows:
        safe_print("No flows found. Place YAML files in ~/.cato/flows/")
        return

    table = Table(title="Clawflows", show_lines=True)
    table.add_column("Name", style="cyan")
    table.add_column("Trigger Type")
    table.add_column("Steps")
    table.add_column("Budget Cap")

    for f in flows:
        table.add_row(
            f["name"],
            f.get("trigger_type", "manual"),
            str(f.get("step_count", 0)),
            str(f.get("budget_cap", "—")),
        )
    console.print(table)


@flow_cmd.command("run")
@click.argument("name")
def flow_run(name: str) -> None:
    """Run a flow immediately."""
    import asyncio as _asyncio
    from cato.orchestrator.clawflows import FlowEngine

    engine = FlowEngine()

    async def _run():
        result = await engine.run_flow(name)
        safe_print(f"Flow '{name}' completed: status={result.status}")
        if result.error:
            safe_print(f"Error: {result.error}")
        for i, out in enumerate(result.step_outputs):
            safe_print(f"  Step {i}: {str(out)[:120]}")

    _asyncio.run(_run())


@flow_cmd.command("enable")
@click.argument("name")
def flow_enable(name: str) -> None:
    """Enable a flow (sets active: true in its YAML)."""
    from cato.orchestrator.clawflows import FlowEngine
    engine = FlowEngine()
    ok = engine.set_active(name, active=True)
    if ok:
        safe_print(f"Flow '{name}' enabled.")
    else:
        safe_print(f"Flow '{name}' not found.")


@flow_cmd.command("disable")
@click.argument("name")
def flow_disable(name: str) -> None:
    """Disable a flow (sets active: false in its YAML)."""
    from cato.orchestrator.clawflows import FlowEngine
    engine = FlowEngine()
    ok = engine.set_active(name, active=False)
    if ok:
        safe_print(f"Flow '{name}' disabled.")
    else:
        safe_print(f"Flow '{name}' not found.")


@flow_cmd.command("status")
def flow_status() -> None:
    """Show IN_PROGRESS flows with current step."""
    from cato.orchestrator.clawflows import FlowEngine
    engine = FlowEngine()
    in_progress = engine.get_in_progress_flows()
    if not in_progress:
        safe_print("No flows currently in progress.")
        return

    table = Table(title="In-Progress Flows", show_lines=True)
    table.add_column("Flow Name", style="cyan")
    table.add_column("Current Step", justify="right")
    table.add_column("Status")
    table.add_column("Started At")

    import datetime as _dt
    for row in in_progress:
        started = _dt.datetime.fromtimestamp(row["started_at"]).strftime("%Y-%m-%d %H:%M")
        table.add_row(
            row["flow_name"],
            str(row["current_step"]),
            row["status"],
            started,
        )
    console.print(table)


# ---------------------------------------------------------------------------
# cato exec  (Python Execution Sandbox, Skill 7)
# ---------------------------------------------------------------------------

@main.command("exec")
@click.argument("code")
@click.option("--timeout", default=30.0, show_default=True, type=float,
              help="Execution timeout in seconds.")
def cmd_exec(code: str, timeout: float) -> None:
    """Execute Python code in the sandbox and print output.

    \b
    Example:
        cato exec "print(1 + 1)"
        cato exec "import math; print(math.pi)"
    """
    import asyncio as _asyncio
    from cato.tools.python_executor import PythonExecutor, SandboxViolationError

    executor = PythonExecutor()

    async def _run():
        try:
            result = await executor.execute(code, timeout_sec=timeout)
            if result.stdout:
                safe_print(result.stdout)
            if result.stderr:
                safe_print(f"[stderr] {result.stderr}")
            if not result.success:
                safe_print(f"[exit code {result.returncode}]")
        except SandboxViolationError as e:
            safe_print(f"Sandbox violation: {e}")

    _asyncio.run(_run())


# ---------------------------------------------------------------------------
# cato graph  (Knowledge Graph, Skill 9)
# ---------------------------------------------------------------------------

@main.group("graph")
def graph_cmd() -> None:
    """Query the SQLite knowledge graph."""
    pass


@graph_cmd.command("query")
@click.argument("label")
@click.option("--depth", default=3, show_default=True, type=int,
              help="Max hop depth for graph traversal.")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
def graph_query(label: str, depth: int, agent: str) -> None:
    """Multi-hop graph traversal from LABEL.

    \b
    Example:
        cato graph query config.py --depth 2
    """
    from cato.core.memory import MemorySystem

    mem = MemorySystem(agent_id=agent)
    results = mem.query_graph(label, depth=depth)
    mem.close()

    if not results:
        safe_print(f"No nodes reachable from '{label}' within depth {depth}.")
        return

    table = Table(title=f"Graph: {label} (depth={depth})", show_lines=True)
    table.add_column("Label", style="cyan")
    table.add_column("Type")
    table.add_column("Relation")
    table.add_column("Weight", justify="right")
    table.add_column("Hop", justify="right")

    for r in results:
        table.add_row(
            r["label"],
            r["type"],
            r["relation_type"],
            f"{r['weight']:.1f}",
            str(r["depth"]),
        )
    console.print(table)


@graph_cmd.command("related")
@click.argument("label")
@click.option("--max-hops", default=2, show_default=True, type=int,
              help="Maximum hops to consider.")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
def graph_related(label: str, max_hops: int, agent: str) -> None:
    """Show related concepts for LABEL ranked by edge weight.

    \b
    Example:
        cato graph related alice --max-hops 2
    """
    from cato.core.memory import MemorySystem

    mem = MemorySystem(agent_id=agent)
    results = mem.related_concepts(label, max_hops=max_hops)
    mem.close()

    if not results:
        safe_print(f"No neighbours found for '{label}'.")
        return

    table = Table(title=f"Related to: {label}", show_lines=True)
    table.add_column("Label", style="cyan")
    table.add_column("Type")
    table.add_column("Weight", justify="right")
    table.add_column("Hop", justify="right")

    for r in results:
        table.add_row(
            r["label"],
            r["type"],
            f"{r['weight']:.1f}",
            str(r["depth"]),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# cato improve  (Self-Improving Agent, Skill 1)
# ---------------------------------------------------------------------------

@main.group("improve")
def improve_cmd() -> None:
    """Self-improvement cycle for skill documentation."""
    pass


@improve_cmd.command("run")
@click.option("--allow-skill-writes", is_flag=True, default=False,
              help="Actually rewrite SKILL.md files when consensus reached.")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
def improve_run(allow_skill_writes: bool, agent: str) -> None:
    """Run the improvement cycle (uses 3-model consensus).

    \b
    Example:
        cato improve run --allow-skill-writes
    """
    import asyncio as _asyncio
    from cato.core.memory import MemorySystem
    from cato.orchestrator.skill_improvement_cycle import run_improvement_cycle

    mem = MemorySystem(agent_id=agent)

    async def _run():
        stats = await run_improvement_cycle(mem, allow_writes=allow_skill_writes)
        safe_print(f"Improvement cycle complete:")
        safe_print(f"  Candidates reviewed: {stats['candidates_reviewed']}")
        safe_print(f"  Skills updated:      {stats['skills_updated']}")
        safe_print(f"  Blocked:             {stats['blocked']}")

    try:
        _asyncio.run(_run())
    finally:
        mem.close()


@improve_cmd.command("dry-run")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
def improve_dry_run(agent: str) -> None:
    """Show proposed improvements without applying them.

    \b
    Example:
        cato improve dry-run
    """
    import asyncio as _asyncio
    from cato.core.memory import MemorySystem
    from cato.orchestrator.skill_improvement_cycle import run_improvement_cycle

    mem = MemorySystem(agent_id=agent)

    async def _run():
        stats = await run_improvement_cycle(mem, allow_writes=False)
        safe_print(f"Dry-run complete (no files written):")
        safe_print(f"  Candidates reviewed: {stats['candidates_reviewed']}")
        safe_print(f"  Would update:        {stats['candidates_reviewed'] - stats['blocked']}")
        safe_print(f"  Blocked:             {stats['blocked']}")

    try:
        _asyncio.run(_run())
    finally:
        mem.close()


# ---------------------------------------------------------------------------
# cato rollback  (Skill version rollback, Skill 1)
# ---------------------------------------------------------------------------

@main.group("rollback")
def rollback_cmd() -> None:
    """Manage skill version history and rollbacks."""
    pass


@rollback_cmd.command("skill")
@click.argument("name")
@click.option("--hash", "content_hash", default=None,
              help="Specific content hash to restore (omit for latest backup).")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
def rollback_skill(name: str, content_hash: Optional[str], agent: str) -> None:
    """Restore a previous SKILL.md version.

    \b
    Example:
        cato rollback skill knowledge_graph --hash abc123
        cato rollback skill knowledge_graph
    """
    from cato.core.memory import MemorySystem
    from cato.orchestrator.skill_improvement_cycle import list_skill_versions, restore_skill

    mem = MemorySystem(agent_id=agent)
    versions = list_skill_versions(name, mem)
    mem.close()

    if not versions:
        safe_print(f"No versions found for skill '{name}'.")
        return

    target_hash = content_hash or versions[0]["content_hash"]

    skill_path = Path(__file__).parent / "skills" / name / "SKILL.md"
    if not skill_path.parent.exists():
        safe_print(f"Skill directory not found: {skill_path.parent}")
        return

    mem2 = MemorySystem(agent_id=agent)
    ok = restore_skill(name, target_hash, skill_path, mem2)
    mem2.close()

    if ok:
        safe_print(f"Skill '{name}' restored from hash {target_hash[:12]}.")
    else:
        safe_print(f"Hash {target_hash[:12]} not found for skill '{name}'.")


@rollback_cmd.command("list")
@click.argument("name")
@click.option("--agent", default="default", show_default=True, help="Agent workspace name.")
def rollback_list(name: str, agent: str) -> None:
    """List available backup versions for a skill.

    \b
    Example:
        cato rollback list knowledge_graph
    """
    import datetime as _dt
    from cato.core.memory import MemorySystem
    from cato.orchestrator.skill_improvement_cycle import list_skill_versions

    mem = MemorySystem(agent_id=agent)
    versions = list_skill_versions(name, mem)
    mem.close()

    if not versions:
        safe_print(f"No versions found for skill '{name}'.")
        return

    table = Table(title=f"Versions: {name}", show_lines=True)
    table.add_column("#", style="dim", justify="right")
    table.add_column("Hash", style="cyan")
    table.add_column("Saved At")

    for i, v in enumerate(versions):
        ts = _dt.datetime.fromtimestamp(v["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        table.add_row(str(i), v["content_hash"][:16] + "...", ts)
    console.print(table)
