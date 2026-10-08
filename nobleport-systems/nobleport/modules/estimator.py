"""Module 3 — Estimator / BidHunter Pro (21–30)."""

from ..core.connection import Connection
from ..core.module import Module
from ..core.readiness import Readiness


def build() -> Module:
    m = Module(
        name="estimator",
        description="Estimator / BidHunter Pro — cost templates, MA HIC proposals",
    )

    edges = [
        (21, "estimator",     "cost_templates", "Roofing/additions/decks/baths $/sqft", Readiness.LIVE, "generic"),
        (22, "estimator",     "proposal_hic",   "MA HIC itemized proposals + schedule", Readiness.LIVE, "send_proposal"),
        (23, "estimator",     "rates",          "Labor rates, 15–25% markups, coastal", Readiness.LIVE, "generic"),
        (24, "structural",    "estimator",      "Structural takeoffs → quantity check", Readiness.PILOT, "generic"),
        (25, "building_code", "estimator",      "Code compliance flags → estimate",     Readiness.PILOT, "generic"),
        (26, "estimator",     "esign",          "Change-order → eSign workflow",        Readiness.PILOT, "send_proposal"),
        (27, "estimator",     "bid_match",      "SAM.gov/NAICS/bonding match",          Readiness.ARCHITECTURE, "generic"),
        (28, "companycam",    "estimator",      "CompanyCam photos → scope refinement", Readiness.PILOT, "generic"),
        (29, "estimator",     "stephanie.ai",   "Estimate data → RAG/NotebookLM",       Readiness.PILOT, "generic"),
        (30, "estimator",     "avatar",         "Voice avatar → proposal summary",      Readiness.SIMULATION, "generic"),
    ]
    for cid, src, tgt, label, ready, action in edges:
        m.add(Connection(
            id=cid, source=src, target=tgt, label=label,
            readiness=ready, authority_action=action,
        ))
    return m
