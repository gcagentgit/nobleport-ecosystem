"""Module 9 — Revenue Closure Engine (81–90)."""

from ..core.connection import Connection
from ..core.module import Module
from ..core.readiness import Readiness


def build() -> Module:
    m = Module(
        name="revenue_closure",
        description="Revenue Closure — AWO→invoice→Stripe→ledger on Cloudflare",
    )

    edges = [
        (81, "awo",             "invoice_stripe_ledger", "AWO → invoice → Stripe → ledger", Readiness.PILOT, "ledger_write"),
        (82, "revenue_closure", "cloudflare",            "Cloudflare Workers / Hono / D1",  Readiness.PILOT, "generic"),
        (83, "proposal_hic",    "schedule",              "Payment schedule tracking",       Readiness.LIVE, "generic"),
        (84, "change_order",    "impact",                "Change-order financial impact",   Readiness.PILOT, "generic"),
        (85, "revenue_closure", "stephanie.ai",          "Job-health revenue forecast",     Readiness.PILOT, "generic"),
        (86, "revenue_closure", "web3",                  "Treasury coordination ↔ Web3",    Readiness.PILOT, "treasury_transfer"),
        (87, "revenue_closure", "webhooks",              "Stripe/PayPal webhook ingestion", Readiness.PILOT, "ledger_write"),
        (88, "revenue_closure", "outcomes",              "Closed revenue → Outcome nodes",  Readiness.PILOT, "generic"),
        (89, "revenue_closure", "kpi",                   "KPI: cash velocity/aging/margin", Readiness.PILOT, "generic"),
        (90, "revenue_closure", "ledger",                "Fail-closed before ledger write", Readiness.LIVE, "ledger_write"),
    ]
    for cid, src, tgt, label, ready, action in edges:
        m.add(Connection(
            id=cid, source=src, target=tgt, label=label,
            readiness=ready, authority_action=action,
        ))
    return m
