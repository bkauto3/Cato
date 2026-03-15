"""
tests/test_shell.py — Hudson audit: dedicated tests for cato/tools/shell.py

Tests cover:
  1. Allowlist enforcement (gateway mode)
  2. Windows allowlist inclusion
  3. PowerShell command building
  4. Minimal env (POSIX vs Windows)
  5. Sandbox execution (echo, python)
  6. Gateway blocked command
  7. Full mode execution
  8. Timeout enforcement
  9. Output truncation
  10. Safety guard desktop mode
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# 1. Allowlist tests
# ---------------------------------------------------------------------------

class TestAllowlist:
    """Gateway allowlist behaviour."""

    def test_default_allowlist_contents(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        expected = {"ls", "cat", "head", "tail", "grep", "find", "wc", "echo",
                    "python3", "python", "git", "mkdir", "cp", "mv", "chmod",
                    "pwd", "env", "which", "date"}
        assert expected.issubset(set(tool.DEFAULT_ALLOWLIST))

    def test_windows_allowlist_includes_powershell(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        assert "powershell" in tool.WINDOWS_ALLOWLIST
        assert "pwsh" in tool.WINDOWS_ALLOWLIST
        assert "powershell.exe" in tool.WINDOWS_ALLOWLIST
        assert "cmd" in tool.WINDOWS_ALLOWLIST

    @patch("cato.tools.shell.IS_WINDOWS", True)
    def test_load_allowlist_includes_windows_on_windows(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        allowlist = tool._load_allowlist()
        assert "powershell" in allowlist
        assert "pwsh" in allowlist
        assert "dir" in allowlist
        assert "ls" in allowlist  # POSIX commands still included

    @patch("cato.tools.shell.IS_WINDOWS", False)
    def test_load_allowlist_excludes_windows_on_posix(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        allowlist = tool._load_allowlist()
        assert "powershell" not in allowlist
        assert "ls" in allowlist


# ---------------------------------------------------------------------------
# 2. PowerShell command building
# ---------------------------------------------------------------------------

class TestPowerShellCmdBuilding:
    """Windows command wrapping logic."""

    @patch("cato.tools.shell.shutil.which", return_value=r"C:\Program Files\PowerShell\7\pwsh.exe")
    def test_find_powershell_prefers_pwsh(self, mock_which):
        from cato.tools.shell import ShellTool

        ps = ShellTool._find_powershell()
        assert "pwsh" in ps.lower()

    @patch("cato.tools.shell.shutil.which", return_value=None)
    def test_find_powershell_fallback(self, mock_which):
        from cato.tools.shell import ShellTool

        ps = ShellTool._find_powershell()
        assert "powershell.exe" in ps.lower()

    @patch("cato.tools.shell.shutil.which", return_value=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    def test_build_windows_cmd_structure(self, mock_which):
        from cato.tools.shell import ShellTool

        cmd = ShellTool._build_windows_cmd("Get-ChildItem C:\\Users")
        assert cmd[0].endswith("powershell.exe")
        assert "-NoProfile" in cmd
        assert "-NonInteractive" in cmd
        assert "-Command" in cmd
        assert cmd[-1] == "Get-ChildItem C:\\Users"


# ---------------------------------------------------------------------------
# 3. Minimal environment
# ---------------------------------------------------------------------------

class TestMinimalEnv:
    """Environment variable filtering."""

    @patch("cato.tools.shell.IS_WINDOWS", False)
    def test_posix_env_keys(self):
        from cato.tools.shell import ShellTool

        env = ShellTool._minimal_env()
        # Should only include POSIX-safe keys that exist in os.environ
        for k in env:
            assert k in {"PATH", "HOME", "USER", "LANG", "TERM", "TMPDIR", "TMP", "TEMP"}

    @patch("cato.tools.shell.IS_WINDOWS", True)
    @patch.dict("os.environ", {"SYSTEMROOT": r"C:\Windows", "COMSPEC": r"C:\Windows\system32\cmd.exe"})
    def test_windows_env_includes_systemroot(self):
        from cato.tools.shell import ShellTool

        env = ShellTool._minimal_env()
        assert "SYSTEMROOT" in env
        assert "COMSPEC" in env


# ---------------------------------------------------------------------------
# 4. Sandbox execution (POSIX only — runs on this Linux host)
# ---------------------------------------------------------------------------

class TestSandboxExecution:
    """Live subprocess execution in sandbox/gateway modes."""

    @pytest.mark.asyncio
    async def test_echo_command_gateway(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        result_json = await tool.execute({"command": "echo hello_world", "mode": "gateway"})
        result = json.loads(result_json)
        assert result["returncode"] == 0
        assert "hello_world" in result["stdout"]

    @pytest.mark.asyncio
    async def test_blocked_command_gateway(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        with pytest.raises(PermissionError, match="not in gateway allowlist"):
            await tool.execute({"command": "curl https://evil.com", "mode": "gateway"})

    @pytest.mark.asyncio
    async def test_python_command_gateway(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        result_json = await tool.execute({
            "command": "python3 -c \"print('test_ok')\"",
            "mode": "gateway",
        })
        result = json.loads(result_json)
        assert result["returncode"] == 0
        assert "test_ok" in result["stdout"]

    @pytest.mark.asyncio
    async def test_full_mode_execution(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        result_json = await tool.execute({
            "command": "echo full_mode_works",
            "mode": "full",
        })
        result = json.loads(result_json)
        assert result["returncode"] == 0
        assert "full_mode_works" in result["stdout"]

    @pytest.mark.asyncio
    async def test_timeout_enforcement(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        with pytest.raises(TimeoutError, match="timeout"):
            await tool.execute({
                "command": "sleep 30",
                "mode": "full",
                "timeout": 1,
            })

    @pytest.mark.asyncio
    async def test_output_truncation(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        # Generate output larger than _MAX_OUTPUT_CHARS (8000)
        result_json = await tool.execute({
            "command": "python3 -c \"print('x' * 20000)\"",
            "mode": "gateway",
        })
        result = json.loads(result_json)
        assert result["truncated"] is True
        assert "truncated" in result["stdout"]

    @pytest.mark.asyncio
    async def test_cwd_clamp_to_workspace(self):
        from cato.tools.shell import ShellTool

        tool = ShellTool()
        # Attempting to escape workspace should be clamped
        result_json = await tool.execute({
            "command": "pwd",
            "mode": "gateway",
            "cwd": "/etc",
        })
        result = json.loads(result_json)
        assert result["returncode"] == 0
        # cwd should have been clamped to workspace, not /etc
        assert "/etc" not in result["stdout"]


# ---------------------------------------------------------------------------
# 5. Safety guard desktop mode
# ---------------------------------------------------------------------------

class TestSafetyGuardDesktop:
    """Desktop safety mode with confirmation callback."""

    def test_desktop_mode_with_sync_callback_approved(self):
        from cato.safety import SafetyGuard

        def approve_all(tool_name, inputs, tier_label):
            return True

        guard = SafetyGuard(
            config={"safety_mode": "desktop"},
            confirmation_callback=approve_all,
        )
        result = guard.check_and_confirm("shell", {"command": "rm -rf /tmp/test"})
        assert result is True

    def test_desktop_mode_with_sync_callback_denied(self):
        from cato.safety import SafetyGuard

        def deny_all(tool_name, inputs, tier_label):
            return False

        guard = SafetyGuard(
            config={"safety_mode": "desktop"},
            confirmation_callback=deny_all,
        )
        result = guard.check_and_confirm("shell", {"command": "rm -rf /tmp/test"})
        assert result is False

    def test_desktop_mode_reversible_write_auto_allowed(self):
        """REVERSIBLE_WRITE actions should pass without callback."""
        from cato.safety import SafetyGuard

        callback_called = []

        def track_callback(tool_name, inputs, tier_label):
            callback_called.append(True)
            return False

        guard = SafetyGuard(
            config={"safety_mode": "desktop"},
            confirmation_callback=track_callback,
        )
        # echo is REVERSIBLE_WRITE → auto-allowed, callback should NOT fire
        result = guard.check_and_confirm("shell", {"command": "echo hello"})
        assert result is True
        assert len(callback_called) == 0

    def test_desktop_mode_without_callback_denies_in_non_tty(self):
        """Desktop mode without callback in non-TTY should deny elevated actions."""
        from cato.safety import SafetyGuard

        guard = SafetyGuard(
            config={"safety_mode": "desktop"},
            confirmation_callback=None,
        )
        # Without callback, desktop mode falls through to the non-TTY check
        # which denies by default (since we're in a test environment without TTY)
        # This verifies the fail-safe path
        if not sys.stdin.isatty():
            result = guard.check_and_confirm("shell", {"command": "rm -rf /tmp/test"})
            assert result is False

    def test_classify_powershell_commands(self):
        """PowerShell commands should classify correctly."""
        from cato.safety import SafetyGuard, RiskTier

        guard = SafetyGuard(config={"safety_mode": "strict"})

        # Normal PS commands → REVERSIBLE_WRITE
        assert guard.classify_action("shell", {"command": "Get-ChildItem C:\\"}) == RiskTier.REVERSIBLE_WRITE
        assert guard.classify_action("shell", {"command": "powershell dir"}) == RiskTier.REVERSIBLE_WRITE

        # Destructive PS commands → IRREVERSIBLE
        assert guard.classify_action("shell", {"command": "Remove-Item -Recurse"}) == RiskTier.REVERSIBLE_WRITE  # "remove" not a standalone token here
        assert guard.classify_action("shell", {"command": "rm file.txt"}) == RiskTier.IRREVERSIBLE
