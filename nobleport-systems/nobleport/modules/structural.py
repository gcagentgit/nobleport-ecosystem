"""Module 5 — Structural Review (41–50)."""

from ..core.connection import Connection
from ..core.module import Module
from ..core.readiness import Readiness


def build() -> Module:
    m = Module(
        name="structural",
        description="Structural Review — LVL/I-Joist/headers/footings, coastal loads",
    )

    edges = [
        (41, "structural", "sizing",          "LVL/I-Joist/header/footing sizing",   Readiness.PILOT, "generic"),
        (42, "structural", "takeoffs",        "Framing schedules + takeoff lists",   Readiness.PILOT, "generic"),
        (43, "structural", "coastal_loads",   "Coastal wind/flood resilience",       Readiness.PILOT, "generic"),
        (44, "structural", "building_code",   "Cross-validation ↔ Building Code",    Readiness.PILOT, "generic"),
        (45, "structural", "estimator",       "Quantity feed → Estimator",           Readiness.PILOT, "generic"),
        (46, "structural", "project_manager", "Engineering summaries → PM schedule", Readiness.PILOT, "generic"),
        (47, "structural", "stephanie.ai",    "Pattern recognition → RAG",           Readiness.SIMULATION, "generic"),
        (48, "structural", "avatar",          "Visual diagrams → avatar briefings",  Readiness.SIMULATION, "generic"),
        (49, "structural", "permitstream.ai", "Takeoffs → permit package checks",    Readiness.PILOT, "generic"),
        (50, "structural", "outcomes",        "Outcome nodes → predictive intel",    Readiness.SIMULATION, "generic"),
    ]
    for cid, src, tgt, label, ready, action in edges:
        m.add(Connection(
            id=cid, source=src, target=tgt, label=label,
            readiness=ready, authority_action=action,
        ))
    return m
