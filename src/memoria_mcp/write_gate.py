"""WriteGate — quality control gate for memory writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class WriteCandidate:
    content: str
    candidate_type: str = "stable_update"  # stable_update / approved_event / candidate_signal
    confidence: float = 0.5
    importance: float = 0.5
    source: str = "user"  # user / agent / auto
    tags: List[str] = field(default_factory=list)


@dataclass
class WriteDecision:
    decision_type: str  # stable_update / approved_event / candidate_only / reject_learning
    approved: bool
    reasons: List[str] = field(default_factory=list)
    candidate: Optional[WriteCandidate] = None


class WriteGate:
    """Quality gate controlling what enters trunk memory.

    Adapted thresholds for programming agent context:
    - Lower confidence/evidence thresholds (0.6 vs 0.8) since programming patterns
      often have moderate confidence but high practical value.
    - No budget_gate or risk_gate dependencies.
    """

    def __init__(
        self,
        min_event_confidence: float = 0.4,
        min_stable_confidence: float = 0.6,
        min_stable_evidence: float = 0.6,
    ):
        self.min_event_confidence = min_event_confidence
        self.min_stable_confidence = min_stable_confidence
        self.min_stable_evidence = min_stable_evidence

    def review(
        self,
        candidate: WriteCandidate,
        *,
        evidence_quality: float = 0.0,
        user_feedback: Optional[str] = None,
    ) -> WriteDecision:
        reasons: List[str] = []

        # User rejection
        feedback = (user_feedback or "").strip().lower()
        if feedback in {"deny", "denied", "reject", "rejected", "delete", "wrong", "false"}:
            return WriteDecision(
                decision_type="reject_learning",
                approved=False,
                reasons=["user_rejected"],
                candidate=candidate,
            )

        candidate_type = candidate.candidate_type

        if candidate_type == "stable_update":
            if float(candidate.confidence) < self.min_stable_confidence:
                reasons.append("stable_update_confidence_too_low")
            if float(evidence_quality) < self.min_stable_evidence:
                reasons.append("stable_update_evidence_too_low")
            if reasons:
                return WriteDecision(
                    decision_type="candidate_only",
                    approved=False,
                    reasons=reasons,
                    candidate=candidate,
                )
            return WriteDecision(
                decision_type="stable_update",
                approved=True,
                reasons=["stable_update_approved"],
                candidate=candidate,
            )

        if candidate_type == "approved_event":
            if float(candidate.confidence) < self.min_event_confidence:
                reasons.append("approved_event_confidence_too_low")
            if reasons:
                return WriteDecision(
                    decision_type="candidate_only",
                    approved=False,
                    reasons=reasons,
                    candidate=candidate,
                )
            return WriteDecision(
                decision_type="approved_event",
                approved=True,
                reasons=["approved_event_approved"],
                candidate=candidate,
            )

        if candidate_type == "candidate_signal":
            return WriteDecision(
                decision_type="candidate_only",
                approved=False,
                reasons=["candidate_signal_not_stable"],
                candidate=candidate,
            )

        return WriteDecision(
            decision_type="candidate_only",
            approved=False,
            reasons=["unknown_candidate_type"],
            candidate=candidate,
        )
