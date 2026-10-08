"""Authority Matrix + HITL Approval Console (connections #4, #65, #69)."""

from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, Optional


class AuthorityLevel(IntEnum):
    L1_AUTO = 1          # Fully automated
    L2_NOTIFY = 2        # Auto with notification
    L3_APPROVE = 3       # Human approval required
    L4_HUMAN_ONLY = 4    # Human must execute


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    requires_human: bool


class AuthorityMatrix:
    """Policy-as-code authority matrix (Cyborg.ai #65)."""

    def __init__(self):
        # action → required level
        self._policy: Dict[str, AuthorityLevel] = {
            "issue_credential": AuthorityLevel.L3_APPROVE,
            "revoke_credential": AuthorityLevel.L3_APPROVE,
            "anchor_permit": AuthorityLevel.L2_NOTIFY,
            "send_proposal": AuthorityLevel.L3_APPROVE,
            "execute_payment": AuthorityLevel.L4_HUMAN_ONLY,
            "ledger_write": AuthorityLevel.L3_APPROVE,
            "treasury_transfer": AuthorityLevel.L4_HUMAN_ONLY,
        }

    def level_for(self, action: str) -> AuthorityLevel:
        return self._policy.get(action, AuthorityLevel.L2_NOTIFY)

    def set_policy(self, action: str, level: AuthorityLevel) -> None:
        self._policy[action] = level


class HITLApprovalConsole:
    """Fail-closed human-in-the-loop approval queue."""

    def __init__(self, authority_matrix: AuthorityMatrix):
        self.matrix = authority_matrix
        self._pending: Dict[str, dict] = {}
        self._approved: Dict[str, str] = {}  # request_id → approver

    def requires_approval(self, action: str, requested_level: AuthorityLevel) -> GateDecision:
        required = self.matrix.level_for(action)
        if required <= AuthorityLevel.L2_NOTIFY:
            return GateDecision(True, "Auto-approved", False)
        if requested_level >= required:
            return GateDecision(True, "Authority satisfied", False)
        return GateDecision(False, f"Needs L{int(required)}+ human approval", True)

    def submit(self, request_id: str, action: str, payload: dict) -> GateDecision:
        decision = self.requires_approval(action, AuthorityLevel.L1_AUTO)
        if decision.allowed:
            return decision
        self._pending[request_id] = {"action": action, "payload": payload}
        return decision

    def pending(self) -> Dict[str, dict]:
        return dict(self._pending)

    def approve(self, request_id: str, approver: str) -> bool:
        if request_id in self._pending:
            self._approved[request_id] = approver
            del self._pending[request_id]
            return True
        return False

    def reject(self, request_id: str) -> bool:
        return self._pending.pop(request_id, None) is not None

    def is_approved(self, request_id: str) -> bool:
        return request_id in self._approved

    def approver(self, request_id: str) -> Optional[str]:
        return self._approved.get(request_id)
