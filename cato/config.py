"""
cato/config.py — Configuration management for CATO.

Loads and saves ~/.cato/config.yaml with defaults for all known fields.
First-run detection: returns defaults when the config file does not yet exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

import yaml

_CONFIG_FILE = Path.home() / ".cato" / "config.yaml"


@dataclass
class CatoConfig:
    """
    Full CATO configuration.

    All fields have safe defaults so CATO works out-of-the-box.
    Persist changes with :meth:`save`.
    """

    # Identity
    agent_name: str = "cato"

    # Model selection
    default_model: str = "claude-sonnet-4-6"

    # SwarmSync intelligent routing
    swarmsync_enabled: bool = False
    swarmsync_api_url: str = "https://api.swarmsync.ai/v1/chat/completions"

    # Budget caps (USD)
    session_cap: float = 1.00
    monthly_cap: float = 20.00

    # Workspace
    workspace_dir: str = str(Path.home() / ".cato" / "workspace")

    # Logging
    log_level: str = "INFO"

    # Messaging channels
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    whatsapp_enabled: bool = False
    webchat_port: int = 8765

    # Planning
    max_planning_turns: int = 2
    context_budget_tokens: int = 7000

    # Internal — path is excluded from YAML serialisation
    _path: Path = field(default_factory=lambda: _CONFIG_FILE, repr=False, compare=False)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "CatoConfig":
        """
        Load config from *config_path* (default ~/.cato/config.yaml).

        Missing fields fall back to dataclass defaults.
        If the file does not exist the default config is returned (first run).
        """
        path = config_path or _CONFIG_FILE
        instance = cls()
        instance._path = path

        if not path.exists():
            return instance  # first-run defaults

        try:
            raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return instance  # corrupted file — return defaults

        # Only set fields that are declared on the dataclass
        valid_names = {f.name for f in fields(cls) if not f.name.startswith("_")}
        for key, value in raw.items():
            if key in valid_names:
                setattr(instance, key, value)

        return instance

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, config_path: Optional[Path] = None) -> None:
        """Write current config to YAML file, creating parent dirs as needed."""
        path = config_path or self._path
        path.parent.mkdir(parents=True, exist_ok=True)

        # Serialise all public fields
        data: dict[str, Any] = {}
        for f in fields(self):
            if not f.name.startswith("_"):
                data[f.name] = getattr(self, f.name)

        path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def workspace_path(self) -> Path:
        """Return :attr:`workspace_dir` as a resolved Path object."""
        return Path(self.workspace_dir).expanduser().resolve()

    def is_first_run(self) -> bool:
        """Return True if no config file exists on disk."""
        return not self._path.exists()

    def to_dict(self) -> dict[str, Any]:
        """Serialise config to a plain dict (excluding private fields)."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }
