"""
cato/vault.py — Encrypted credential storage for CATO.

AES-256-GCM encryption with Argon2id key derivation.
Stores API keys, tokens, and passwords in ~/.cato/vault.enc.
Master password is prompted once on first run; derived key is cached in memory only.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import secrets
from pathlib import Path
from typing import Optional

from argon2.low_level import hash_secret_raw, Type
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VAULT_FILE = Path.home() / ".cato" / "vault.enc"
_SALT_SIZE = 32       # bytes — stored inside the vault file
_NONCE_SIZE = 12      # bytes — per-encryption nonce
_KEY_SIZE = 32        # bytes — AES-256

# Argon2id parameters (OWASP recommended minimum)
_ARGON2_TIME_COST = 3
_ARGON2_MEMORY_COST = 65536   # 64 MiB
_ARGON2_PARALLELISM = 4


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit key from *password* using Argon2id."""
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=_ARGON2_TIME_COST,
        memory_cost=_ARGON2_MEMORY_COST,
        parallelism=_ARGON2_PARALLELISM,
        hash_len=_KEY_SIZE,
        type=Type.ID,
    )


def _encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Return nonce || ciphertext using AES-256-GCM."""
    nonce = secrets.token_bytes(_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return nonce + ciphertext


def _decrypt(blob: bytes, key: bytes) -> bytes:
    """Decrypt nonce || ciphertext produced by _encrypt."""
    nonce = blob[:_NONCE_SIZE]
    ciphertext = blob[_NONCE_SIZE:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)


# ---------------------------------------------------------------------------
# Vault
# ---------------------------------------------------------------------------

class VaultError(Exception):
    """Raised on vault authentication or I/O failures."""


class Vault:
    """
    AES-256-GCM encrypted credential store.

    The on-disk layout of ``vault.enc`` is:

        base64( salt[32] + nonce[12] + aesgcm_ciphertext )

    The plaintext inside the ciphertext is a UTF-8 JSON object
    mapping string keys to string values.

    Usage::

        vault = Vault()
        vault.set("OPENAI_API_KEY", "sk-...")
        key = vault.get("OPENAI_API_KEY")
        vault.delete("OPENAI_API_KEY")
    """

    def __init__(self, vault_path: Optional[Path] = None) -> None:
        self._path: Path = vault_path or _VAULT_FILE
        self._key: Optional[bytes] = None          # in-memory only
        self._data: Optional[dict[str, str]] = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _prompt_password(self, confirm: bool = False) -> str:
        """Prompt for the master password, with optional confirmation."""
        password = getpass.getpass("Vault master password: ")
        if confirm:
            confirm_pw = getpass.getpass("Confirm master password: ")
            if password != confirm_pw:
                raise VaultError("Passwords do not match.")
        return password

    def _unlock(self) -> None:
        """Load and decrypt the vault, caching the key and data in memory."""
        if self._key is not None and self._data is not None:
            return  # already unlocked

        if not self._path.exists():
            # First run — create new vault
            password = self._prompt_password(confirm=True)
            salt = secrets.token_bytes(_SALT_SIZE)
            self._key = _derive_key(password, salt)
            self._data = {}
            self._save(salt)
            return

        # Existing vault
        raw = base64.b64decode(self._path.read_bytes())
        salt = raw[:_SALT_SIZE]
        blob = raw[_SALT_SIZE:]

        password = self._prompt_password(confirm=False)
        key = _derive_key(password, salt)

        try:
            plaintext = _decrypt(blob, key)
        except Exception as exc:
            raise VaultError("Wrong master password or corrupted vault.") from exc

        self._key = key
        self._data = json.loads(plaintext.decode("utf-8"))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self, salt: Optional[bytes] = None) -> None:
        """Encrypt current _data and write to disk."""
        assert self._key is not None and self._data is not None

        if salt is None:
            # Re-read existing salt from disk
            existing = base64.b64decode(self._path.read_bytes())
            salt = existing[:_SALT_SIZE]

        plaintext = json.dumps(self._data, ensure_ascii=True).encode("utf-8")
        blob = _encrypt(plaintext, self._key)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(base64.b64encode(salt + blob))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        """Return the stored value for *key*, or None if not found."""
        self._unlock()
        assert self._data is not None
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        """Store *value* under *key* and persist to disk."""
        self._unlock()
        assert self._data is not None
        self._data[key] = value
        self._save()

    def delete(self, key: str) -> bool:
        """Remove *key* from the vault. Returns True if it existed."""
        self._unlock()
        assert self._data is not None
        existed = key in self._data
        if existed:
            del self._data[key]
            self._save()
        return existed

    def list_keys(self) -> list[str]:
        """Return sorted list of stored key names (not values)."""
        self._unlock()
        assert self._data is not None
        return sorted(self._data.keys())

    def is_locked(self) -> bool:
        """Return True if the vault has not yet been unlocked this session."""
        return self._key is None

    @classmethod
    def create(cls, password: str, vault_path: Path | None = None) -> "Vault":
        """Create and initialize a new vault with the given password."""
        v = cls(vault_path)
        v.unlock(password)
        return v

    def unlock(self, password: str) -> None:
        """Unlock the vault with the given password (bypasses getpass prompt).

        Creates a new vault if the file does not yet exist.
        Raises VaultError on wrong password.
        """
        if self._key is not None and self._data is not None:
            return  # already unlocked

        if not self._path.exists():
            salt = secrets.token_bytes(_SALT_SIZE)
            self._key = _derive_key(password, salt)
            self._data = {}
            self._save(salt)
            return

        raw = base64.b64decode(self._path.read_bytes())
        salt = raw[:_SALT_SIZE]
        blob = raw[_SALT_SIZE:]
        key = _derive_key(password, salt)
        try:
            plaintext = _decrypt(blob, key)
        except Exception as exc:
            raise VaultError("Wrong master password or corrupted vault.") from exc
        self._key = key
        self._data = json.loads(plaintext.decode("utf-8"))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_vault_instance: Optional[Vault] = None


def get_vault() -> Vault:
    """Return the module-level Vault singleton."""
    global _vault_instance
    if _vault_instance is None:
        _vault_instance = Vault()
    return _vault_instance
