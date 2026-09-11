"""Auto-tagging rules tuned for a construction / real-estate operation.

Rules are plain keyword lists matched (case-insensitive) against the subject,
sender and body text.  Override or extend them with a JSON file at
``<data_dir>/rules.json`` shaped like ``{"tag": ["keyword", ...], ...}``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_RULES: dict[str, list[str]] = {
    "permit": ["permit", "building department", "inspection", "certificate of occupancy", "zoning", "variance"],
    "bid": ["bid", "rfp", "rfq", "proposal", "quote", "estimate", "takeoff"],
    "invoice": ["invoice", "payment due", "past due", "remittance", "statement", "receipt", "pay app", "aia g702"],
    "contract": ["contract", "agreement", "change order", "subcontract", "lien waiver", "addendum"],
    "closing": ["closing", "title", "escrow", "deed", "settlement statement", "hud-1", "wire instructions"],
    "lease": ["lease", "tenant", "rent", "renewal", "landlord", "security deposit", "eviction"],
    "insurance": ["insurance", "certificate of insurance", "coi", "policy", "claim", "bond"],
    "schedule": ["schedule", "delay", "milestone", "site meeting", "walkthrough", "punch list"],
    "safety": ["osha", "safety", "incident", "injury", "hazard"],
    "supplier": ["delivery", "purchase order", "backorder", "material", "lumber", "concrete"],
    "legal": ["attorney", "counsel", "lawsuit", "litigation", "subpoena", "notice of"],
    "urgent": ["urgent", "asap", "immediately", "deadline", "final notice"],
}

_SENDER_RULES: dict[str, list[str]] = {
    "finance": ["quickbooks", "intuit", "stripe", "paypal", "mercury", "bank"],
    "docusign": ["docusign", "signnow", "hellosign", "pandadoc"],
}


class Tagger:
    def __init__(self, rules: dict[str, list[str]] | None = None, sender_rules: dict[str, list[str]] | None = None):
        self.rules = rules if rules is not None else DEFAULT_RULES
        self.sender_rules = sender_rules if sender_rules is not None else _SENDER_RULES
        self._compiled = {
            tag: re.compile(r"\b(?:" + "|".join(re.escape(k) for k in kws) + r")\b", re.IGNORECASE)
            for tag, kws in self.rules.items() if kws
        }

    @classmethod
    def load(cls, data_dir: Path) -> "Tagger":
        path = data_dir / "rules.json"
        if path.exists():
            custom = json.loads(path.read_text())
            merged = {**DEFAULT_RULES, **{k: list(v) for k, v in custom.items()}}
            return cls(merged)
        return cls()

    def tags_for(self, subject: str, from_addr: str, body_text: str) -> list[str]:
        head = f"{subject}\n{body_text[:4000]}"
        tags = [tag for tag, rx in self._compiled.items() if rx.search(head)]
        sender = from_addr.lower()
        for tag, kws in self.sender_rules.items():
            if any(k in sender for k in kws):
                tags.append(tag)
        return sorted(set(tags))
