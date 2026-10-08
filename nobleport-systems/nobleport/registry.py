"""Build the full 100-connection NoblePort registry."""

from typing import Dict, List

from .core.audit import AuditBeacon, AuditRecord
from .core.connection import Connection
from .core.gate import AuthorityMatrix, HITLApprovalConsole
from .core.graph import NoblePortGraph
from .core.readiness import Readiness
from .core.truth import CanonicalTruthStandard, ClaimRegister
from .modules import BUILDERS


class NoblePortRegistry:
    """Top-level facade — builds and wires all 100 connections."""

    def __init__(self):
        self.audit = AuditBeacon()
        self.claim_register = ClaimRegister()
        self.truth = CanonicalTruthStandard(self.claim_register)
        self.matrix = AuthorityMatrix()
        self.hitl = HITLApprovalConsole(self.matrix)

        self.graph = NoblePortGraph(
            audit=self.audit,
            truth=self.truth,
            matrix=self.matrix,
            hitl=self.hitl,
        )
        self.graph.register_all(build() for build in BUILDERS)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def connections(self) -> List[Connection]:
        return self.graph.all_connections()

    def connection(self, cid: int) -> Connection:
        return self.graph.connection(cid)

    def readiness_summary(self) -> Dict[str, int]:
        summary = {r.value: 0 for r in Readiness}
        for c in self.connections():
            summary[c.readiness.value] += 1
        return summary

    def orchestration_report(self) -> Dict[str, bool]:
        return self.graph.verify_orchestration()

    def audit_trail(self) -> List[AuditRecord]:
        return self.audit.chain()
