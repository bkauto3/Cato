"""
EpistemicMonitor — Skill 3 (Epistemic Layer)

Tracks factual premises extracted from model outputs, monitors confidence
gaps, and manages interrupt budgets for clarification sub-queries.
"""

import re
import time


class EpistemicMonitor:
    """Monitor epistemic state across a multi-model reasoning session."""

    PREMISE_MARKERS = [
        "because",
        "since",
        "assuming",
        "given that",
        "the fact that",
    ]

    def __init__(self, threshold: float = 0.70, max_interrupts: int = 3):
        self.threshold = threshold
        self.max_interrupts = max_interrupts
        self._premise_confidence_map: dict[str, float] = {}
        self._interrupt_count: int = 0
        self._unresolved_gaps: list[dict] = []

    # ------------------------------------------------------------------
    # Premise extraction
    # ------------------------------------------------------------------

    def extract_premises(self, text: str) -> list[str]:
        """
        Extract factual premises from *text*.

        Splits on sentence boundaries (``". "`` or newline), then keeps every
        sentence that contains at least one of the marker phrases.  Returns
        the matched sentences (stripped).
        """
        # Split on ". " or "\n" to get individual sentences
        sentences = re.split(r"\.\s+|\n", text)
        premises: list[str] = []
        for sentence in sentences:
            stripped = sentence.strip()
            if not stripped:
                continue
            lower = stripped.lower()
            for marker in self.PREMISE_MARKERS:
                if marker in lower:
                    premises.append(stripped)
                    break
        return premises

    # ------------------------------------------------------------------
    # Confidence management
    # ------------------------------------------------------------------

    def update_confidence(self, premise: str, score: float) -> None:
        """Store/update confidence for *premise* (key is lowercased + stripped)."""
        key = premise.lower().strip()
        self._premise_confidence_map[key] = score

    def get_gaps(self) -> list[str]:
        """Return premises whose stored confidence is below ``self.threshold``."""
        return [
            premise
            for premise, score in self._premise_confidence_map.items()
            if score < self.threshold
        ]

    # ------------------------------------------------------------------
    # Sub-query generation
    # ------------------------------------------------------------------

    def generate_sub_query(self, premise: str) -> str:
        """Return a verification sub-query for *premise*."""
        return f"I need to verify: {premise}"

    # ------------------------------------------------------------------
    # Unresolved gap tracking
    # ------------------------------------------------------------------

    def record_unresolved(self, premise: str, confidence: float) -> None:
        """Append *premise* and its *confidence* to the unresolved gaps list."""
        self._unresolved_gaps.append(
            {
                "premise": premise,
                "confidence": confidence,
                "timestamp": time.time(),
            }
        )

    # ------------------------------------------------------------------
    # Interrupt budget
    # ------------------------------------------------------------------

    def can_interrupt(self) -> bool:
        """Return ``True`` if the interrupt budget has not been exhausted."""
        return self._interrupt_count < self.max_interrupts

    def consume_interrupt(self) -> None:
        """Consume one unit of the interrupt budget."""
        self._interrupt_count += 1

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def reset_session(self) -> None:
        """Clear all per-session state (confidence map and interrupt count)."""
        self._premise_confidence_map.clear()
        self._interrupt_count = 0

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_unresolved_summary(self) -> dict:
        """Return a summary dict of all unresolved gaps."""
        return {
            "total": len(self._unresolved_gaps),
            "gaps": self._unresolved_gaps,
        }
