"""Module 2 — PermitStream.ai: Municipal Permit Intelligence (11–20)."""

from ..core.connection import Connection
from ..core.module import Module
from ..core.readiness import Readiness


def build() -> Module:
    m = Module(
        name="permitstream.ai",
        description="Municipal permit intelligence — 34 Essex County municipalities",
    )

    edges = [
        (11, "monitoring", "municipalities",    "Monitoring feeds → 34 Essex Co.",    Readiness.LIVE, "generic"),
        (12, "andover",    "snapshot_registry", "Andover canary → SnapshotRegistry",  Readiness.PILOT, "anchor_permit"),
        (13, "permit",     "onchain",           "Permit status → keccak256 anchor",   Readiness.PILOT, "anchor_permit"),
        (14, "permit",     "stephanie.ai",      "Permit hierarchy → orchestration",   Readiness.LIVE, "generic"),
        (15, "permit",     "building_code",     "Building-code depth cross-check",    Readiness.LIVE, "generic"),
        (16, "permit",     "project_manager",   "Permit timeline → scheduling",       Readiness.LIVE, "generic"),
        (17, "permit",     "stephanie.ai",      "Risk signal injection",              Readiness.PILOT, "generic"),
        (18, "permit",     "dashboard",         "Public dashboard → React/Bloomberg", Readiness.PILOT, "generic"),
        (19, "permit",     "truth_standard",    "Source-evidence gating + CTS",       Readiness.LIVE, "generic"),
        (20, "permit",     "stephanie.ai",      "Closed-loop feed → RAG patterns",    Readiness.PILOT, "generic"),
    ]
    for cid, src, tgt, label, ready, action in edges:
        m.add(Connection(
            id=cid, source=src, target=tgt, label=label,
            readiness=ready, authority_action=action,
            metadata={"requires_evidence": True},
        ))
    return m
