"""Module 4 — Project Manager / GCagent.ai (31–40)."""

from ..core.connection import Connection
from ..core.module import Module
from ..core.readiness import Readiness


def build() -> Module:
    m = Module(
        name="project_manager",
        description="Project Manager / GCagent.ai — daily logs, CPM scheduling",
    )

    edges = [
        (31, "pm", "daily_logs",      "Daily logs → photos/notes/CompanyCam",  Readiness.LIVE, "generic"),
        (32, "pm", "erpnext",         "Critical-path scheduling + ERPNext",    Readiness.PILOT, "generic"),
        (33, "pm", "materials",       "Material tracking + shortage flags",    Readiness.PILOT, "generic"),
        (34, "pm", "subcontractors",  "Sub coordination notifications",        Readiness.PILOT, "generic"),
        (35, "pm", "inspections",     "Inspection scheduling → MA timelines",  Readiness.PILOT, "generic"),
        (36, "pm", "stephanie.ai",    "Job-health KPIs → daily briefing",      Readiness.PILOT, "generic"),
        (37, "pm", "revenue_closure", "AWO / change-order → Revenue Closure",  Readiness.PILOT, "generic"),
        (38, "pm", "stephanie.ai",    "Risk alerts → executive layer",         Readiness.PILOT, "generic"),
        (39, "pm", "estimator",       "Cross-sync ↔ Estimator + Structural",   Readiness.PILOT, "generic"),
        (40, "pm", "stephanie.ai",    "Actions → memory for pattern learning", Readiness.PILOT, "generic"),
    ]
    for cid, src, tgt, label, ready, action in edges:
        m.add(Connection(
            id=cid, source=src, target=tgt, label=label,
            readiness=ready, authority_action=action,
        ))
    return m
