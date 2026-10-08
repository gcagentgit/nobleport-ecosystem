"""Module 8 — eSign / Document Workflow (71–80)."""

from ..core.connection import Connection
from ..core.module import Module
from ..core.readiness import Readiness


def build() -> Module:
    m = Module(
        name="esign",
        description="FastAPI + PostgreSQL eSign — ESIGN/UETA, hash-chained",
    )

    edges = [
        (71, "esign",        "envelope_ids",    "Deterministic IDs + hash-chained log", Readiness.LIVE, "generic"),
        (72, "esign",        "signing_order",   "Ordered signing → ESIGN/UETA consent", Readiness.LIVE, "generic"),
        (73, "esign",        "pdf",             "ReportLab/pypdf + pyHanko sealing",    Readiness.PILOT, "generic"),
        (74, "esign",        "webhooks",        "Webhook delivery → signed status",     Readiness.PILOT, "generic"),
        (75, "esign",        "envelopes",       "Proposal/contract/AWO/CO envelopes",   Readiness.PILOT, "send_proposal"),
        (76, "esign",        "verification",    "Verification endpoints",               Readiness.PILOT, "generic"),
        (77, "estimator",    "esign",           "Estimator proposals → eSign",          Readiness.PILOT, "send_proposal"),
        (78, "esign",        "revenue_closure", "Post-signature → Revenue Closure",     Readiness.PILOT, "generic"),
        (79, "stephanie.ai", "esign",           "Voice-initiated envelope creation",    Readiness.SIMULATION, "send_proposal"),
        (80, "esign",        "project_manager", "Closed-loop document memory",          Readiness.PILOT, "generic"),
    ]
    for cid, src, tgt, label, ready, action in edges:
        m.add(Connection(
            id=cid, source=src, target=tgt, label=label,
            readiness=ready, authority_action=action,
        ))
    return m
