"""Sales object model — keyed on ``rep_id``.

Every opportunity carries the fields management asked for on day one:
contact_id, deal_id, property_address, source, project_type, pipeline_stage,
last_touch, next_action, next_action_due, assigned_rep, confidence and
qualification. Fields that only exist after a stage (deal_id, assigned_rep)
are Optional; ``required_fields_for_stage`` says when each becomes mandatory
and the compliance_data_agent enforces it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


# --- Enumerations -----------------------------------------------------------
class LeadSource(str, Enum):
    """Canonical source. Every channel maps onto one of these — one intake."""
    SOCIAL = "social"
    PHONE = "phone"
    WEBSITE = "website"
    EMAIL = "email"
    REFERRAL = "referral"
    OTHER = "other"


class PipelineStage(str, Enum):
    """The operating loop, in order."""
    LEAD_ENTERED = "lead_entered"
    DEDUPED = "deduped"
    QUALIFIED = "qualified"
    ASSIGNED = "assigned"
    RESPONDED = "responded"              # immediate response sent
    APPOINTMENT = "appointment"
    SCOPE_ESTIMATE = "scope_estimate"
    FOLLOW_UP = "follow_up"
    CONTRACT_DEPOSIT = "contract_deposit"
    PRODUCTION_HANDOFF = "production_handoff"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"


STAGE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.LEAD_ENTERED, PipelineStage.DEDUPED, PipelineStage.QUALIFIED,
    PipelineStage.ASSIGNED, PipelineStage.RESPONDED, PipelineStage.APPOINTMENT,
    PipelineStage.SCOPE_ESTIMATE, PipelineStage.FOLLOW_UP,
    PipelineStage.CONTRACT_DEPOSIT, PipelineStage.PRODUCTION_HANDOFF,
    PipelineStage.CLOSED_WON,
)
TERMINAL_STAGES = frozenset({PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST})

# Stages the lead_router may set on its own (capture -> dedupe -> qualify ->
# assign). Everything after requires an executed human-approved action or
# rep input — an agent never "sends" or "books" its way forward by itself.
AUTONOMOUS_STAGES = frozenset({
    PipelineStage.LEAD_ENTERED, PipelineStage.DEDUPED,
    PipelineStage.QUALIFIED, PipelineStage.ASSIGNED,
})


def stage_index(stage: PipelineStage) -> int:
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else len(STAGE_ORDER)


def next_stage(stage: PipelineStage) -> Optional[PipelineStage]:
    if stage in TERMINAL_STAGES:
        return None
    i = STAGE_ORDER.index(stage)
    return STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None


class Qualification(str, Enum):
    UNQUALIFIED = "unqualified"
    QUALIFYING = "qualifying"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"


class ProjectType(str, Enum):
    """Mirrors the ``leads.projectType`` enum in nobleport-gateway."""
    ROOF = "roof"
    DECK = "deck"
    SIDING = "siding"
    FOUNDATION = "foundation"
    ADDITION = "addition"
    RENOVATION = "renovation"
    NEW_CONSTRUCTION = "new_construction"
    COMMERCIAL = "commercial"
    DEMOLITION = "demolition"
    HVAC = "hvac"
    ELECTRICAL = "electrical"
    PLUMBING = "plumbing"
    PAINTING = "painting"
    ADU = "adu"
    OTHER = "other"


class Timeline(str, Enum):
    ASAP = "asap"
    ONE_MONTH = "1_month"
    THREE_MONTHS = "3_months"
    SIX_MONTHS = "6_months"
    FLEXIBLE = "flexible"


class Gate(str, Enum):
    AUTONOMOUS = "autonomous"
    HUMAN_GATED = "human_gated"


class ActionType(str, Enum):
    # Autonomous — research / monitoring / prioritization / drafting / hygiene
    RESEARCH_ACCOUNT = "research_account"
    SCORE_LEAD = "score_lead"
    DEDUPE_FLAG = "dedupe_flag"
    CAPTURE_LEAD = "capture_lead"            # create contact+deal from intake
    ASSIGN_REP = "assign_rep"                # initial assignment only
    ADVANCE_STAGE = "advance_stage"          # within AUTONOMOUS_STAGES only
    SET_NEXT_ACTION = "set_next_action"
    ADD_NOTE = "add_note"
    ADD_TASK = "add_task"
    DRAFT_OUTREACH = "draft_outreach"
    DRAFT_MEETING_BRIEF = "draft_meeting_brief"
    DRAFT_ESTIMATE_INPUTS = "draft_estimate_inputs"
    PRIORITIZE = "prioritize"
    STALE_FLAG = "stale_flag"
    DATA_QUALITY_FLAG = "data_quality_flag"
    COACHING_INSIGHT = "coaching_insight"
    # Human-gated — outbound comms, pricing, commitments, material CRM changes
    SEND_OUTBOUND = "send_outbound"
    FINAL_PRICING = "final_pricing"
    SEND_PROPOSAL = "send_proposal"
    CONTRACT_COMMITMENT = "contract_commitment"
    DEPOSIT_REQUEST = "deposit_request"
    REASSIGN_REP = "reassign_rep"
    MERGE_CONTACT = "merge_contact"
    DELETE_RECORD = "delete_record"
    CHANGE_DEAL_AMOUNT = "change_deal_amount"
    CLOSE_DEAL = "close_deal"                # closed won / lost
    PRODUCTION_HANDOFF = "production_handoff"
    REDACT_SENSITIVE = "redact_sensitive"    # rewriting customer data


class ActionStatus(str, Enum):
    PROPOSED = "proposed"
    EXECUTED = "executed"
    PARKED = "parked"        # human-gated, awaiting signature
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class OutboundChannel(str, Enum):
    SMS = "sms"
    EMAIL = "email"
    SOCIAL = "social"
    PHONE = "phone"


# --- Reps ---------------------------------------------------------------------
class SalesRep(BaseModel):
    rep_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    email: Optional[str] = None
    hubspot_owner_id: Optional[str] = None
    territories: list[str] = Field(default_factory=list,
                                   description="lower-cased town / region names")
    project_types: list[ProjectType] = Field(default_factory=list,
                                             description="empty = any")
    capacity: int = Field(default=25, ge=1, description="max open opportunities")
    active: bool = True


# --- Contact -------------------------------------------------------------------
class Contact(BaseModel):
    contact_id: Optional[str] = None
    name: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    consent_sms: bool = False
    consent_email: bool = False
    do_not_contact: bool = False


# --- Raw inbound lead (any channel) ---------------------------------------------
class RawLead(BaseModel):
    """What a channel adapter hands to the intake layer. Loose on purpose."""
    channel: str = Field(min_length=1, description="e.g. instagram, web_form, inbound_call")
    name: str = ""
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    property_address: Optional[str] = None
    project_type: Optional[str] = None
    description: Optional[str] = None
    sqft: Optional[int] = None
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    timeline: Optional[str] = None
    referred_by: Optional[str] = None
    consent_sms: bool = False
    consent_email: bool = False
    utm: dict[str, str] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=utcnow)
    raw: dict[str, Any] = Field(default_factory=dict)


# --- Touch history -----------------------------------------------------------------
class Touch(BaseModel):
    at: datetime
    direction: str                       # inbound | outbound
    channel: str
    by: Optional[str] = None             # rep_id or agent name
    summary: str = ""


# --- Opportunity -------------------------------------------------------------------
class Opportunity(BaseModel):
    opportunity_id: str = Field(default_factory=lambda: _new_id("opp"))
    contact_id: Optional[str] = None
    deal_id: Optional[str] = None
    property_address: Optional[str] = None
    source: LeadSource = LeadSource.OTHER
    channel: Optional[str] = None
    project_type: ProjectType = ProjectType.OTHER
    pipeline_stage: PipelineStage = PipelineStage.LEAD_ENTERED
    last_touch: Optional[datetime] = None
    next_action: Optional[str] = None
    next_action_due: Optional[datetime] = None
    assigned_rep: Optional[str] = Field(default=None, description="rep_id")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    qualification: Qualification = Qualification.UNQUALIFIED

    # Supporting detail
    contact: Contact = Field(default_factory=Contact)
    score: int = Field(default=0, ge=0, le=100)
    score_breakdown: dict[str, int] = Field(default_factory=dict)
    description: Optional[str] = None
    sqft: Optional[int] = None
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    timeline: Timeline = Timeline.FLEXIBLE
    referred_by: Optional[str] = None
    deal_amount: Optional[int] = None
    stage_entered_at: datetime = Field(default_factory=utcnow)
    created_at: datetime = Field(default_factory=utcnow)
    touches: list[Touch] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    duplicate_of: Optional[str] = None
    lost_reason: Optional[str] = None

    @field_validator("assigned_rep")
    @classmethod
    def _rep_not_blank(cls, v):
        if v is not None and not v.strip():
            raise ValueError("assigned_rep must be a rep_id or None")
        return v

    @property
    def is_open(self) -> bool:
        return self.pipeline_stage not in TERMINAL_STAGES

    def stage_age(self, now: Optional[datetime] = None) -> timedelta:
        return (now or utcnow()) - self.stage_entered_at

    def touch(self, direction: str, channel: str, by: Optional[str], summary: str,
              at: Optional[datetime] = None) -> None:
        t = Touch(at=at or utcnow(), direction=direction, channel=channel, by=by, summary=summary)
        self.touches.append(t)
        self.last_touch = t.at

    def set_stage(self, stage: PipelineStage, now: Optional[datetime] = None) -> None:
        self.pipeline_stage = stage
        self.stage_entered_at = now or utcnow()


# Fields that must be populated once an opportunity reaches a stage.
REQUIRED_FIELDS_BY_STAGE: dict[PipelineStage, tuple[str, ...]] = {
    PipelineStage.LEAD_ENTERED: ("source", "project_type"),
    PipelineStage.DEDUPED: ("source", "project_type"),
    PipelineStage.QUALIFIED: ("source", "project_type", "contact_id", "qualification"),
    PipelineStage.ASSIGNED: ("source", "project_type", "contact_id", "assigned_rep",
                             "next_action", "next_action_due"),
    PipelineStage.RESPONDED: ("contact_id", "assigned_rep", "last_touch", "next_action"),
    PipelineStage.APPOINTMENT: ("contact_id", "deal_id", "assigned_rep", "property_address",
                                "next_action", "next_action_due"),
    PipelineStage.SCOPE_ESTIMATE: ("contact_id", "deal_id", "assigned_rep", "property_address",
                                   "next_action"),
    PipelineStage.FOLLOW_UP: ("contact_id", "deal_id", "assigned_rep", "next_action",
                              "next_action_due"),
    PipelineStage.CONTRACT_DEPOSIT: ("contact_id", "deal_id", "assigned_rep",
                                     "property_address", "deal_amount"),
    PipelineStage.PRODUCTION_HANDOFF: ("contact_id", "deal_id", "assigned_rep",
                                       "property_address", "deal_amount"),
    PipelineStage.CLOSED_WON: ("contact_id", "deal_id", "assigned_rep", "deal_amount"),
    PipelineStage.CLOSED_LOST: ("contact_id", "assigned_rep", "lost_reason"),
}


def required_fields_for_stage(stage: PipelineStage) -> tuple[str, ...]:
    return REQUIRED_FIELDS_BY_STAGE.get(stage, ())


def missing_required_fields(opp: Opportunity) -> list[str]:
    missing = []
    for name in required_fields_for_stage(opp.pipeline_stage):
        value = getattr(opp, name, None)
        if value is None or value == "" or value == Qualification.UNQUALIFIED:
            missing.append(name)
    return missing


# --- Actions -----------------------------------------------------------------------
class SalesAction(BaseModel):
    """A proposal from an agent. Autonomous ones execute; gated ones PARK."""
    action_id: str = Field(default_factory=lambda: _new_id("act"))
    action_type: ActionType
    agent: str
    opportunity_id: Optional[str] = None
    rep_id: Optional[str] = None
    gate: Gate = Gate.AUTONOMOUS
    approval_level: str = "L1"
    status: ActionStatus = ActionStatus.PROPOSED
    rationale: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    channel: Optional[OutboundChannel] = None
    human_signature: Optional[str] = None
    approved_by: Optional[str] = None
    rejected_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    truth_label: str = "STAGED"


class AgentFinding(BaseModel):
    agent: str
    responsibility: str
    truth_label: str = "STAGED"
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    actions: list[SalesAction] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list,
                                description="channels/actions this finding vetoes")
