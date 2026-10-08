"""Orchestration graph — Stephanie.ai is the single authority."""

from typing import Dict, Iterable, List, Optional

import networkx as nx

from .audit import AuditBeacon
from .connection import ORCHESTRATOR, Connection
from .gate import AuthorityMatrix, HITLApprovalConsole
from .module import Module
from .readiness import Readiness
from .truth import CanonicalTruthStandard

# Connection #1 (LangGraph orchestration) targets every module.
FAN_OUT_TARGET = "all_modules"


class NoblePortGraph:
    """Directed graph of all 100 connections.

    Stephanie.ai (module 1) is the orchestrator — every other module
    must be reachable from it, and every state-changing edge funnels
    through the same gate stack.

    Node kinds:
      - module   — one of the 10 subsystems
      - endpoint — a source/target named on a connection (e.g. "permit")
    Edge kinds:
      - connection — one of the 100 numbered connections
      - owns       — module → the endpoints its connections originate from
    """

    ORCHESTRATOR = ORCHESTRATOR

    def __init__(
        self,
        audit: Optional[AuditBeacon] = None,
        truth: Optional[CanonicalTruthStandard] = None,
        matrix: Optional[AuthorityMatrix] = None,
        hitl: Optional[HITLApprovalConsole] = None,
    ):
        self.audit = audit or AuditBeacon()
        self.truth = truth or CanonicalTruthStandard()
        self.matrix = matrix or AuthorityMatrix()
        self.hitl = hitl or HITLApprovalConsole(self.matrix)

        self.modules: Dict[str, Module] = {}
        self.graph = nx.MultiDiGraph()
        self._connections: Dict[int, Connection] = {}
        self._fan_out: List[Connection] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_module(self, module: Module) -> None:
        self.modules[module.name] = module
        self.graph.add_node(module.name, kind="module")
        for conn in self._fan_out:
            self._add_fan_out_edge(conn, module.name)

    def register_connection(self, conn: Connection, owner: Optional[str] = None) -> None:
        if conn.id in self._connections:
            raise ValueError(f"Duplicate connection id {conn.id}")
        conn.wire(self.audit, self.truth, self.matrix, self.hitl)
        self._connections[conn.id] = conn

        for node in (conn.source, conn.target):
            if node not in self.graph:
                self.graph.add_node(node, kind="endpoint")

        if owner is not None and owner != conn.source:
            self.graph.add_edge(owner, conn.source, kind="owns")

        if conn.target == FAN_OUT_TARGET:
            self._fan_out.append(conn)
            for name in self.modules:
                self._add_fan_out_edge(conn, name)
        else:
            self._add_connection_edge(conn, conn.source, conn.target)

    def _add_connection_edge(self, conn: Connection, src: str, tgt: str) -> None:
        self.graph.add_edge(
            src,
            tgt,
            key=conn.id,
            kind="connection",
            id=conn.id,
            label=conn.label,
            readiness=conn.readiness.value,
        )

    def _add_fan_out_edge(self, conn: Connection, module_name: str) -> None:
        if module_name != self.ORCHESTRATOR:
            self._add_connection_edge(conn, conn.source, module_name)

    def register_all(self, modules: Iterable[Module]) -> None:
        for m in modules:
            self.register_module(m)
            for c in m.connections:
                self.register_connection(c, owner=m.name)

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    def connection(self, conn_id: int) -> Connection:
        return self._connections[conn_id]

    def all_connections(self) -> List[Connection]:
        return [self._connections[i] for i in sorted(self._connections)]

    def by_readiness(self, readiness: Readiness) -> List[Connection]:
        return [c for c in self.all_connections() if c.readiness == readiness]

    # ------------------------------------------------------------------
    # Orchestration invariants
    # ------------------------------------------------------------------

    def verify_orchestration(self) -> Dict[str, bool]:
        """Stephanie.ai must reach every module."""
        results: Dict[str, bool] = {}
        for name in self.modules:
            if name == self.ORCHESTRATOR:
                continue
            results[name] = (
                self.ORCHESTRATOR in self.graph
                and nx.has_path(self.graph, self.ORCHESTRATOR, name)
            )
        return results

    def verify_connectivity_count(self, expected: int = 100) -> bool:
        return len(self._connections) == expected
