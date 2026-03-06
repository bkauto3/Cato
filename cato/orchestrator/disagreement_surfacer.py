"""
DisagreementSurfacer — Skill 9 (Epistemic Layer)

Detects and characterises disagreement across multi-model outputs using
character-level Jaccard similarity (no external dependencies).
"""

import math


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jaccard(a: str, b: str) -> float:
    """Character-level Jaccard similarity between two strings (word tokens)."""
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a and not tokens_b:
        return 1.0
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union if union > 0 else 0.0


def _stdev(values: list[float]) -> float:
    """Population standard deviation (no numpy required)."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class DisagreementSurfacer:
    """Detect, classify, and surface disagreements across model outputs."""

    THRESHOLDS: dict[str, float] = {
        "code": 0.30,
        "research": 0.40,
        "decision": 0.25,
        "default": 0.35,
    }

    # ------------------------------------------------------------------
    # Score computation
    # ------------------------------------------------------------------

    def compute_disagreement_score(
        self,
        outputs: dict[str, str],
        confidences: dict[str, float],
        task_type: str = "default",
    ) -> float:
        """
        Compute a combined disagreement score in [0.0, 1.0].

        Semantic distance: max pairwise Jaccard distance (1 - similarity).
        Confidence divergence: population stdev of confidence values.
        Combined: 0.6 * max_semantic_distance + 0.4 * confidence_stdev.
        """
        output_values = list(outputs.values())

        # Pairwise Jaccard distances
        max_distance = 0.0
        for i in range(len(output_values)):
            for j in range(i + 1, len(output_values)):
                dist = 1.0 - _jaccard(output_values[i], output_values[j])
                if dist > max_distance:
                    max_distance = dist

        conf_stdev = _stdev(list(confidences.values()))

        combined = 0.6 * max_distance + 0.4 * conf_stdev
        # Normalise to [0.0, 1.0]
        combined = max(0.0, min(1.0, combined))
        return round(combined, 4)

    # ------------------------------------------------------------------
    # Threshold check
    # ------------------------------------------------------------------

    def is_disagreement(self, score: float, task_type: str = "default") -> bool:
        """Return ``True`` if *score* exceeds the threshold for *task_type*."""
        threshold = self.THRESHOLDS.get(task_type, self.THRESHOLDS["default"])
        return score > threshold

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def classify_disagreement(self, outputs: dict[str, str]) -> str:
        """
        Classify the nature of the disagreement from the combined output text.

        Priority order: RISK_ASSESSMENT > VALUE_JUDGMENT > APPROACH > FACTUAL.
        """
        combined = " ".join(outputs.values()).lower()

        risk_keywords = ["dangerous", "safe", "risk", "unlikely", "threat"]
        value_keywords = ["more important", "prefer", "recommend", "should"]
        approach_keywords = ["instead", "alternatively", "better to", "approach"]

        for kw in risk_keywords:
            if kw in combined:
                return "RISK_ASSESSMENT"

        for kw in value_keywords:
            if kw in combined:
                return "VALUE_JUDGMENT"

        for kw in approach_keywords:
            if kw in combined:
                return "APPROACH"

        return "FACTUAL"

    # ------------------------------------------------------------------
    # Action recommendation
    # ------------------------------------------------------------------

    def recommend_action(self, disagreement_type: str) -> str:
        """Map a disagreement type to a recommended action."""
        mapping = {
            "FACTUAL": "run_additional_queries",
            "APPROACH": "proceed_with_consensus",
            "RISK_ASSESSMENT": "request_user_judgment",
            "VALUE_JUDGMENT": "request_user_judgment",
        }
        return mapping.get(disagreement_type, "run_additional_queries")

    # ------------------------------------------------------------------
    # Surface
    # ------------------------------------------------------------------

    def surface(
        self,
        outputs: dict[str, str],
        confidences: dict[str, float],
        task_type: str = "default",
    ) -> dict | None:
        """
        Return a structured disagreement report if disagreement is detected,
        otherwise return ``None``.
        """
        score = self.compute_disagreement_score(outputs, confidences, task_type)
        if not self.is_disagreement(score, task_type):
            return None

        # Consensus view: output from the model with highest confidence
        consensus_model = max(confidences, key=lambda m: confidences[m])
        minority_model = min(confidences, key=lambda m: confidences[m])

        disagreement_type = self.classify_disagreement(outputs)

        return {
            "consensus_view": outputs[consensus_model],
            "minority_view": outputs[minority_model],
            "minority_model": minority_model,
            "disagreement_type": disagreement_type,
            "disagreement_score": score,
            "recommended_action": self.recommend_action(disagreement_type),
        }
