"""
cato/audit/ledger.py — Causal Action Ledger (Unbuilt Skill 1).

Hash-chained, Ed25519-signed tamper-evident record of every agent decision.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_GENESIS_PREV_HASH = "0" * 64  # Fixed genesis sentinel

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_records (
    seq                   INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id             TEXT NOT NULL UNIQUE,
    prev_hash             TEXT NOT NULL,
    timestamp             TEXT NOT NULL,
    agent_session_id      TEXT NOT NULL,
    tool_name             TEXT NOT NULL,
    tool_input_hash       TEXT NOT NULL,
    tool_output_hash      TEXT NOT NULL,
    reasoning_excerpt     TEXT NOT NULL DEFAULT '',
    confidence_score      REAL NOT NULL DEFAULT 0.0,
    model_source          TEXT NOT NULL DEFAULT 'claude',
    reversibility         REAL NOT NULL DEFAULT 0.5,
    delegation_token_id   TEXT,
    record_hash           TEXT NOT NULL,
    record_signature      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ledger_ts       ON ledger_records(timestamp);
CREATE INDEX IF NOT EXISTS idx_ledger_tool     ON ledger_records(tool_name);
CREATE INDEX IF NOT EXISTS idx_ledger_session  ON ledger_records(agent_session_id);
CREATE INDEX IF NOT EXISTS idx_ledger_token    ON ledger_records(delegation_token_id);
"""


@dataclass
class LedgerRecord:
    seq: int
    record_id: str
    prev_hash: str
    timestamp: str
    agent_session_id: str
    tool_name: str
    tool_input_hash: str
    tool_output_hash: str
    reasoning_excerpt: str
    confidence_score: float
    model_source: str
    reversibility: float
    delegation_token_id: Optional[str]
    record_hash: str
    record_signature: str


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _hash_json(obj: Any) -> str:
    return _sha256(json.dumps(obj, sort_keys=True, default=str))


class LedgerMiddleware:
    """
    Intercepts tool calls and appends signed records to the hash chain.

    Thread-safe via write lock. Blocking write before returning to caller.
    """

    def __init__(self, db_path: Optional[Path] = None, signing_key: Any = None) -> None:
        if db_path is None:
            from ..platform import get_data_dir
            db_path = get_data_dir() / "cato.db"
        self._db_path = db_path
        self._signing_key = signing_key  # Ed25519 SigningKey or None
        self._write_lock = threading.Lock()
        self._conn = self._open_db()

    def _open_db(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def _last_record_hash(self) -> str:
        row = self._conn.execute(
            "SELECT record_hash FROM ledger_records ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row["record_hash"] if row else _GENESIS_PREV_HASH

    def _sign(self, record_hash: str) -> str:
        if self._signing_key is None:
            return ""
        try:
            sig = self._signing_key.sign(record_hash.encode("utf-8"))
            return sig.signature.hex() if hasattr(sig, "signature") else sig.hex()
        except Exception as exc:
            logger.warning("Ledger signing failed: %s", exc)
            return ""

    def append(
        self,
        tool_name: str,
        tool_input: Any,
        tool_output: Any,
        agent_session_id: str,
        reasoning_excerpt: str = "",
        confidence_score: float = 0.0,
        model_source: str = "claude",
        reversibility: float = 0.5,
        delegation_token_id: Optional[str] = None,
    ) -> str:
        """Append a signed record to the chain. Returns record_id."""
        record_id = str(uuid.uuid4())
        now_ts = time.time()
        timestamp = (
            time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime(now_ts))
            + f"{int(now_ts * 1000) % 1000:03d}Z"
        )
        tool_input_hash = _hash_json(tool_input)
        tool_output_hash = _hash_json(tool_output)

        with self._write_lock:
            prev_hash = self._last_record_hash()

            # Build record hash from all fields
            record_data = "|".join([
                record_id, prev_hash, timestamp, agent_session_id,
                tool_name, tool_input_hash, tool_output_hash,
                reasoning_excerpt[:500], str(confidence_score),
                model_source, str(reversibility),
                delegation_token_id or "",
            ])
            record_hash = _sha256(record_data)
            record_signature = self._sign(record_hash)

            self._conn.execute(
                """INSERT INTO ledger_records
                   (record_id, prev_hash, timestamp, agent_session_id, tool_name,
                    tool_input_hash, tool_output_hash, reasoning_excerpt,
                    confidence_score, model_source, reversibility,
                    delegation_token_id, record_hash, record_signature)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id, prev_hash, timestamp, agent_session_id, tool_name,
                    tool_input_hash, tool_output_hash, reasoning_excerpt[:500],
                    confidence_score, model_source, reversibility,
                    delegation_token_id, record_hash, record_signature,
                ),
            )
            self._conn.commit()

        logger.debug("Ledger record appended: %s (tool=%s)", record_id, tool_name)
        return record_id

    def close(self) -> None:
        self._conn.close()


class LedgerQuery:
    """Query interface for the ledger chain."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            from ..platform import get_data_dir
            db_path = get_data_dir() / "cato.db"
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def _row_to_record(self, row: sqlite3.Row) -> LedgerRecord:
        d = dict(row)
        return LedgerRecord(**d)

    def by_time_range(self, start: float, end: float) -> list[LedgerRecord]:
        # Use prefix-friendly bounds: start without suffix (lexicographically earlier),
        # end with trailing "Z~" so millisecond variants within the last second are included.
        start_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(start))
        end_s = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(end)) + "Z~"
        rows = self._conn.execute(
            "SELECT * FROM ledger_records WHERE timestamp >= ? AND timestamp <= ? ORDER BY seq",
            (start_s, end_s),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def by_tool(self, tool_name: str) -> list[LedgerRecord]:
        rows = self._conn.execute(
            "SELECT * FROM ledger_records WHERE tool_name = ? ORDER BY seq",
            (tool_name,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def by_session(self, session_id: str) -> list[LedgerRecord]:
        rows = self._conn.execute(
            "SELECT * FROM ledger_records WHERE agent_session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def by_confidence_below(self, threshold: float) -> list[LedgerRecord]:
        rows = self._conn.execute(
            "SELECT * FROM ledger_records WHERE confidence_score < ? ORDER BY seq",
            (threshold,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def by_delegation_token(self, token_id: str) -> list[LedgerRecord]:
        rows = self._conn.execute(
            "SELECT * FROM ledger_records WHERE delegation_token_id = ? ORDER BY seq",
            (token_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def replay_session(self, session_id: str) -> list[dict]:
        records = self.by_session(session_id)
        return [
            {
                "record_id": r.record_id,
                "timestamp": r.timestamp,
                "tool_name": r.tool_name,
                "reasoning_excerpt": r.reasoning_excerpt,
                "confidence_score": r.confidence_score,
                "reversibility": r.reversibility,
            }
            for r in records
        ]

    def last_n(self, n: int) -> list[LedgerRecord]:
        rows = self._conn.execute(
            "SELECT * FROM ledger_records ORDER BY seq DESC LIMIT ?", (n,)
        ).fetchall()
        return [self._row_to_record(r) for r in reversed(rows)]

    def close(self) -> None:
        self._conn.close()


def verify_chain(db_path: Optional[Path] = None) -> tuple[bool, str]:
    """
    Walk the full chain and verify hash linkage.

    Returns (True, "VALID (N records...)") or
    (False, "TAMPERED at record {id} — {reason}").
    """
    if db_path is None:
        from ..platform import get_data_dir
        db_path = get_data_dir() / "cato.db"

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM ledger_records ORDER BY seq ASC"
    ).fetchall()
    conn.close()

    if not rows:
        return True, "VALID (0 records, empty chain)"

    expected_prev = _GENESIS_PREV_HASH
    for i, row in enumerate(rows):
        # Verify prev_hash linkage
        if row["prev_hash"] != expected_prev:
            return False, (
                f"TAMPERED at record {row['record_id']} (index {i}) — "
                f"prev_hash mismatch: expected {expected_prev[:16]}…, "
                f"got {row['prev_hash'][:16]}…"
            )

        # Verify record_hash matches re-computed hash of all fields
        expected_hash = _sha256("|".join([
            row["record_id"], row["prev_hash"], row["timestamp"],
            row["agent_session_id"], row["tool_name"],
            row["tool_input_hash"], row["tool_output_hash"],
            row["reasoning_excerpt"], str(row["confidence_score"]),
            row["model_source"], str(row["reversibility"]),
            row["delegation_token_id"] or "",
        ]))
        if expected_hash != row["record_hash"]:
            return False, (
                f"TAMPERED at record {row['record_id']} (index {i}) — "
                f"field hash mismatch: stored {row['record_hash'][:16]}…, "
                f"recomputed {expected_hash[:16]}…"
            )

        expected_prev = row["record_hash"]

    return True, f"VALID ({len(rows)} records, chain intact)"
