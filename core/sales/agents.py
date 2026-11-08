"""The ten sales sub-agents — one narrow responsibility each.

    lead_router           capture, dedupe, qualify, score, assign, stage
    followup_agent        callbacks, stale leads, follow-up cadence
    outreach_agent        draft SMS / email / social (sending is gated)
    meeting_prep          pre-call / site-visit briefs
    account_research      homeowner / company / property / account context
    pipeline_analyst      stalled deals, missing next actions, pipeline risk
    estimate_agent        scope, estimate inputs, allowances, exclusions
    call_recap_agent      calls / meetings -> notes, tasks, next actions
    sales_coach           activity, conversion, response speed, coaching
    compliance_data_agent dedupe, required fields, consent, sensitive data

Same contract as core/gateway/subagents.py: a handler takes a context and
returns a STAGED finding. Agents *propose* SalesActions; the orchestrator
executes the autonomous ones and parks the human-gated ones. An agent can
never send, price, commit or materially change the CRM by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from .gating import classify
from .intake import Assigner, Deduper
from .learning import RepLearning
from .models import (ActionType, AgentFinding, Opportunity, OutboundChannel, PipelineStage,
                     ProjectType, Qualification, SalesAction, SalesRep, TERMINAL_STAGES,
                     missing_required_fields, utcnow)
from .scoring import score_opportunity


# --- Configuration -----------------------------------------------------------
@dataclass(frozen=True)
class SalesConfig:
    response_sla_minutes: int = 15
    # hours since last touch before a stage is "stale"
    cadence_hours: dict[PipelineStage, float] = field(default_factory=lambda: {
        PipelineStage.ASSIGNED: 0.25, PipelineStage.RESPONDED: 24,
        PipelineStage.APPOINTMENT: 24, PipelineStage.SCOPE_ESTIMATE: 72,
        PipelineStage.FOLLOW_UP: 48, PipelineStage.CONTRACT_DEPOSIT: 72,
        PipelineStage.PRODUCTION_HANDOFF: 168,
    })
    # days in a stage before the deal is "stalled"
    stall_days: dict[PipelineStage, float] = field(default_factory=lambda: {
        PipelineStage.LEAD_ENTERED: 0.5, PipelineStage.DEDUPED: 0.5,
        PipelineStage.QUALIFIED: 1, PipelineStage.ASSIGNED: 1,
        PipelineStage.RESPONDED: 5, PipelineStage.APPOINTMENT: 10,
        PipelineStage.SCOPE_ESTIMATE: 10, PipelineStage.FOLLOW_UP: 14,
        PipelineStage.CONTRACT_DEPOSIT: 14, PipelineStage.PRODUCTION_HANDOFF: 21,
    })
    max_unanswered_followups: int = 6
    company_name: str = "NoblePort"
    prohibited_terms: tuple[str, ...] = ()


# --- Context -------------------------------------------------------------------
@dataclass
class AgentContext:
    opp: Opportunity
    now: datetime
    config: SalesConfig
    rep: Optional[SalesRep] = None
    learning: Optional[RepLearning] = None
    deduper: Optional[Deduper] = None
    assigner: Optional[Assigner] = None
    account: dict[str, Any] = field(default_factory=dict)   # prior deals, contact record
    inputs: dict[str, Any] = field(default_factory=dict)    # call_transcript, meeting_notes


Handler = Callable[[AgentContext], Awaitable[AgentFinding]]


@dataclass(frozen=True)
class SalesSubAgent:
    name: str
    responsibility: str
    stages: frozenset[PipelineStage]      # empty = every open stage
    handler: Handler
    needs_input: Optional[str] = None     # only runs when this input is present
    enabled: bool = True


def _action(agent: str, ctx: AgentContext, action_type: ActionType, rationale: str,
            channel: Optional[OutboundChannel] = None, **payload) -> SalesAction:
    a = SalesAction(action_type=action_type, agent=agent, opportunity_id=ctx.opp.opportunity_id,
                    rep_id=ctx.opp.assigned_rep, rationale=rationale, channel=channel,
                    payload={"contact_id": ctx.opp.contact_id, **payload})
    return classify(a)


def _finding(agent: SalesSubAgent, summary: str, actions: list[SalesAction] | None = None,
             blocking: list[str] | None = None, **details) -> AgentFinding:
    return AgentFinding(agent=agent.name, responsibility=agent.responsibility, summary=summary,
                        details=details, actions=actions or [], blocking=blocking or [])


def _hours_since(ts: Optional[datetime], now: datetime) -> Optional[float]:
    return None if ts is None else round((now - ts).total_seconds() / 3600, 2)


# ============================================================================
# 1. lead_router
# ============================================================================
async def lead_router(ctx: AgentContext) -> AgentFinding:
    opp, now = ctx.opp, ctx.now
    actions: list[SalesAction] = []

    # dedupe
    match = ctx.deduper.find(opp) if ctx.deduper else None
    if match:
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.DEDUPE_FLAG,
                               f"matches existing opportunity on {match.matched_on}",
                               duplicate_of=match.opportunity_id, matched_on=match.matched_on))
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.ADD_NOTE,
                               "log repeat inquiry on the existing opportunity",
                               target_opportunity_id=match.opportunity_id,
                               body=f"Repeat inbound via {opp.channel or opp.source.value}: "
                                    f"{opp.description or 'no details'}"))
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.MERGE_CONTACT,
                               "merge duplicate into existing record (human decides)",
                               duplicate_of=match.opportunity_id))
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.ADVANCE_STAGE,
                               "deduped as duplicate", stage=PipelineStage.DEDUPED.value))
        return _finding(LEAD_ROUTER, f"Duplicate of {match.opportunity_id} ({match.matched_on})",
                        actions, duplicate=True)
    actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.ADVANCE_STAGE, "no duplicate found",
                           stage=PipelineStage.DEDUPED.value))

    # qualify + score
    if opp.contact.do_not_contact:
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.SCORE_LEAD,
                               "contact is do-not-contact", score=0, breakdown={},
                               qualification=Qualification.DISQUALIFIED.value, confidence=0.0))
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.CLOSE_DEAL,
                               "do-not-contact flag — close lost pending human review",
                               won=False, reason="do_not_contact"))
        return _finding(LEAD_ROUTER, "Disqualified: do-not-contact", actions, duplicate=False)
    if not (opp.contact.email or opp.contact.phone):
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.SCORE_LEAD,
                               "no way to reach the lead", score=0, breakdown={},
                               qualification=Qualification.DISQUALIFIED.value, confidence=0.0))
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.DATA_QUALITY_FLAG,
                               "no email or phone captured", missing=["email|phone"]))
        return _finding(LEAD_ROUTER, "Disqualified: no contact channel", actions, duplicate=False)

    result = score_opportunity(opp)
    actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.SCORE_LEAD,
                           f"scored {result.score}/100 ({result.tier})", score=result.score,
                           breakdown=result.breakdown, qualification=result.qualification.value,
                           confidence=result.confidence, tier=result.tier,
                           deposit_amount_cents=result.deposit_amount_cents))
    actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.ADVANCE_STAGE, "qualification recorded",
                           stage=PipelineStage.QUALIFIED.value))

    # assign (initial assignment is autonomous; reassignment is gated)
    rep_id = opp.assigned_rep
    if not rep_id and ctx.assigner:
        rep_id = ctx.assigner.choose(opp)
    if rep_id and not opp.assigned_rep:
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.ASSIGN_REP,
                               "territory / fit / capacity / fewest-open", rep_id=rep_id))

    # capture into CRM (after assignment so the deal is created with its owner)
    if not opp.contact_id or not opp.deal_id:
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.CAPTURE_LEAD,
                               "create contact + deal in HubSpot"))
    if rep_id:
        due = now + timedelta(minutes=ctx.config.response_sla_minutes)
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.ADVANCE_STAGE, "routed to rep",
                               stage=PipelineStage.ASSIGNED.value))
        actions.append(_action(LEAD_ROUTER.name, ctx, ActionType.SET_NEXT_ACTION,
                               f"respond within {ctx.config.response_sla_minutes} min",
                               next_action="Immediate response to new lead", due=due.isoformat()))
    summary = f"Scored {result.score} ({result.tier}); " + (f"assigned to {rep_id}" if rep_id
                                                            else "no active rep available")
    return _finding(LEAD_ROUTER, summary, actions, duplicate=False, score=result.score,
                    tier=result.tier, assigned_rep=rep_id)


# ============================================================================
# 2. followup_agent
# ============================================================================
async def followup_agent(ctx: AgentContext) -> AgentFinding:
    opp, now, cfg = ctx.opp, ctx.now, ctx.config
    actions: list[SalesAction] = []
    since = _hours_since(opp.last_touch, now)
    cadence = cfg.cadence_hours.get(opp.pipeline_stage)
    stale = cadence is not None and (since is None or since > cadence)
    overdue = opp.next_action_due is not None and opp.next_action_due < now
    outbound_unanswered = 0
    for t in reversed(opp.touches):
        if t.direction == "inbound":
            break
        outbound_unanswered += 1

    if stale:
        actions.append(_action(FOLLOWUP.name, ctx, ActionType.STALE_FLAG,
                               f"{since if since is not None else '∞'}h since last touch "
                               f"(cadence {cadence}h)", hours_since_touch=since, cadence_hours=cadence))
    if overdue:
        actions.append(_action(FOLLOWUP.name, ctx, ActionType.ADD_TASK,
                               "next action is overdue",
                               subject=f"Callback: {opp.contact.name or opp.opportunity_id} — {opp.next_action}",
                               due=now.isoformat()))
    if not opp.next_action:
        due = now + timedelta(hours=cadence or 24)
        actions.append(_action(FOLLOWUP.name, ctx, ActionType.SET_NEXT_ACTION,
                               "no next action on file",
                               next_action=f"Follow up ({opp.pipeline_stage.value})",
                               due=due.isoformat()))
    if outbound_unanswered >= cfg.max_unanswered_followups:
        actions.append(_action(FOLLOWUP.name, ctx, ActionType.CLOSE_DEAL,
                               f"{outbound_unanswered} unanswered outbound touches",
                               won=False, reason="unresponsive"))
    summary = ("stale" if stale else "on cadence") + ("; overdue" if overdue else "")
    return _finding(FOLLOWUP, summary, actions, stale=stale, overdue=overdue,
                    hours_since_touch=since, outbound_unanswered=outbound_unanswered)


# ============================================================================
# 3. outreach_agent
# ============================================================================
_TEMPLATES: dict[PipelineStage, dict[str, str]] = {
    PipelineStage.ASSIGNED: {
        "sms": "Hi {first}, this is {rep} with {co}. Thanks for reaching out about your {project}"
               "{addr}. When is a good time for a quick call today?",
        "email": "Hi {first},\n\nThanks for contacting {co} about your {project}{addr}. "
                 "I'd like to learn more and set up a site visit. Would today or tomorrow work "
                 "for a short call?\n\n{rep}\n{co}",
        "social": "Hi {first}, thanks for the message about your {project}! I'm {rep} with {co}. "
                  "Can I get a phone number or email to set up a quick call?",
    },
    PipelineStage.RESPONDED: {
        "sms": "Hi {first}, {rep} at {co} — following up on your {project}. "
               "Do you have 15 minutes this week for a site visit?",
        "email": "Hi {first},\n\nFollowing up on your {project}{addr}. Happy to schedule a site "
                 "visit at your convenience — what days work best?\n\n{rep}\n{co}",
        "social": "Hi {first}, following up on your {project} — still happy to help. "
                  "What's the best way to reach you?",
    },
    PipelineStage.APPOINTMENT: {
        "sms": "Hi {first}, confirming our visit{addr}. I'll bring a checklist for the {project}. "
               "Reply if anything changes. — {rep}, {co}",
        "email": "Hi {first},\n\nConfirming our appointment{addr} for the {project}. If you have "
                 "photos, plans or an HOA/permit history, bring them along.\n\n{rep}\n{co}",
        "social": "Hi {first}, looking forward to our visit about the {project}. See you then! — {rep}",
    },
    PipelineStage.SCOPE_ESTIMATE: {
        "sms": "Hi {first}, {rep} here. Your {project} scope is being prepared. "
               "Any must-haves I should include before pricing?",
        "email": "Hi {first},\n\nWe're preparing the scope for your {project}{addr}. Before we "
                 "finalize, are there any must-haves, finishes or exclusions to note?\n\n{rep}\n{co}",
        "social": "Hi {first}, working on your {project} scope now — anything you want included?",
    },
    PipelineStage.FOLLOW_UP: {
        "sms": "Hi {first}, checking in on the {project} proposal. Any questions I can answer? — {rep}",
        "email": "Hi {first},\n\nChecking in on the {project} proposal. Happy to walk through "
                 "any line items or adjust scope.\n\n{rep}\n{co}",
        "social": "Hi {first}, just checking in on the {project} proposal — any questions?",
    },
    PipelineStage.CONTRACT_DEPOSIT: {
        "sms": "Hi {first}, {rep} at {co}. The agreement for your {project} is ready for review. "
               "Let me know if you'd like to go over it together.",
        "email": "Hi {first},\n\nThe agreement for your {project}{addr} is ready for your review. "
                 "I'm glad to walk through it before you sign.\n\n{rep}\n{co}",
        "social": "Hi {first}, the agreement for the {project} is ready — want to review together?",
    },
}


def screen_copy(text: str, prohibited: tuple[str, ...]) -> list[str]:
    low = text.lower()
    return [term for term in prohibited if term.lower() in low]


def _preferred_channel(opp: Opportunity) -> Optional[OutboundChannel]:
    c = opp.contact
    if c.do_not_contact:
        return None
    if c.phone and c.consent_sms:
        return OutboundChannel.SMS
    if c.email and c.consent_email:
        return OutboundChannel.EMAIL
    if opp.source.value == "social":
        return OutboundChannel.SOCIAL
    if c.phone:
        return OutboundChannel.PHONE       # a call, not a text — no TCPA consent needed
    return None


async def outreach_agent(ctx: AgentContext) -> AgentFinding:
    opp, cfg = ctx.opp, ctx.config
    templates = _TEMPLATES.get(opp.pipeline_stage)
    if not templates:
        return _finding(OUTREACH, "no outreach template for this stage")
    first = (opp.contact.name or "there").split(" ")[0]
    fill = {"first": first, "rep": ctx.rep.name if ctx.rep else "the team",
            "co": cfg.company_name, "project": opp.project_type.value.replace("_", " "),
            "addr": f" at {opp.property_address}" if opp.property_address else ""}
    drafts = {ch: tpl.format(**fill) for ch, tpl in templates.items()}
    flagged = {ch: hits for ch, text in drafts.items() if (hits := screen_copy(text, cfg.prohibited_terms))}
    actions = [_action(OUTREACH.name, ctx, ActionType.DRAFT_OUTREACH,
                       f"drafts for {opp.pipeline_stage.value}", drafts=drafts,
                       claims_screened=not flagged, flagged_terms=flagged)]
    channel = _preferred_channel(opp)
    if channel and not flagged:
        body = drafts.get(channel.value, drafts["sms"] if channel is OutboundChannel.PHONE else drafts["email"])
        actions.append(_action(OUTREACH.name, ctx, ActionType.SEND_OUTBOUND,
                               f"send {channel.value} (needs rep approval)", channel=channel,
                               body=body, to=opp.contact.phone if channel in (OutboundChannel.SMS, OutboundChannel.PHONE)
                               else opp.contact.email))
    return _finding(OUTREACH, f"drafted {len(drafts)} channels; preferred={channel.value if channel else None}",
                    actions, preferred_channel=channel.value if channel else None, flagged_terms=flagged)


# ============================================================================
# 4. meeting_prep
# ============================================================================
async def meeting_prep(ctx: AgentContext) -> AgentFinding:
    opp = ctx.opp
    prior = ctx.account.get("prior_deals", [])
    questions = [f"Confirm {f.replace('_', ' ')}" for f in missing_required_fields(opp)]
    if not opp.budget_min and not opp.budget_max:
        questions.append("Budget range and financing")
    if opp.timeline.value == "flexible":
        questions.append("Target start / completion dates")
    questions += ["Decision makers present?", "Permit / HOA history at the property",
                  "Access, parking, pets, working hours"]
    brief = {
        "who": {"name": opp.contact.name, "company": opp.contact.company,
                "phone": opp.contact.phone, "email": opp.contact.email,
                "referred_by": opp.referred_by},
        "property": opp.property_address,
        "project": {"type": opp.project_type.value, "sqft": opp.sqft,
                    "budget": [opp.budget_min, opp.budget_max], "timeline": opp.timeline.value,
                    "description": opp.description},
        "score": {"value": opp.score, "breakdown": opp.score_breakdown,
                  "confidence": opp.confidence, "qualification": opp.qualification.value},
        "history": [f"{t.at.date()} {t.direction} {t.channel}: {t.summary}" for t in opp.touches[-8:]],
        "prior_deals": prior,
        "open_questions": questions,
        "stage": opp.pipeline_stage.value,
    }
    actions = [_action(MEETING_PREP.name, ctx, ActionType.DRAFT_MEETING_BRIEF,
                       "pre-visit brief", brief=brief),
               _action(MEETING_PREP.name, ctx, ActionType.ADD_NOTE, "brief attached to deal",
                       body=f"Meeting brief prepared ({len(questions)} open questions)")]
    return _finding(MEETING_PREP, f"brief with {len(questions)} open questions", actions,
                    open_questions=len(questions), prior_deals=len(prior))


# ============================================================================
# 5. account_research
# ============================================================================
async def account_research(ctx: AgentContext) -> AgentFinding:
    opp = ctx.opp
    prior = ctx.account.get("prior_deals", [])
    contact = ctx.account.get("contact", {})
    sources = ["hubspot.contact", "hubspot.deals"]
    tags = []
    if prior:
        tags.append("repeat_customer")
    if opp.referred_by:
        tags.append("referred")
        sources.append("referral_partner")
    if opp.contact.company:
        tags.append("commercial_account")
    if opp.property_address:
        sources.append("property_record:pending")     # public record lookup is STAGED
    context = {
        "prior_deals": prior, "contact_record": contact, "company": opp.contact.company,
        "referred_by": opp.referred_by, "property_address": opp.property_address,
        "sources_consulted": sources, "suggested_tags": tags,
    }
    actions = [_action(RESEARCH.name, ctx, ActionType.RESEARCH_ACCOUNT, "account context",
                       context=context, tags=tags)]
    return _finding(RESEARCH, f"{len(prior)} prior deal(s); tags={tags}", actions, **context)


# ============================================================================
# 6. pipeline_analyst
# ============================================================================
_LATE = {PipelineStage.SCOPE_ESTIMATE, PipelineStage.FOLLOW_UP,
         PipelineStage.CONTRACT_DEPOSIT, PipelineStage.PRODUCTION_HANDOFF}


async def pipeline_analyst(ctx: AgentContext) -> AgentFinding:
    opp, now, cfg = ctx.opp, ctx.now, ctx.config
    reasons: list[str] = []
    risk = 0
    if not opp.next_action:
        reasons.append("missing_next_action"); risk += 30
    if opp.next_action_due and opp.next_action_due < now:
        reasons.append("next_action_overdue"); risk += 20
    limit = cfg.stall_days.get(opp.pipeline_stage)
    age_days = opp.stage_age(now).total_seconds() / 86400
    if limit is not None and age_days > limit:
        reasons.append(f"stalled_in_stage:{round(age_days, 1)}d>{limit}d"); risk += 25
    if not opp.assigned_rep and opp.pipeline_stage not in (PipelineStage.LEAD_ENTERED, PipelineStage.DEDUPED):
        reasons.append("unassigned"); risk += 25
    if opp.pipeline_stage in _LATE and opp.confidence < 0.4:
        reasons.append("low_confidence_late_stage"); risk += 15
    if opp.pipeline_stage in (PipelineStage.CONTRACT_DEPOSIT, PipelineStage.PRODUCTION_HANDOFF) and not opp.deal_amount:
        reasons.append("no_deal_amount"); risk += 20
    risk = min(100, risk)
    value = opp.deal_amount or ((opp.budget_min or 0) + (opp.budget_max or 0)) // 2
    priority = round(0.5 * opp.score + 0.3 * risk + (20 if value >= 100_000 else 10 if value >= 25_000 else 0), 1)
    actions = [_action(ANALYST.name, ctx, ActionType.PRIORITIZE, "; ".join(reasons) or "healthy",
                       risk=risk, priority=priority, reasons=reasons, stage_age_days=round(age_days, 2))]
    if "missing_next_action" in reasons:
        actions.append(_action(ANALYST.name, ctx, ActionType.ADD_TASK, "no next action on deal",
                               subject=f"Set next action: {opp.contact.name or opp.opportunity_id}",
                               due=now.isoformat()))
    return _finding(ANALYST, f"risk={risk} priority={priority}", actions, risk=risk,
                    priority=priority, reasons=reasons)


# ============================================================================
# 7. estimate_agent
# ============================================================================
SCOPE_TEMPLATES: dict[ProjectType, list[str]] = {
    ProjectType.ROOF: ["Tear-off and disposal", "Ice & water shield / underlayment", "Shingles + ridge vent",
                       "Flashing, drip edge, pipe boots", "Gutter check", "Site cleanup"],
    ProjectType.DECK: ["Footings / permits", "Framing + ledger", "Decking + fasteners", "Railings + stairs",
                       "Finish / stain", "Inspection"],
    ProjectType.SIDING: ["Removal + disposal", "House wrap + flashing", "Siding install", "Trim + corners",
                         "Caulk + paint touch-up"],
    ProjectType.FOUNDATION: ["Excavation", "Footings + walls", "Waterproofing + drainage", "Backfill + grading"],
    ProjectType.ADDITION: ["Design + permits", "Foundation", "Framing + roof tie-in", "Windows/doors",
                           "MEP rough-in", "Insulation + drywall", "Finishes", "Inspections"],
    ProjectType.RENOVATION: ["Demo + disposal", "Framing changes", "MEP rough-in", "Drywall + paint",
                             "Finishes (floor/tile/cabinets)", "Punch list"],
    ProjectType.NEW_CONSTRUCTION: ["Site work + permits", "Foundation", "Framing", "Roofing + envelope",
                                   "MEP", "Insulation + drywall", "Interior finishes", "Exterior + landscape", "CO"],
    ProjectType.COMMERCIAL: ["Design + permits", "Demo", "Structural / MEP", "Fire / life safety",
                             "Finishes", "Inspections + CO"],
    ProjectType.ADU: ["Zoning + permits", "Foundation", "Shell", "MEP + utilities", "Finishes", "CO"],
    ProjectType.HVAC: ["Load calc", "Equipment", "Ductwork", "Electrical", "Commissioning"],
    ProjectType.ELECTRICAL: ["Panel / service", "Rough wiring", "Devices + fixtures", "Inspection"],
    ProjectType.PLUMBING: ["Rough plumbing", "Fixtures", "Water heater", "Inspection"],
    ProjectType.PAINTING: ["Prep + repairs", "Prime", "Finish coats", "Cleanup"],
    ProjectType.DEMOLITION: ["Permits + utility disconnect", "Demo", "Haul + disposal", "Site grading"],
}
DEFAULT_EXCLUSIONS = ["Permit fees (billed at cost)", "Hazardous material abatement", "Unforeseen conditions",
                      "Owner-supplied materials", "Engineering stamps", "Utility company fees",
                      "Landscaping restoration beyond work area"]
DEFAULT_ALLOWANCES = {"fixtures": "owner selection allowance", "finishes": "standard-grade allowance",
                      "contingency": "10% of hard cost"}


async def estimate_agent(ctx: AgentContext) -> AgentFinding:
    opp = ctx.opp
    lines = SCOPE_TEMPLATES.get(opp.project_type, ["Scope to be defined at site visit"])
    budget_band = [opp.budget_min, opp.budget_max]
    assumptions = [f"Project type: {opp.project_type.value}",
                   f"Square footage: {opp.sqft or 'TBD'}",
                   f"Timeline: {opp.timeline.value}",
                   "Pricing subject to site verification and licensed review"]
    inputs = {"scope_lines": lines, "allowances": DEFAULT_ALLOWANCES, "exclusions": DEFAULT_EXCLUSIONS,
              "assumptions": assumptions, "budget_band": budget_band, "sqft": opp.sqft,
              "description": opp.description, "customer_must_haves": ctx.inputs.get("must_haves", [])}
    actions = [_action(ESTIMATE.name, ctx, ActionType.DRAFT_ESTIMATE_INPUTS,
                       "scope + allowances + exclusions", estimate_inputs=inputs),
               _action(ESTIMATE.name, ctx, ActionType.FINAL_PRICING,
                       "final pricing requires estimator sign-off", budget_band=budget_band,
                       scope_lines=lines),
               _action(ESTIMATE.name, ctx, ActionType.SEND_PROPOSAL,
                       "proposal goes out only after pricing is signed", depends_on="final_pricing")]
    return _finding(ESTIMATE, f"{len(lines)} scope lines, {len(DEFAULT_EXCLUSIONS)} exclusions",
                    actions, scope_lines=len(lines))


# ============================================================================
# 8. call_recap_agent
# ============================================================================
_ACTION_CUES = ("follow up", "follow-up", "send ", "call back", "callback", "schedule", "todo",
                "action:", "need to", "will ", "next step", "get back", "email ", "text ")
_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{2})?)\s*(k)?", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def _relative_due(text: str, now: datetime) -> datetime:
    low = text.lower()
    if "today" in low: return now + timedelta(hours=4)
    if "tomorrow" in low: return now + timedelta(days=1)
    if "next week" in low: return now + timedelta(days=7)
    if "friday" in low: return now + timedelta(days=(4 - now.weekday()) % 7 or 7)
    if "monday" in low: return now + timedelta(days=(0 - now.weekday()) % 7 or 7)
    return now + timedelta(days=2)


async def call_recap_agent(ctx: AgentContext) -> AgentFinding:
    opp, now = ctx.opp, ctx.now
    text = ctx.inputs.get("call_transcript") or ctx.inputs.get("meeting_notes") or ""
    sentences = [s.strip() for s in _SENT.split(text) if s.strip()]
    summary = " ".join(sentences[:2])[:400] or "No transcript content"
    items = [s for s in sentences if any(cue in s.lower() for cue in _ACTION_CUES)][:5]
    actions = [_action(RECAP.name, ctx, ActionType.ADD_NOTE, "call recap",
                       body=f"Call recap: {summary}", action_items=items)]
    for item in items:
        actions.append(_action(RECAP.name, ctx, ActionType.ADD_TASK, "action item from call",
                               subject=item[:120], due=_relative_due(item, now).isoformat()))
    if items:
        actions.append(_action(RECAP.name, ctx, ActionType.SET_NEXT_ACTION, "first action item",
                               next_action=items[0][:120], due=_relative_due(items[0], now).isoformat()))
    amounts = []
    for m in _MONEY.finditer(text):
        val = float(m.group(1).replace(",", ""))
        amounts.append(int(val * 1000) if m.group(2) else int(val))
    if amounts:
        actions.append(_action(RECAP.name, ctx, ActionType.CHANGE_DEAL_AMOUNT,
                               "dollar figure discussed on call — human confirms",
                               amount=max(amounts), all_amounts=amounts))
    # channel activity is autonomous telemetry; the recap itself is the touch
    return _finding(RECAP, f"{len(items)} action item(s), {len(amounts)} amount(s)", actions,
                    recap=summary, action_items=items, amounts=amounts,
                    touch_channel=ctx.inputs.get("channel", "phone"))


# ============================================================================
# 9. sales_coach
# ============================================================================
async def sales_coach(ctx: AgentContext) -> AgentFinding:
    lr, cfg = ctx.learning, ctx.config
    if lr is None or (lr.leads_assigned == 0 and lr.closed == 0):
        return _finding(COACH, "insufficient rep data yet")
    insights: list[str] = []
    med = lr.median_response_minutes()
    if med is not None and med > cfg.response_sla_minutes:
        insights.append(f"median first response {med} min vs {cfg.response_sla_minutes} min SLA")
    rr = lr.response_rate()
    if rr is not None and rr < 0.8:
        insights.append(f"only {int(rr * 100)}% of assigned leads received a response")
    wr = lr.win_rate()
    if wr is not None and lr.closed >= 5 and wr < 0.2:
        insights.append(f"win rate {int(wr * 100)}% over {lr.closed} closed — review qualification/pricing")
    if lr.appointments and lr.estimates and lr.estimates / lr.appointments < 0.5:
        insights.append("fewer than half of appointments turn into estimates")
    if lr.activity and max(lr.activity.values()) == sum(lr.activity.values()) and sum(lr.activity.values()) >= 10:
        insights.append(f"all activity on one channel ({max(lr.activity, key=lr.activity.get)}) — diversify")
    best = lr.best_source()
    if best:
        insights.append(f"best-converting source: {best}")
    actions = [_action(COACH.name, ctx, ActionType.COACHING_INSIGHT, "rep coaching (internal)",
                       rep_id=lr.rep_id, insights=insights,
                       metrics={"median_response_minutes": med, "response_rate": rr,
                                "win_rate": wr, "won": lr.won, "lost": lr.lost,
                                "leads_assigned": lr.leads_assigned})]
    return _finding(COACH, f"{len(insights)} insight(s) for {lr.rep_id}", actions, insights=insights)


# ============================================================================
# 10. compliance_data_agent
# ============================================================================
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")
_DOB = re.compile(r"\b(?:dob|date of birth)\b", re.I)


def detect_sensitive(text: str) -> list[str]:
    found = []
    if _SSN.search(text): found.append("ssn")
    if _CARD.search(text): found.append("card_number")
    if _DOB.search(text): found.append("dob")
    return found


async def compliance_data_agent(ctx: AgentContext) -> AgentFinding:
    opp = ctx.opp
    actions: list[SalesAction] = []
    blocking: list[str] = []
    missing = missing_required_fields(opp)
    if missing:
        actions.append(_action(COMPLIANCE.name, ctx, ActionType.DATA_QUALITY_FLAG,
                               f"missing required fields for {opp.pipeline_stage.value}", missing=missing))
    dup = ctx.deduper.find(opp) if ctx.deduper else None
    if dup and opp.duplicate_of != dup.opportunity_id:
        actions.append(_action(COMPLIANCE.name, ctx, ActionType.DEDUPE_FLAG,
                               f"possible duplicate ({dup.matched_on})",
                               duplicate_of=dup.opportunity_id, matched_on=dup.matched_on))
    c = opp.contact
    if c.do_not_contact:
        blocking += ["sms", "email", "social", "phone"]
    else:
        if not c.consent_sms:
            blocking.append("sms")
        if not c.consent_email:
            blocking.append("email")
    corpus = " ".join(filter(None, [opp.description, ctx.inputs.get("call_transcript"),
                                    ctx.inputs.get("meeting_notes")]))
    sensitive = detect_sensitive(corpus)
    if sensitive:
        actions.append(_action(COMPLIANCE.name, ctx, ActionType.DATA_QUALITY_FLAG,
                               "sensitive data present in free text", sensitive=sensitive))
        actions.append(_action(COMPLIANCE.name, ctx, ActionType.REDACT_SENSITIVE,
                               "redact sensitive data from CRM free text", fields=sensitive))
    summary = (f"{len(missing)} missing field(s); consent-blocked={sorted(set(blocking))}; "
               f"sensitive={sensitive}")
    return _finding(COMPLIANCE, summary, actions, blocking=sorted(set(blocking)), missing=missing,
                    sensitive=sensitive, duplicate_of=dup.opportunity_id if dup else None)


# ============================================================================
# Registry
# ============================================================================
_OPEN = frozenset(s for s in PipelineStage if s not in TERMINAL_STAGES)
_ROUTING = frozenset({PipelineStage.LEAD_ENTERED, PipelineStage.DEDUPED, PipelineStage.QUALIFIED})
_ACTIVE = _OPEN - _ROUTING
_OUTREACH = frozenset(_TEMPLATES)

LEAD_ROUTER = SalesSubAgent("lead_router", "capture, dedupe, qualify, score, assign, stage", _ROUTING, lead_router)
FOLLOWUP = SalesSubAgent("followup_agent", "callbacks, stale leads, follow-up cadence", _ACTIVE, followup_agent)
OUTREACH = SalesSubAgent("outreach_agent", "draft SMS / email / social outreach", _OUTREACH, outreach_agent)
MEETING_PREP = SalesSubAgent("meeting_prep", "pre-call / site-visit briefs",
                             frozenset({PipelineStage.APPOINTMENT, PipelineStage.SCOPE_ESTIMATE}), meeting_prep)
RESEARCH = SalesSubAgent("account_research", "homeowner / company / property / account context",
                         frozenset({PipelineStage.LEAD_ENTERED, PipelineStage.DEDUPED, PipelineStage.QUALIFIED,
                                    PipelineStage.ASSIGNED, PipelineStage.RESPONDED, PipelineStage.APPOINTMENT}),
                         account_research)
ANALYST = SalesSubAgent("pipeline_analyst", "stalled deals, missing next actions, pipeline risk", _ACTIVE, pipeline_analyst)
ESTIMATE = SalesSubAgent("estimate_agent", "scope, estimate inputs, allowances, exclusions",
                         frozenset({PipelineStage.SCOPE_ESTIMATE}), estimate_agent)
RECAP = SalesSubAgent("call_recap_agent", "calls / meetings -> notes, tasks, next actions", _OPEN,
                      call_recap_agent, needs_input="call_transcript|meeting_notes")
COACH = SalesSubAgent("sales_coach", "rep activity, conversion, response speed, coaching", _OPEN, sales_coach)
COMPLIANCE = SalesSubAgent("compliance_data_agent", "dedupe, required fields, consent, sensitive data",
                           frozenset(PipelineStage), compliance_data_agent)

SALES_AGENTS: tuple[SalesSubAgent, ...] = (
    LEAD_ROUTER, FOLLOWUP, OUTREACH, MEETING_PREP, RESEARCH,
    ANALYST, ESTIMATE, RECAP, COACH, COMPLIANCE,
)
AGENT_NAMES = frozenset(a.name for a in SALES_AGENTS)


def select(stage: PipelineStage, inputs: Optional[dict[str, Any]] = None) -> tuple[SalesSubAgent, ...]:
    """Which sub-agents serve an opportunity at this stage (registry order)."""
    inputs = inputs or {}
    chosen = []
    for a in SALES_AGENTS:
        if not a.enabled:
            continue
        if a.stages and stage not in a.stages:
            continue
        if a.needs_input and not any(k in inputs for k in a.needs_input.split("|")):
            continue
        chosen.append(a)
    return tuple(chosen)
