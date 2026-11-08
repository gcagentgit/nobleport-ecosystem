"""Unified intake layer — every channel lands in the same pipeline.

    normalize(raw)  channel -> canonical LeadSource + clean fields
    Deduper         fingerprints (email / phone / address) -> existing opp
    Assigner        territory -> project-type fit -> capacity -> fewest open

Social, phone, website, email and referral leads do not get separate
pipelines; they get a ``source`` tag on one Opportunity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import (Contact, LeadSource, Opportunity, PipelineStage, ProjectType,
                     RawLead, SalesRep, Timeline)

# channel (what the adapter says) -> canonical source
CHANNEL_SOURCE: dict[str, LeadSource] = {
    # social
    "instagram": LeadSource.SOCIAL, "facebook": LeadSource.SOCIAL, "meta": LeadSource.SOCIAL,
    "linkedin": LeadSource.SOCIAL, "tiktok": LeadSource.SOCIAL, "x": LeadSource.SOCIAL,
    "twitter": LeadSource.SOCIAL, "youtube": LeadSource.SOCIAL, "nextdoor": LeadSource.SOCIAL,
    "social": LeadSource.SOCIAL, "social_dm": LeadSource.SOCIAL,
    # phone / messaging
    "phone": LeadSource.PHONE, "inbound_call": LeadSource.PHONE, "voicemail": LeadSource.PHONE,
    "sms": LeadSource.PHONE, "text": LeadSource.PHONE, "whatsapp": LeadSource.PHONE,
    "telegram": LeadSource.PHONE,
    # website
    "website": LeadSource.WEBSITE, "web_form": LeadSource.WEBSITE, "intake_form": LeadSource.WEBSITE,
    "chat_widget": LeadSource.WEBSITE, "api": LeadSource.WEBSITE, "landing_page": LeadSource.WEBSITE,
    # email
    "email": LeadSource.EMAIL, "inbox": LeadSource.EMAIL, "contact_email": LeadSource.EMAIL,
    # referral
    "referral": LeadSource.REFERRAL, "referral_partner": LeadSource.REFERRAL,
    "past_customer": LeadSource.REFERRAL, "realtor": LeadSource.REFERRAL,
    "architect": LeadSource.REFERRAL, "word_of_mouth": LeadSource.REFERRAL,
}

_PHONE_DIGITS = re.compile(r"\D+")
_ADDR_PUNCT = re.compile(r"[^\w\s]")
_ADDR_ABBR = {
    "street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "court": "ct", "boulevard": "blvd", "place": "pl", "terrace": "ter",
    "circle": "cir", "north": "n", "south": "s", "east": "e", "west": "w",
    "apartment": "apt", "unit": "unit", "suite": "ste",
}


def source_for_channel(channel: str) -> LeadSource:
    key = (channel or "").strip().lower().replace("-", "_").replace(" ", "_")
    return CHANNEL_SOURCE.get(key, LeadSource.OTHER)


def norm_email(email: Optional[str]) -> Optional[str]:
    e = (email or "").strip().lower()
    return e or None


def norm_phone(phone: Optional[str]) -> Optional[str]:
    digits = _PHONE_DIGITS.sub("", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) >= 10 else (digits or None)


def norm_address(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    a = _ADDR_PUNCT.sub(" ", address.lower())
    words = [_ADDR_ABBR.get(w, w) for w in a.split()]
    return " ".join(words) or None


def _project_type(value: Optional[str]) -> ProjectType:
    key = (value or "other").strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return ProjectType(key)
    except ValueError:
        return ProjectType.OTHER


def _timeline(value: Optional[str]) -> Timeline:
    key = (value or "flexible").strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return Timeline(key)
    except ValueError:
        return Timeline.FLEXIBLE


def normalize(raw: RawLead) -> Opportunity:
    """One RawLead from any channel -> one Opportunity at LEAD_ENTERED."""
    source = source_for_channel(raw.channel)
    if raw.referred_by and source is LeadSource.OTHER:
        source = LeadSource.REFERRAL
    contact = Contact(
        name=(raw.name or "").strip(), email=norm_email(raw.email),
        phone=norm_phone(raw.phone), company=(raw.company or "").strip() or None,
        consent_sms=raw.consent_sms, consent_email=raw.consent_email,
    )
    opp = Opportunity(
        source=source, channel=raw.channel.strip().lower(),
        project_type=_project_type(raw.project_type),
        property_address=(raw.property_address or "").strip() or None,
        description=(raw.description or "").strip() or None,
        sqft=raw.sqft, budget_min=raw.budget_min, budget_max=raw.budget_max,
        timeline=_timeline(raw.timeline), referred_by=raw.referred_by,
        contact=contact, created_at=raw.received_at, stage_entered_at=raw.received_at,
        tags=[f"utm:{k}={v}" for k, v in sorted(raw.utm.items())],
    )
    opp.touch("inbound", raw.channel, None, "Lead received", at=raw.received_at)
    opp.set_stage(PipelineStage.LEAD_ENTERED, now=raw.received_at)
    return opp


# --- Dedupe -----------------------------------------------------------------
@dataclass
class DedupeMatch:
    opportunity_id: str
    matched_on: str          # email | phone | address


@dataclass
class Deduper:
    """In-memory fingerprint index. Address match alone is a *soft* match —
    the same house can have a second, unrelated project — so it is only
    reported when the opportunity at that address is still open."""
    by_email: dict[str, str] = field(default_factory=dict)
    by_phone: dict[str, str] = field(default_factory=dict)
    by_address: dict[str, str] = field(default_factory=dict)
    open_ids: set[str] = field(default_factory=set)

    def index(self, opp: Opportunity) -> None:
        if opp.contact.email:
            self.by_email.setdefault(opp.contact.email, opp.opportunity_id)
        if opp.contact.phone:
            self.by_phone.setdefault(opp.contact.phone, opp.opportunity_id)
        addr = norm_address(opp.property_address)
        if addr:
            self.by_address.setdefault(addr, opp.opportunity_id)
        if opp.is_open:
            self.open_ids.add(opp.opportunity_id)
        else:
            self.open_ids.discard(opp.opportunity_id)

    def find(self, opp: Opportunity) -> Optional[DedupeMatch]:
        for key, table, label in ((opp.contact.email, self.by_email, "email"),
                                  (opp.contact.phone, self.by_phone, "phone")):
            if key and key in table and table[key] != opp.opportunity_id:
                return DedupeMatch(table[key], label)
        addr = norm_address(opp.property_address)
        if addr and addr in self.by_address:
            other = self.by_address[addr]
            if other != opp.opportunity_id and other in self.open_ids:
                return DedupeMatch(other, "address")
        return None


# --- Assignment ---------------------------------------------------------------
@dataclass
class Assigner:
    reps: dict[str, SalesRep]
    open_counts: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_reps(cls, reps: Iterable[SalesRep]) -> "Assigner":
        return cls(reps={r.rep_id: r for r in reps})

    def _town(self, address: Optional[str]) -> str:
        return norm_address(address) or ""

    def choose(self, opp: Opportunity) -> Optional[str]:
        """Territory match first, then project-type fit, then capacity, then
        the rep with the fewest open opportunities. Deterministic."""
        candidates = [r for r in self.reps.values() if r.active]
        if not candidates:
            return None
        addr = self._town(opp.property_address)
        territorial = [r for r in candidates
                       if any(t.lower() in addr for t in r.territories)] if addr else []
        pool = territorial or candidates
        specialists = [r for r in pool if r.project_types and opp.project_type in r.project_types]
        generalists = [r for r in pool if not r.project_types]
        pool = specialists or generalists or pool
        with_room = [r for r in pool if self.open_counts.get(r.rep_id, 0) < r.capacity]
        pool = with_room or pool
        best = min(pool, key=lambda r: (self.open_counts.get(r.rep_id, 0), r.rep_id))
        return best.rep_id

    def note_assignment(self, rep_id: str) -> None:
        self.open_counts[rep_id] = self.open_counts.get(rep_id, 0) + 1

    def note_close(self, rep_id: Optional[str]) -> None:
        if rep_id and self.open_counts.get(rep_id, 0) > 0:
            self.open_counts[rep_id] -= 1
