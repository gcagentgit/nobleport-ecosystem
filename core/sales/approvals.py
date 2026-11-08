"""Approval queue — the human gate, as a queue.

Gated actions are PARKED here with the gateway approval level they need.
A human approves with a signature (the same token the governance gate
expects on an ``McpEnvelope``) or rejects with a reason. Nothing executes
from this queue without a signature.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .models import ActionStatus, ActionType, SalesAction, utcnow


class ApprovalError(RuntimeError):
    pass


class ApprovalQueue:
    def __init__(self):
        self._actions: dict[str, SalesAction] = {}

    def park(self, action: SalesAction) -> SalesAction:
        action.status = ActionStatus.PARKED
        self._actions[action.action_id] = action
        return action

    def get(self, action_id: str) -> SalesAction:
        try:
            return self._actions[action_id]
        except KeyError:
            raise ApprovalError(f"unknown action {action_id}") from None

    def pending(self, *, rep_id: Optional[str] = None,
                action_type: Optional[ActionType] = None) -> list[SalesAction]:
        out = [a for a in self._actions.values() if a.status is ActionStatus.PARKED]
        if rep_id:
            out = [a for a in out if a.rep_id == rep_id]
        if action_type:
            out = [a for a in out if a.action_type is action_type]
        return sorted(out, key=lambda a: (a.created_at, a.action_id))

    def approve(self, action_id: str, *, signature: str, approver: str) -> SalesAction:
        if not signature or not signature.strip():
            raise ApprovalError("a human signature is required to approve")
        action = self.get(action_id)
        if action.status is not ActionStatus.PARKED:
            raise ApprovalError(f"action {action_id} is {action.status.value}, not parked")
        action.human_signature = signature
        action.approved_by = approver
        action.status = ActionStatus.APPROVED
        return action

    def reject(self, action_id: str, *, reason: str, by: str) -> SalesAction:
        action = self.get(action_id)
        if action.status is not ActionStatus.PARKED:
            raise ApprovalError(f"action {action_id} is {action.status.value}, not parked")
        action.status = ActionStatus.REJECTED
        action.rejected_reason = f"{by}: {reason}"
        return action

    def mark_executed(self, action_id: str, at: Optional[datetime] = None) -> SalesAction:
        action = self.get(action_id)
        if action.status is not ActionStatus.APPROVED:
            raise ApprovalError(f"action {action_id} must be approved before execution")
        action.status = ActionStatus.EXECUTED
        action.payload["executed_at"] = (at or utcnow()).isoformat()
        return action

    def all(self) -> list[SalesAction]:
        return list(self._actions.values())
