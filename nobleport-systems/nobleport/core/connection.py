"""Connection edge primitive — one of the 100 NoblePort connections."""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .audit import AuditBeacon
from .gate import AuthorityLevel, AuthorityMatrix, HITLApprovalConsole
from .readiness import Readiness
from .truth import CanonicalTruthStandard, TruthVerdict

ORCHESTRATOR = "stephanie.ai"


@dataclass
class ConnectionResult:
    connection_id: int
    success: bool
    output: Any = None
    truth: Optional[TruthVerdict] = None
    audit_seq: Optional[int] = None
    reason: str = ""


@dataclass
class Connection:
    """A single gated edge between two NoblePort modules.

    Matches the numbered connection map exactly.
    """
    id: int
    source: str
    target: str
    label: str
    readiness: Readiness = Readiness.ARCHITECTURE
    authority_action: str = "generic"
    handler: Optional[Callable[[Dict[str, Any]], Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Runtime wiring (set by NoblePortGraph.register_connection)
    _audit: Optional[AuditBeacon] = field(default=None, repr=False, compare=False)
    _truth: Optional[CanonicalTruthStandard] = field(default=None, repr=False, compare=False)
    _matrix: Optional[AuthorityMatrix] = field(default=None, repr=False, compare=False)
    _hitl: Optional[HITLApprovalConsole] = field(default=None, repr=False, compare=False)

    def wire(
        self,
        audit: AuditBeacon,
        truth: CanonicalTruthStandard,
        matrix: AuthorityMatrix,
        hitl: HITLApprovalConsole,
    ) -> "Connection":
        self._audit = audit
        self._truth = truth
        self._matrix = matrix
        self._hitl = hitl
        return self

    @property
    def authority_level(self) -> AuthorityLevel:
        if self._matrix is None:
            return AuthorityLevel.L1_AUTO
        return self._matrix.level_for(self.authority_action)

    def _fail(self, reason: str, verdict=None, audit_seq=None) -> ConnectionResult:
        return ConnectionResult(
            connection_id=self.id,
            success=False,
            truth=verdict,
            audit_seq=audit_seq,
            reason=reason,
        )

    def fire(
        self,
        payload: Dict[str, Any],
        actor: str = ORCHESTRATOR,
        human_reviewed: bool = False,
        approval_id: Optional[str] = None,
    ) -> ConnectionResult:
        """Execute the connection with full gating.

        Order of enforcement (fail-closed):
          1. Readiness gate — ARCHITECTURE / SIMULATION cannot mutate state.
          2. AuditBeacon — record intent BEFORE execution.
          3. Canonical Truth Standard — verify evidence + claims.
          4. Authority Matrix + HITL — human gate for L3, human actor for L4.
          5. Handler execution.

        Human review is satisfied either by an `approval_id` approved in the
        HITL console (preferred — records the named approver) or by an
        explicit `human_reviewed=True` attestation from the caller.
        """
        # 1. Readiness gate
        if not self.readiness.permits_state_change():
            return self._fail(f"Readiness={self.readiness.value} blocks state change")

        level = self.authority_level

        approved_by: Optional[str] = None
        if approval_id is not None and self._hitl is not None:
            if self._hitl.is_approved(approval_id):
                human_reviewed = True
                approved_by = self._hitl.approver(approval_id)
        if human_reviewed and approved_by is None:
            approved_by = f"attested-by:{actor}"

        # 2. AuditBeacon
        audit_seq = None
        if self._audit is not None:
            rec = self._audit.emit(
                module=self.source,
                action=f"#{self.id} {self.label}",
                payload=payload,
                actor=actor,
                authority_level=int(level),
                approved_by=approved_by,
            )
            audit_seq = rec.seq

        # 3. Truth standard
        verdict = None
        if self._truth is not None:
            verdict = self._truth.evaluate(
                payload,
                required_evidence=self.metadata.get("requires_evidence", True),
                human_reviewed=human_reviewed,
                authority_level=int(level),
            )
            if not verdict.passed:
                return self._fail("; ".join(verdict.reasons), verdict, audit_seq)

        # 4. Authority / HITL
        if level >= AuthorityLevel.L3_APPROVE and not human_reviewed:
            return self._fail(
                f"L{int(level)} action requires human review", verdict, audit_seq
            )
        if level >= AuthorityLevel.L4_HUMAN_ONLY and actor == ORCHESTRATOR:
            return self._fail(
                f"L{int(level)} action must be executed by a human, not {ORCHESTRATOR}",
                verdict,
                audit_seq,
            )

        # 5. Handler
        if self.handler is None:
            return ConnectionResult(
                connection_id=self.id,
                success=True,
                output={"noop": True, "label": self.label},
                truth=verdict,
                audit_seq=audit_seq,
            )

        try:
            output = self.handler(payload)
        except Exception as e:  # noqa: BLE001
            return self._fail(f"Handler error: {e}", verdict, audit_seq)
        return ConnectionResult(
            connection_id=self.id,
            success=True,
            output=output,
            truth=verdict,
            audit_seq=audit_seq,
        )
