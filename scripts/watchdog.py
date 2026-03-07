"""
Cato Gateway Watchdog
=====================
Polls port 8080 (or CATO_PORT env var) every 30 seconds.
If the gateway is down, clears the stale PID file and restarts `cato start`.

Run continuously:  python scripts/watchdog.py
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT: int = int(os.environ.get("CATO_PORT", "8080"))
HOST: str = os.environ.get("CATO_HOST", "127.0.0.1")
POLL_INTERVAL: int = int(os.environ.get("CATO_WATCHDOG_INTERVAL", "30"))  # seconds
STARTUP_GRACE: int = int(os.environ.get("CATO_WATCHDOG_GRACE", "8"))      # seconds after restart

# Resolve cato data dir the same way the CLI does
try:
    from cato.platform import get_data_dir
    _CATO_DIR = Path(get_data_dir())
except Exception:
    _CATO_DIR = Path(os.environ.get("APPDATA", Path.home())) / "cato"

PID_FILE: Path = _CATO_DIR / "cato.pid"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_CATO_DIR / "watchdog.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("cato.watchdog")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _gateway_alive() -> bool:
    """Return True if the gateway is accepting TCP connections on HOST:PORT."""
    try:
        with socket.create_connection((HOST, PORT), timeout=3):
            return True
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    """Return True if a process with this PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _clear_stale_pid() -> None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
        except (ValueError, OSError):
            pid = None

        if pid is None or not _pid_alive(pid):
            PID_FILE.unlink(missing_ok=True)
            log.info("Cleared stale PID file (pid=%s)", pid)
        else:
            log.warning(
                "PID %s is still alive but port %s is not responding — leaving PID file",
                pid, PORT,
            )


def _start_gateway() -> None:
    """Launch `cato start` as a detached background process."""
    log.info("Starting cato gateway...")
    cato_exe = "cato"
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                [cato_exe, "start"],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                [cato_exe, "start"],
                start_new_session=True,
                close_fds=True,
            )
        log.info("cato start launched — waiting %ss for startup...", STARTUP_GRACE)
        time.sleep(STARTUP_GRACE)
    except FileNotFoundError:
        log.error("'cato' not found on PATH — is the package installed?")
    except Exception as exc:
        log.error("Failed to start gateway: %s", exc)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run() -> None:
    log.info("Watchdog started — monitoring %s:%s every %ss", HOST, PORT, POLL_INTERVAL)
    consecutive_failures = 0

    while True:
        if _gateway_alive():
            if consecutive_failures > 0:
                log.info("Gateway recovered after %s failed poll(s)", consecutive_failures)
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            log.warning("Gateway DOWN (failure #%s) — attempting restart", consecutive_failures)
            _clear_stale_pid()
            _start_gateway()

            if _gateway_alive():
                log.info("Gateway successfully restarted on %s:%s", HOST, PORT)
                consecutive_failures = 0
            else:
                log.error("Gateway still unreachable after restart attempt")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
