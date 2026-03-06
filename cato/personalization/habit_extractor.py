"""
cato/personalization/habit_extractor.py — Habit Pattern Extractor (Unbuilt Skill 10).

Passively observes Talk Page interactions and infers implicit user preferences.
Extracted habits become soft constraints prepended to skill prompts.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interaction_events (
    event_id    TEXT PRIMARY KEY,
    timestamp   REAL NOT NULL,
    event_type  TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    response_id TEXT,
    event_detail TEXT NOT NULL DEFAULT '{}',
    skill_used  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ie_session  ON interaction_events(session_id);
CREATE INDEX IF NOT EXISTS idx_ie_type     ON interaction_events(event_type);
CREATE INDEX IF NOT EXISTS idx_ie_ts       ON interaction_events(timestamp);

CREATE TABLE IF NOT EXISTS inferred_habits (
    habit_id          TEXT PRIMARY KEY,
    habit_description TEXT NOT NULL,
    evidence_count    INTEGER NOT NULL DEFAULT 0,
    confidence        REAL NOT NULL DEFAULT 0.0,
    skill_affinity    TEXT NOT NULL DEFAULT 'all',
    soft_constraint   TEXT NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        REAL NOT NULL,
    user_confirmed    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ih_active   ON inferred_habits(active);
CREATE INDEX IF NOT EXISTS idx_ih_affinity ON inferred_habits(skill_affinity);
"""

# Minimum events before a habit is inferred
_MIN_EVIDENCE = 5

# Event types
EVENT_ACCEPTED = "RESPONSE_ACCEPTED"
EVENT_MODIFIED = "RESPONSE_MODIFIED"
EVENT_REJECTED = "RESPONSE_REJECTED"
EVENT_FOLLOWUP = "FOLLOWUP_QUESTION"
EVENT_CORRECTION = "EXPLICIT_CORRECTION"

# Rejection signal phrases
_REJECTION_PHRASES = frozenset(["no,", "wrong", "redo", "try again", "that's wrong", "not right", "incorrect"])


@dataclass
class InferredHabit:
    habit_id: str
    habit_description: str
    evidence_count: int
    confidence: float
    skill_affinity: str
    soft_constraint: str
    active: bool
    created_at: float
    user_confirmed: Optional[bool]  # None=unreviewed, True=confirmed, False=rejected


class HabitExtractor:
    """
    Passive behavioral observer for the Talk Page interaction stream.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        if db_path is None:
            from ..platform import get_data_dir
            db_path = get_data_dir() / "cato.db"
        self._db_path = db_path
        self._conn = self._open_db()

    def _open_db(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    # ------------------------------------------------------------------
    # Event logging
    # ------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        session_id: str,
        response_id: Optional[str] = None,
        detail: dict | None = None,
        skill_used: str = "",
    ) -> str:
        """Log an interaction event. Returns event_id."""
        event_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO interaction_events
               (event_id, timestamp, event_type, session_id, response_id, event_detail, skill_used)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, time.time(), event_type, session_id, response_id,
             json.dumps(detail or {}), skill_used),
        )
        self._conn.commit()
        return event_id

    def classify_user_message(self, message: str) -> str:
        """Classify a user message as an event type."""
        msg_lower = message.lower().strip()
        if any(phrase in msg_lower for phrase in _REJECTION_PHRASES):
            return EVENT_REJECTED
        return EVENT_ACCEPTED

    # ------------------------------------------------------------------
    # Pattern extraction
    # ------------------------------------------------------------------

    def extract_patterns(self, window_days: int = 30) -> list[InferredHabit]:
        """
        Run pattern extraction over rolling window.
        Returns newly inferred habits (not yet stored).
        """
        since = time.time() - window_days * 86400
        habits: list[InferredHabit] = []

        # Pattern 1: Rejection rate by skill — high rejection = agent blind spot
        rows = self._conn.execute(
            """SELECT skill_used,
                      COUNT(*) as total,
                      SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END) as rejections
               FROM interaction_events
               WHERE timestamp > ? AND skill_used != ''
               GROUP BY skill_used
               HAVING total >= ?""",
            (EVENT_REJECTED, since, _MIN_EVIDENCE),
        ).fetchall()

        for row in rows:
            skill = row["skill_used"]
            rejection_rate = row["rejections"] / row["total"]
            if rejection_rate > 0.6:
                habits.append(InferredHabit(
                    habit_id=str(uuid.uuid4()),
                    habit_description=(
                        f"User frequently rejects {skill} responses ({rejection_rate:.0%} rejection rate)"
                    ),
                    evidence_count=row["total"],
                    confidence=min(0.95, rejection_rate),
                    skill_affinity=skill,
                    soft_constraint=(
                        f"Review {skill} responses carefully before responding; user has high standards here."
                    ),
                    active=True,
                    created_at=time.time(),
                    user_confirmed=None,
                ))

        # Pattern 2: Security follow-ups on file write / API calls
        security_followups = self._conn.execute(
            """SELECT COUNT(*) as n FROM interaction_events
               WHERE timestamp > ?
                 AND event_type = ?
                 AND json_extract(event_detail, '$.followup_type') = 'security'""",
            (since, EVENT_FOLLOWUP),
        ).fetchone()["n"]

        total_write_api = self._conn.execute(
            """SELECT COUNT(*) as n FROM interaction_events
               WHERE timestamp > ?
                 AND skill_used IN ('write_file', 'edit_file', 'api_payment', 'api.call')""",
            (since,),
        ).fetchone()["n"]

        if total_write_api >= _MIN_EVIDENCE and security_followups / max(total_write_api, 1) > 0.6:
            habits.append(InferredHabit(
                habit_id=str(uuid.uuid4()),
                habit_description="User frequently asks security follow-up questions after file/API operations",
                evidence_count=security_followups,
                confidence=0.85,
                skill_affinity="all",
                soft_constraint="Include a brief security note when writing files or making API calls.",
                active=True,
                created_at=time.time(),
                user_confirmed=None,
            ))

        return habits

    def save_habit(self, habit: InferredHabit) -> None:
        """Persist an inferred habit."""
        self._conn.execute(
            """INSERT OR REPLACE INTO inferred_habits
               (habit_id, habit_description, evidence_count, confidence,
                skill_affinity, soft_constraint, active, created_at, user_confirmed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (habit.habit_id, habit.habit_description, habit.evidence_count,
             habit.confidence, habit.skill_affinity, habit.soft_constraint,
             1 if habit.active else 0, habit.created_at,
             None if habit.user_confirmed is None else (1 if habit.user_confirmed else 0)),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Habit management
    # ------------------------------------------------------------------

    def list_habits(self, active_only: bool = True) -> list[InferredHabit]:
        query = "SELECT * FROM inferred_habits"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY confidence DESC"
        rows = self._conn.execute(query).fetchall()
        return [self._row_to_habit(r) for r in rows]

    def get_habits_for_skill(self, skill_name: str) -> list[InferredHabit]:
        rows = self._conn.execute(
            "SELECT * FROM inferred_habits WHERE active = 1"
            " AND (skill_affinity = ? OR skill_affinity = 'all') ORDER BY confidence DESC",
            (skill_name,),
        ).fetchall()
        return [self._row_to_habit(r) for r in rows]

    def confirm_habit(self, habit_id: str, confirmed: bool) -> bool:
        cur = self._conn.execute(
            "UPDATE inferred_habits SET user_confirmed = ? WHERE habit_id = ?",
            (1 if confirmed else 0, habit_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_habit(self, habit_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM inferred_habits WHERE habit_id = ?", (habit_id,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    def clear_unconfirmed(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM inferred_habits WHERE user_confirmed IS NULL"
        )
        self._conn.commit()
        return cur.rowcount

    def get_soft_constraints(self, skill_name: str) -> list[str]:
        """Return soft_constraint strings for injection into skill prompts."""
        habits = self.get_habits_for_skill(skill_name)
        return [h.soft_constraint for h in habits]

    def _row_to_habit(self, row: sqlite3.Row) -> InferredHabit:
        d = dict(row)
        confirmed = d.get("user_confirmed")
        if confirmed is None:
            d["user_confirmed"] = None
        else:
            d["user_confirmed"] = bool(confirmed)
        d["active"] = bool(d["active"])
        return InferredHabit(**d)

    def close(self) -> None:
        self._conn.close()
