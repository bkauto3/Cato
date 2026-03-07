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

from .platform import get_data_dir

_CONFIG_FILE = get_data_dir() / "config.yaml"


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
    workspace_dir: str = str(get_data_dir() / "workspace")

    # Logging
    log_level: str = "INFO"

    # Messaging channels
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    whatsapp_enabled: bool = False
    webchat_port: int = 8080

    # Planning
    max_planning_turns: int = 2
    context_budget_tokens: int = 7000

    # Conduit browser engine (opt-in)
    conduit_enabled: bool = False
    conduit_budget_per_session: int = 100   # cents
    conduit_extract_max_chars: int = 20_000
    searxng_url: str = ""
    search_rerank_enabled: bool = False
    conduit_crawl_delay_sec: float = 1.0
    conduit_crawl_max_delay_sec: float = 60.0
    selector_healing_enabled: bool = False
    vault: Optional[dict] = None   # API keys / credentials for search, login, etc.

    # Safety gates
    safety_mode: str = "strict"             # strict | permissive | off

    # Budget forecast
    budget_forecast_enabled: bool = True    # show cost estimate before tasks

    # Audit log
    audit_enabled: bool = True              # append-only action log

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

    def get(self, key: str, default: Any = None) -> Any:
        """Vault-style get for API keys (used by WebSearchTool and Conduit login)."""
        if self.vault and isinstance(self.vault, dict):
            return self.vault.get(key, default)
        return default

    def to_conduit_bridge_config(
        self,
        session_id: str,
        data_dir: Optional[str] = None,
        conduit_budget_per_session: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Build config dict for ConduitBridge so bridge _config drives Conduit behavior.

        Use when creating the bridge (e.g. when conduit_enabled)::

            bridge = ConduitBridge(
                cfg.to_conduit_bridge_config(
                    session_id,
                    data_dir=str(get_data_dir()),
                    conduit_budget_per_session=cfg.conduit_budget_per_session,
                ),
                session_id,
            )
        """
        out: dict[str, Any] = {
            "session_id": session_id,
            "conduit_extract_max_chars": self.conduit_extract_max_chars,
            "searxng_url": self.searxng_url or "",
            "search_rerank_enabled": self.search_rerank_enabled,
            "conduit_crawl_delay_sec": self.conduit_crawl_delay_sec,
            "conduit_crawl_max_delay_sec": self.conduit_crawl_max_delay_sec,
            "selector_healing_enabled": self.selector_healing_enabled,
            "vault": self.vault,
        }
        if data_dir is not None:
            out["data_dir"] = data_dir
        if conduit_budget_per_session is not None:
            out["conduit_budget_per_session"] = conduit_budget_per_session
        return out

    def to_dict(self) -> dict[str, Any]:
        """Serialise config to a plain dict (excluding private fields)."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }
