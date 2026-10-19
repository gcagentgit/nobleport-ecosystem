"""CRM layer — HubSpot is the source of truth.

``CrmClient`` is the narrow interface the agents use. ``InMemoryCrm`` backs
tests and local runs; ``HubSpotCrm`` talks to the HubSpot v3 CRM API and is
STAGED until a private-app token is configured. Both refuse *material*
writes without a human signature — defense in depth behind the gating layer.

Opportunity <-> HubSpot mapping lives here so Python never invents a field
name the CRM does not have.
"""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any, Optional, Protocol

from .models import Contact, Opportunity, PipelineStage, utcnow


class CrmWriteRefused(PermissionError):
    pass


# Opportunity field -> HubSpot deal property. Custom properties (prefixed
# ``np_``) must exist on the HubSpot portal; ``hs_``/standard ones are built in.
DEAL_PROPERTY_MAP: dict[str, str] = {
    "pipeline_stage": "dealstage",
    "deal_amount": "amount",
    "assigned_rep": "hubspot_owner_id",          # via SalesRep.hubspot_owner_id
    "property_address": "np_property_address",
    "source": "np_lead_source",
    "channel": "np_lead_channel",
    "project_type": "np_project_type",
    "next_action": "np_next_action",
    "next_action_due": "np_next_action_due",
    "last_touch": "notes_last_contacted",
    "confidence": "np_confidence",
    "qualification": "np_qualification",
    "score": "np_lead_score",
    "opportunity_id": "np_opportunity_id",
    "sqft": "np_sqft",
    "budget_min": "np_budget_min",
    "budget_max": "np_budget_max",
    "timeline": "np_timeline",
    "referred_by": "np_referred_by",
    "lost_reason": "closed_lost_reason",
}

CONTACT_PROPERTY_MAP: dict[str, str] = {
    "name": "firstname",                          # split on write
    "email": "email",
    "phone": "phone",
    "company": "company",
    "consent_sms": "np_consent_sms",
    "consent_email": "np_consent_email",
    "do_not_contact": "np_do_not_contact",
}

# Default HubSpot dealstage ids: one per pipeline stage, same label. Portals
# with custom stage ids override via HubSpotCrm(stage_map=...).
DEFAULT_STAGE_MAP: dict[PipelineStage, str] = {s: s.value for s in PipelineStage}

# Writes that count as "material" (see gating.GATED_GROUPS) — the CRM layer
# refuses them without a signature even if a caller skipped the orchestrator.
MATERIAL_DEAL_PROPERTIES = frozenset({"amount", "hubspot_owner_id", "closed_lost_reason"})
MATERIAL_STAGES = frozenset({
    PipelineStage.CONTRACT_DEPOSIT, PipelineStage.PRODUCTION_HANDOFF,
    PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST,
})


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "value"):
        return v.value
    return v


def deal_properties(opp: Opportunity, *, owner_id: Optional[str] = None,
                    stage_map: Optional[dict[PipelineStage, str]] = None) -> dict[str, Any]:
    stage_map = stage_map or DEFAULT_STAGE_MAP
    props: dict[str, Any] = {}
    for field_name, prop in DEAL_PROPERTY_MAP.items():
        value = getattr(opp, field_name, None)
        if value is None:
            continue
        if field_name == "pipeline_stage":
            value = stage_map.get(value, value.value)
        elif field_name == "assigned_rep":
            value = owner_id or value
        props[prop] = _iso(value)
    props["dealname"] = f"{opp.contact.name or 'Lead'} — {opp.project_type.value} — {opp.property_address or 'TBD'}"
    return props


def contact_properties(contact: Contact) -> dict[str, Any]:
    first, _, last = (contact.name or "").partition(" ")
    props: dict[str, Any] = {"firstname": first, "lastname": last}
    for field_name, prop in CONTACT_PROPERTY_MAP.items():
        if field_name == "name":
            continue
        value = getattr(contact, field_name, None)
        if value is not None:
            props[prop] = value
    return props


def is_material_deal_update(properties: dict[str, Any], *,
                            stage_map: Optional[dict[PipelineStage, str]] = None,
                            current: Optional[dict[str, Any]] = None) -> bool:
    """Amount, owner and lost-reason are material — except setting the first
    owner on an ownerless deal (initial assignment, which is autonomous)."""
    material = MATERIAL_DEAL_PROPERTIES & set(properties)
    if "hubspot_owner_id" in material and current is not None and not current.get("hubspot_owner_id"):
        material = material - {"hubspot_owner_id"}
    if material:
        return True
    stage = properties.get("dealstage")
    if stage is None:
        return False
    stage_map = stage_map or DEFAULT_STAGE_MAP
    material_ids = {stage_map[s] for s in MATERIAL_STAGES}
    return stage in material_ids


# --- Interface ---------------------------------------------------------------
class CrmClient(Protocol):
    async def find_contact(self, *, email: Optional[str], phone: Optional[str]) -> Optional[str]: ...
    async def upsert_contact(self, contact: Contact) -> str: ...
    async def get_contact(self, contact_id: str) -> dict[str, Any]: ...
    async def create_deal(self, opp: Opportunity, *, owner_id: Optional[str] = None) -> str: ...
    async def update_deal(self, deal_id: str, properties: dict[str, Any], *,
                          human_signature: Optional[str] = None) -> None: ...
    async def get_deal(self, deal_id: str) -> dict[str, Any]: ...
    async def deals_for_contact(self, contact_id: str) -> list[dict[str, Any]]: ...
    async def add_note(self, *, deal_id: Optional[str], contact_id: Optional[str],
                       body: str, by: str) -> str: ...
    async def create_task(self, *, deal_id: Optional[str], subject: str, due: datetime,
                          owner_id: Optional[str], by: str) -> str: ...
    async def log_outbound(self, *, deal_id: Optional[str], channel: str, body: str,
                           by: str, human_signature: str) -> str: ...


# --- In-memory (tests / local) ------------------------------------------------
class InMemoryCrm:
    def __init__(self, *, stage_map: Optional[dict[PipelineStage, str]] = None):
        self.stage_map = stage_map or DEFAULT_STAGE_MAP
        self.contacts: dict[str, dict[str, Any]] = {}
        self.deals: dict[str, dict[str, Any]] = {}
        self.notes: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.outbound: list[dict[str, Any]] = []
        self._ids = itertools.count(1)

    def _next(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    async def find_contact(self, *, email=None, phone=None) -> Optional[str]:
        for cid, c in self.contacts.items():
            if email and c.get("email") == email:
                return cid
            if phone and c.get("phone") == phone:
                return cid
        return None

    async def upsert_contact(self, contact: Contact) -> str:
        cid = contact.contact_id or await self.find_contact(email=contact.email, phone=contact.phone)
        props = contact_properties(contact)
        if cid and cid in self.contacts:
            self.contacts[cid].update({k: v for k, v in props.items() if v not in (None, "")})
            return cid
        cid = self._next("c")
        self.contacts[cid] = {"id": cid, **props}
        return cid

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        return dict(self.contacts.get(contact_id, {}))

    async def create_deal(self, opp: Opportunity, *, owner_id=None) -> str:
        did = self._next("d")
        self.deals[did] = {"id": did, "contact_id": opp.contact_id,
                           **deal_properties(opp, owner_id=owner_id, stage_map=self.stage_map)}
        return did

    async def update_deal(self, deal_id: str, properties: dict[str, Any], *,
                          human_signature=None) -> None:
        current = self.deals[deal_id]
        if (is_material_deal_update(properties, stage_map=self.stage_map, current=current)
                and not human_signature):
            raise CrmWriteRefused(f"material deal update on {deal_id} requires a human signature")
        current.update({k: _iso(v) for k, v in properties.items()})
        if human_signature:
            self.deals[deal_id]["_last_signature"] = human_signature

    async def get_deal(self, deal_id: str) -> dict[str, Any]:
        return dict(self.deals.get(deal_id, {}))

    async def deals_for_contact(self, contact_id: str) -> list[dict[str, Any]]:
        return [dict(d) for d in self.deals.values() if d.get("contact_id") == contact_id]

    async def add_note(self, *, deal_id, contact_id, body, by) -> str:
        nid = self._next("n")
        self.notes.append({"id": nid, "deal_id": deal_id, "contact_id": contact_id,
                           "body": body, "by": by, "at": utcnow().isoformat()})
        return nid

    async def create_task(self, *, deal_id, subject, due, owner_id, by) -> str:
        tid = self._next("t")
        self.tasks.append({"id": tid, "deal_id": deal_id, "subject": subject,
                           "due": due.isoformat(), "owner_id": owner_id, "by": by})
        return tid

    async def log_outbound(self, *, deal_id, channel, body, by, human_signature) -> str:
        if not human_signature:
            raise CrmWriteRefused("outbound communication requires a human signature")
        oid = self._next("o")
        self.outbound.append({"id": oid, "deal_id": deal_id, "channel": channel,
                              "body": body, "by": by, "signature": human_signature,
                              "at": utcnow().isoformat()})
        return oid


# --- HubSpot (STAGED until a token is configured) ------------------------------
class HubSpotCrm:
    """HubSpot CRM v3. Construct with a private-app token. Requires httpx.

    Every method maps to a documented endpoint; nothing is fabricated when the
    token is missing — calls raise so the caller knows the CRM is not wired.
    """

    BASE = "https://api.hubapi.com"

    def __init__(self, token: Optional[str], *, pipeline_id: str = "default",
                 stage_map: Optional[dict[PipelineStage, str]] = None,
                 timeout_s: float = 10.0):
        self.token = token
        self.pipeline_id = pipeline_id
        self.stage_map = stage_map or DEFAULT_STAGE_MAP
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _client(self):
        import httpx
        if not self.configured:
            raise RuntimeError("HubSpot token not configured — CRM is STAGED, not LIVE")
        return httpx.AsyncClient(base_url=self.BASE, timeout=self.timeout_s,
                                 headers={"Authorization": f"Bearer {self.token}"})

    async def _search(self, obj: str, prop: str, value: str) -> Optional[str]:
        async with self._client() as c:
            r = await c.post(f"/crm/v3/objects/{obj}/search", json={
                "filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}],
                "limit": 1})
            r.raise_for_status()
            results = r.json().get("results", [])
            return results[0]["id"] if results else None

    async def find_contact(self, *, email=None, phone=None) -> Optional[str]:
        if email:
            cid = await self._search("contacts", "email", email)
            if cid:
                return cid
        if phone:
            return await self._search("contacts", "phone", phone)
        return None

    async def upsert_contact(self, contact: Contact) -> str:
        cid = contact.contact_id or await self.find_contact(email=contact.email, phone=contact.phone)
        props = contact_properties(contact)
        async with self._client() as c:
            if cid:
                r = await c.patch(f"/crm/v3/objects/contacts/{cid}", json={"properties": props})
            else:
                r = await c.post("/crm/v3/objects/contacts", json={"properties": props})
            r.raise_for_status()
            return r.json()["id"]

    async def get_contact(self, contact_id: str) -> dict[str, Any]:
        async with self._client() as c:
            r = await c.get(f"/crm/v3/objects/contacts/{contact_id}",
                            params={"properties": ",".join(CONTACT_PROPERTY_MAP.values())})
            r.raise_for_status()
            return r.json().get("properties", {})

    async def create_deal(self, opp: Opportunity, *, owner_id=None) -> str:
        props = deal_properties(opp, owner_id=owner_id, stage_map=self.stage_map)
        props["pipeline"] = self.pipeline_id
        body: dict[str, Any] = {"properties": props}
        if opp.contact_id:
            body["associations"] = [{"to": {"id": opp.contact_id},
                                     "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                                "associationTypeId": 3}]}]
        async with self._client() as c:
            r = await c.post("/crm/v3/objects/deals", json=body)
            r.raise_for_status()
            return r.json()["id"]

    async def update_deal(self, deal_id: str, properties: dict[str, Any], *,
                          human_signature=None) -> None:
        current = await self.get_deal(deal_id) if "hubspot_owner_id" in properties else None
        if (is_material_deal_update(properties, stage_map=self.stage_map, current=current)
                and not human_signature):
            raise CrmWriteRefused(f"material deal update on {deal_id} requires a human signature")
        async with self._client() as c:
            r = await c.patch(f"/crm/v3/objects/deals/{deal_id}",
                              json={"properties": {k: _iso(v) for k, v in properties.items()}})
            r.raise_for_status()

    async def get_deal(self, deal_id: str) -> dict[str, Any]:
        async with self._client() as c:
            r = await c.get(f"/crm/v3/objects/deals/{deal_id}",
                            params={"properties": ",".join(DEAL_PROPERTY_MAP.values())})
            r.raise_for_status()
            return r.json().get("properties", {})

    async def deals_for_contact(self, contact_id: str) -> list[dict[str, Any]]:
        async with self._client() as c:
            r = await c.get(f"/crm/v4/objects/contacts/{contact_id}/associations/deals")
            r.raise_for_status()
            ids = [x["toObjectId"] for x in r.json().get("results", [])]
            out = []
            for did in ids:
                d = await c.get(f"/crm/v3/objects/deals/{did}",
                                params={"properties": ",".join(DEAL_PROPERTY_MAP.values())})
                d.raise_for_status()
                out.append({"id": str(did), **d.json().get("properties", {})})
            return out

    async def _engagement(self, obj: str, props: dict[str, Any], deal_id: Optional[str],
                          contact_id: Optional[str], assoc_type: int) -> str:
        body: dict[str, Any] = {"properties": props, "associations": []}
        if deal_id:
            body["associations"].append({"to": {"id": deal_id},
                                         "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                                    "associationTypeId": assoc_type}]})
        async with self._client() as c:
            r = await c.post(f"/crm/v3/objects/{obj}", json=body)
            r.raise_for_status()
            return r.json()["id"]

    async def add_note(self, *, deal_id, contact_id, body, by) -> str:
        return await self._engagement("notes", {
            "hs_timestamp": utcnow().isoformat(), "hs_note_body": f"[{by}] {body}"},
            deal_id, contact_id, 214)

    async def create_task(self, *, deal_id, subject, due, owner_id, by) -> str:
        props = {"hs_timestamp": due.isoformat(), "hs_task_subject": subject,
                 "hs_task_status": "NOT_STARTED", "hs_task_body": f"created by {by}"}
        if owner_id:
            props["hubspot_owner_id"] = owner_id
        return await self._engagement("tasks", props, deal_id, None, 216)

    async def log_outbound(self, *, deal_id, channel, body, by, human_signature) -> str:
        if not human_signature:
            raise CrmWriteRefused("outbound communication requires a human signature")
        obj = {"sms": "communications", "social": "communications",
               "email": "emails", "phone": "calls"}.get(channel, "communications")
        props = {"hs_timestamp": utcnow().isoformat(),
                 "hs_communication_channel_type": channel.upper(),
                 "hs_communication_body": body,
                 "hs_communication_logged_from": "CRM"}
        if obj == "emails":
            props = {"hs_timestamp": props["hs_timestamp"], "hs_email_direction": "EMAIL",
                     "hs_email_text": body, "hs_email_status": "SENT"}
        elif obj == "calls":
            props = {"hs_timestamp": props["hs_timestamp"], "hs_call_body": body,
                     "hs_call_direction": "OUTBOUND", "hs_call_status": "COMPLETED"}
        assoc = {"communications": 81, "emails": 210, "calls": 206}[obj]
        return await self._engagement(obj, props, deal_id, None, assoc)
