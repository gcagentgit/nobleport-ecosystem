"""Module 1 — Stephanie.ai: Executive Orchestration & Avatar Layer (1–10)."""

from ..core.connection import Connection
from ..core.module import Module
from ..core.readiness import Readiness


def build() -> Module:
    m = Module(
        name="stephanie.ai",
        description="Executive orchestration, avatar, and single authority",
    )

    edges = [
        (1,  "langgraph", "all_modules",    "LangGraph multi-agent orchestration",     Readiness.LIVE, "generic"),
        (2,  "livekit",   "elevenlabs",     "Voice pipeline → Boston accent TTS",      Readiness.PILOT, "generic"),
        (3,  "outcomes",  "rag",            "RAG / NotebookLM closed-loop learning",   Readiness.PILOT, "generic"),
        (4,  "authority", "hitl",           "Authority Matrix + HITL Console → L3/L4", Readiness.LIVE, "generic"),
        (5,  "forecast",  "coach",          "Predictive Intelligence Decision Coach",  Readiness.SIMULATION, "generic"),
        (6,  "stephanie", "auditbeacon",    "AuditBeacon before state change",         Readiness.LIVE, "generic"),
        (7,  "mcp",       "llm_router",     "MCP Gateway → Claude/Gemini/Grok",        Readiness.PILOT, "generic"),
        (8,  "telegram",  "social_signals", "Telegram bot + social signal ingestion",  Readiness.PILOT, "generic"),
        (9,  "stephanie", "readiness",      "Feature Readiness Matrix gate",           Readiness.LIVE, "generic"),
        (10, "outcomes",  "agi_loop",       "Outcome node logging → AGI loop",         Readiness.PILOT, "generic"),
    ]
    for cid, src, tgt, label, ready, action in edges:
        m.add(Connection(
            id=cid, source=src, target=tgt, label=label,
            readiness=ready, authority_action=action,
        ))
    return m
