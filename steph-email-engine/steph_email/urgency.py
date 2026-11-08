"""Urgency scoring and filtering.

Every synced message gets a 0–100 score, a level (critical / high / normal /
low) and the list of reasons that produced it, so the dashboard and the brief
can show *why* something was surfaced instead of just that it was.

The weights are tuned for a construction / real-estate owner's inbox: safety
and legal stops outrank everything, then money and deadlines, then permits.
Newsletters and no-reply senders are pushed down hard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LEVELS: tuple[str, ...] = ("low", "normal", "high", "critical")
RANK: dict[str, int] = {lvl: i for i, lvl in enumerate(LEVELS)}

THRESHOLDS = ((85, "critical"), (50, "high"), (20, "normal"))

_URGENT_WORDS = re.compile(
    r"\b(urgent|asap|immediately|right away|time[- ]sensitive|deadline|final notice|past due|"
    r"overdue|last chance|action required|response required|expires? (today|tomorrow))\b", re.I)
_STOP_WORDS = re.compile(
    r"\b(stop[- ]work|osha|injur(y|ed)|accident|notice of violation|cease and desist|lien|"
    r"lawsuit|subpoena|court date|emergency|gas leak|flood(ing|ed)|fire)\b", re.I)
_DEADLINE = re.compile(
    r"\b(by|before|due|no later than|needed by|respond by)\s+"
    r"(today|tomorrow|tonight|eod|end of (the )?day|cob|noon|this (morning|afternoon|week)|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}/\d{1,2}(/\d{2,4})?|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2})\b", re.I)
_MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?")
_ASKS = re.compile(
    r"\?|\b(please (confirm|advise|approve|sign|review|respond|reply|call|let)|need your|"
    r"can you|could you|would you|let me know|awaiting your|waiting (on|for) you|"
    r"your approval|sign[- ]off|get back to (me|us))\b", re.I)
_NOREPLY = re.compile(r"no-?reply|donotreply|do-not-reply|newsletter|notifications?@|mailer|marketing@|news@|info@", re.I)
_UNSUB = re.compile(r"unsubscribe|view (this|it) in (your )?browser|manage (your )?preferences|email preferences", re.I)
_AUTOMATED_SUBJECT = re.compile(r"^(your (order|receipt|statement)|out of office|automatic reply|auto-?reply|delivery status)", re.I)


@dataclass
class UrgencyResult:
    level: str
    score: int
    reasons: list[str] = field(default_factory=list)
    asks_reply: bool = False

    def as_dict(self) -> dict:
        return {"level": self.level, "score": self.score, "reasons": list(self.reasons), "asks_reply": self.asks_reply}


def level_for(score: int) -> str:
    for threshold, level in THRESHOLDS:
        if score >= threshold:
            return level
    return "low"


def at_least(level: str, minimum: str) -> bool:
    return RANK[level] >= RANK[minimum]


class UrgencyScorer:
    def __init__(self, vip_senders: list[str] | tuple[str, ...] = (), vip_domains: list[str] | tuple[str, ...] = (),
                 owner_addresses: list[str] | tuple[str, ...] = ()):
        self.vip_senders = {s.lower() for s in vip_senders}
        self.vip_domains = {d.lower().lstrip("@") for d in vip_domains}
        self.owner_addresses = {a.lower() for a in owner_addresses}

    def score(self, *, subject: str, from_addr: str, body_text: str, tags: list[str] | None = None,
              to_addrs: list[dict] | None = None, cc_addrs: list[dict] | None = None, flagged: bool = False) -> UrgencyResult:
        tags = set(tags or [])
        head = f"{subject}\n{body_text[:4000]}"
        subject_l = subject.lower()
        sender = from_addr.lower()
        domain = sender.rsplit("@", 1)[-1] if "@" in sender else ""
        score = 20  # a plain human email starts as "normal"
        reasons: list[str] = []

        def add(points: int, why: str) -> None:
            nonlocal score
            score += points
            reasons.append(why)

        # --- outbound from the owner never needs the owner's attention
        if sender in self.owner_addresses:
            return UrgencyResult("low", 0, ["sent by you"], False)

        # --- automated / bulk mail goes down first so nothing below can rescue spam
        automated = False
        if _NOREPLY.search(sender) or "newsletter" in tags:
            add(-30, "automated or bulk sender"); automated = True
        if _UNSUB.search(body_text):
            add(-25, "newsletter footer"); automated = True
        if _AUTOMATED_SUBJECT.search(subject_l):
            add(-15, "automatic notification"); automated = True

        # --- hard stops
        m = _STOP_WORDS.search(head)
        if m:
            add(50, f"safety / legal stop: '{m.group(0).lower()}'")
        elif "safety" in tags or "legal" in tags:
            add(25, "safety or legal matter")

        # --- explicit urgency language
        m = _URGENT_WORDS.search(head)
        if m:
            add(20, f"urgent language: '{m.group(0).lower()}'")
        elif "urgent" in tags:
            add(20, "urgent language")

        # --- deadlines
        m = _DEADLINE.search(head)
        if m:
            add(15, f"deadline: '{m.group(0)}'")

        # --- money on the table
        biggest = 0
        for m in _MONEY.finditer(head):
            try:
                biggest = max(biggest, int(m.group(1).replace(",", "")))
            except ValueError:
                continue
        if biggest >= 100_000:
            add(20, f"large amount: ${biggest:,}")
        elif biggest >= 10_000:
            add(10, f"amount: ${biggest:,}")

        # --- construction / real-estate domain tags
        if "permit" in tags:
            add(15, "permit or inspection")
        if tags & {"contract", "closing"}:
            add(10, "contract or closing paperwork")
        if "invoice" in tags and not automated:
            add(5, "invoice or payment")

        # --- who it is from / to
        if sender in self.vip_senders:
            add(25, "VIP sender")
        elif domain and domain in self.vip_domains:
            add(20, "VIP domain")
        if self.owner_addresses and to_addrs is not None:
            to_set = {a.get("address", "").lower() for a in to_addrs}
            cc_set = {a.get("address", "").lower() for a in (cc_addrs or [])}
            if to_set & self.owner_addresses:
                add(5, "addressed to you")
            elif cc_set & self.owner_addresses:
                add(-5, "you are only cc'd")

        # --- does it want something from you?
        asks = bool(_ASKS.search(head)) and not automated
        if asks:
            add(10, "asks for a response")
        if flagged:
            add(10, "flagged")

        score = max(0, min(100, score))
        return UrgencyResult(level_for(score), score, reasons, asks)


def filter_by_level(messages: list[dict], minimum: str) -> list[dict]:
    """Keep messages at or above ``minimum``, most urgent first."""
    if minimum not in RANK:
        raise ValueError(f"unknown urgency level {minimum!r}; choose one of {', '.join(LEVELS)}")
    kept = [m for m in messages if at_least(m.get("urgency", "normal"), minimum)]
    return sorted(kept, key=lambda m: (-RANK[m.get("urgency", "normal")], -int(m.get("urgency_score", 0)), m.get("date") or ""), )
