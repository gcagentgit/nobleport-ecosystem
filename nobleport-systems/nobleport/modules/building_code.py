"""Module 6 — Building Code Module (51–60)."""

from ..core.connection import Connection
from ..core.module import Module
from ..core.readiness import Readiness


def build() -> Module:
    m = Module(
        name="building_code",
        description="Building Code — IRC/IBC + MA amendments, coastal Chapter 91",
    )

    edges = [
        (51, "code", "irc_ibc",          "IRC/IBC + MA amendments",             Readiness.LIVE, "generic"),
        (52, "code", "energy_adu_flood", "Energy code + ADU + Ch.91 flood",     Readiness.PILOT, "generic"),
        (53, "code", "residential",      "Deck/stair/egress compliance",        Readiness.LIVE, "generic"),
        (54, "code", "permit_summary",   "Permit summaries → section refs",     Readiness.PILOT, "generic"),
        (55, "code", "estimator",        "Cost impact → Estimator",             Readiness.PILOT, "generic"),
        (56, "code", "structural",       "Validation loop ↔ Structural Review", Readiness.PILOT, "generic"),
        (57, "code", "stephanie.ai",     "Risk flags → Stephanie alerts",       Readiness.PILOT, "generic"),
        (58, "code", "rag",              "KB updates via RAG",                  Readiness.PILOT, "generic"),
        (59, "code", "avatar",           "Voice explanations + visual aids",    Readiness.SIMULATION, "generic"),
        (60, "code", "outcomes",         "Coastal MA case-by-case learning",    Readiness.SIMULATION, "generic"),
    ]
    for cid, src, tgt, label, ready, action in edges:
        m.add(Connection(
            id=cid, source=src, target=tgt, label=label,
            readiness=ready, authority_action=action,
        ))
    return m
