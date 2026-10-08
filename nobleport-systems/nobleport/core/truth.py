"""Canonical Truth Standard — every connection must pass before execution."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TruthVerdict:
    passed: bool
    reasons: List[str] = field(default_factory=list)
    evidence_refs: List[str] = field(default_factory=list)


class ClaimRegister:
    """Register of rejected / fabricated claims (Cyborg.ai #68)."""

    def __init__(self):
        self._rejected: Dict[str, str] = {}

    def reject(self, claim_id: str, reason: str) -> None:
        self._rejected[claim_id] = reason

    def is_rejected(self, claim_id: str) -> bool:
        return claim_id in self._rejected

    def reason(self, claim_id: str) -> str:
        return self._rejected.get(claim_id, "")


class CanonicalTruthStandard:
    """Gates a connection payload against the NoblePort truth rules.

    Rules:
      1. Source evidence must be present (URL, hash, or canonical ref).
      2. No fabricated claims — claim register check.
      3. Human review required for L3/L4 authority actions.
    """

    def __init__(self, claim_register: Optional[ClaimRegister] = None):
        self.claim_register = claim_register

    def evaluate(
        self,
        payload: Dict[str, Any],
        required_evidence: bool = True,
        human_reviewed: bool = False,
        authority_level: int = 1,
    ) -> TruthVerdict:
        reasons: List[str] = []
        evidence: List[str] = []

        if required_evidence:
            ref = payload.get("evidence_ref")
            if not ref:
                reasons.append("Missing source evidence reference")
            else:
                evidence.append(ref)

        # Fabricated claim check
        if self.claim_register is not None:
            claim_id = payload.get("claim_id")
            if claim_id and self.claim_register.is_rejected(claim_id):
                reasons.append(
                    f"Claim {claim_id} is in rejected register: "
                    f"{self.claim_register.reason(claim_id)}"
                )

        # HITL gate
        if authority_level >= 3 and not human_reviewed:
            reasons.append(f"L{authority_level} action requires human review")

        return TruthVerdict(
            passed=len(reasons) == 0,
            reasons=reasons,
            evidence_refs=evidence,
        )
