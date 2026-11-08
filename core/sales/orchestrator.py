"""Sales Orchestrator — one command layer above the reps.

    intake(raw)            any channel -> normalize -> run the routing agents
    run(opp, inputs)       select agents for the stage, gather findings,
                           execute autonomous actions, PARK gated ones
    sweep()                monitoring pass over every open opportunity
    approve / reject       the human gate (signature required)
    record_outcome(...)    closed won / lost -> rep learning loop
    command_view()         consolidated management view

Autonomy is enforced twice: gating.classify stamps every proposal with the
policy gate (agents cannot under-declare), and the CRM layer refuses material
writes without a signature. A gated action becomes real only via approve()
with a human signature, after which execute_approved() applies it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field

from . import agents as A
from .approvals import ApprovalQueue
from .crm import CrmClient, deal_properties
from .gating import requires_human, to_envelope
from .intake import Assigner, Deduper, normalize
from .learning import LearningStore
from .models import (AUTONOMOUS_STAGES, ActionStatus, ActionType, AgentFinding, Gate,
                     LeadSource, Opportunity, PipelineStage, ProjectType, Qualification,
                     RawLead, SalesAction, SalesRep, TERMINAL_STAGES, utcnow)

log = logging.getLogger("nobleport.sales.orchestrator")


class LoopResult(BaseModel):
    opportunity_id: str
    stage_before: PipelineStage
    stage_after: PipelineStage
    agents: list[str]
    findings: list[AgentFinding]
    executed: list[SalesAction]
    parked: list[SalesAction]
    vetoed: list[SalesAction] = Field(default_factory=list)
    truth_label: str = "STAGED"


class SalesOrchestrator:
    def __init__(self, crm: CrmClient, reps: Iterable[SalesRep], *,
                 config: Optional[A.SalesConfig] = None,
                 clock=utcnow, prohibited_terms: tuple[str, ...] = ()):
        self.crm = crm
        self.reps: dict[str, SalesRep] = {r.rep_id: r for r in reps}
        cfg = config or A.SalesConfig()
        if prohibited_terms and not cfg.prohibited_terms:
            cfg = A.SalesConfig(**{**cfg.__dict__, "prohibited_terms": tuple(prohibited_terms)})
        self.config = cfg
        self.clock = clock
        self.opportunities: dict[str, Opportunity] = {}
        self.deduper = Deduper()
        self.assigner = Assigner.from_reps(self.reps.values())
        self.approvals = ApprovalQueue()
        self.learning = LearningStore()
        self.history: list[LoopResult] = []

    # ------------------------------------------------------------------ intake
    async def intake(self, raw: RawLead) -> LoopResult:
        opp = normalize(raw)
        self.opportunities[opp.opportunity_id] = opp
        result = await self.run(opp)
        self.deduper.index(opp)
        return result

    # ------------------------------------------------------------------ loop
    async def run(self, opp: Opportunity, inputs: Optional[dict[str, Any]] = None) -> LoopResult:
        now = self.clock()
        inputs = inputs or {}
        before = opp.pipeline_stage
        selected = A.select(opp.pipeline_stage, inputs)
        ctx = A.AgentContext(
            opp=opp, now=now, config=self.config,
            rep=self.reps.get(opp.assigned_rep) if opp.assigned_rep else None,
            learning=self.learning.get(opp.assigned_rep) if opp.assigned_rep else None,
            deduper=self.deduper, assigner=self.assigner,
            account=await self._account_context(opp), inputs=inputs,
        )
        findings = list(await asyncio.gather(*(a.handler(ctx) for a in selected)))
        vetoes = {ch for f in findings for ch in f.blocking}

        executed: list[SalesAction] = []
        parked: list[SalesAction] = []
        vetoed: list[SalesAction] = []
        for finding in findings:
            for action in finding.actions:
                if action.channel and action.channel.value in vetoes:
                    action.status = ActionStatus.REJECTED
                    action.rejected_reason = f"compliance_data_agent: no consent for {action.channel.value}"
                    vetoed.append(action)
                    continue
                if requires_human(action):
                    parked.append(self.approvals.park(action))
                    continue
                try:
                    await self._apply(opp, action, now)
                    action.status = ActionStatus.EXECUTED
                    executed.append(action)
                except Exception as exc:  # an autonomous failure never blocks the loop
                    log.warning("action %s failed: %s", action.action_type.value, exc)
                    action.status = ActionStatus.FAILED
                    action.rejected_reason = str(exc)
                    vetoed.append(action)

        # rep learning: recap of a call is rep activity
        if opp.assigned_rep and any(f.agent == "call_recap_agent" for f in findings):
            self.learning.get(opp.assigned_rep).on_activity(inputs.get("channel", "phone"))

        result = LoopResult(opportunity_id=opp.opportunity_id, stage_before=before,
                            stage_after=opp.pipeline_stage, agents=[a.name for a in selected],
                            findings=findings, executed=executed, parked=parked, vetoed=vetoed)
        self.history.append(result)
        return result

    async def sweep(self) -> list[LoopResult]:
        """Monitoring pass: every open opportunity, no new inputs."""
        out = []
        for opp in list(self.opportunities.values()):
            if opp.is_open and opp.pipeline_stage not in AUTONOMOUS_STAGES - {PipelineStage.ASSIGNED}:
                out.append(await self.run(opp))
        return out

    # ------------------------------------------------------------------ human gate
    def pending(self, *, rep_id: Optional[str] = None) -> list[SalesAction]:
        return self.approvals.pending(rep_id=rep_id)

    async def approve(self, action_id: str, *, signature: str, approver: str) -> SalesAction:
        action = self.approvals.approve(action_id, signature=signature, approver=approver)
        return await self.execute_approved(action)

    def reject(self, action_id: str, *, reason: str, by: str) -> SalesAction:
        return self.approvals.reject(action_id, reason=reason, by=by)

    async def execute_approved(self, action: SalesAction) -> SalesAction:
        if action.status is not ActionStatus.APPROVED or not action.human_signature:
            raise PermissionError("gated action requires an approved status and a human signature")
        opp = self.opportunities.get(action.opportunity_id or "")
        if opp is None:
            raise KeyError(f"unknown opportunity {action.opportunity_id}")
        now = self.clock()
        await self._apply(opp, action, now)
        self.approvals.mark_executed(action.action_id, now)
        # the envelope that would carry this through the MCP gateway (audited there)
        action.payload["gateway_envelope"] = to_envelope(action).model_dump(mode="json")
        return action

    # ------------------------------------------------------------------ outcomes
    async def record_outcome(self, opportunity_id: str, *, won: bool, reason: Optional[str],
                             signature: str, by: str, amount: Optional[int] = None) -> Opportunity:
        """Closed won / lost is a material CRM change: it needs a signature and it
        feeds the rep learning loop."""
        opp = self.opportunities[opportunity_id]
        action = SalesAction(action_type=ActionType.CLOSE_DEAL, agent="human", rep_id=opp.assigned_rep,
                             opportunity_id=opportunity_id, rationale=reason or "",
                             payload={"won": won, "reason": reason, "amount": amount})
        self.approvals.park(action)
        await self.approve(action.action_id, signature=signature, approver=by)
        return opp

    # ------------------------------------------------------------------ command layer
    def command_view(self) -> dict[str, Any]:
        opps = list(self.opportunities.values())
        open_opps = [o for o in opps if o.is_open]
        now = self.clock()
        by_stage: dict[str, int] = {}
        by_source: dict[str, int] = {}
        for o in opps:
            by_stage[o.pipeline_stage.value] = by_stage.get(o.pipeline_stage.value, 0) + 1
            by_source[o.source.value] = by_source.get(o.source.value, 0) + 1
        per_rep = []
        for rep in sorted(self.reps.values(), key=lambda r: r.rep_id):
            mine = [o for o in open_opps if o.assigned_rep == rep.rep_id]
            lr = self.learning.get(rep.rep_id)
            per_rep.append({
                "rep_id": rep.rep_id, "name": rep.name, "open": len(mine),
                "capacity": rep.capacity,
                "overdue": sum(1 for o in mine if o.next_action_due and o.next_action_due < now),
                "missing_next_action": sum(1 for o in mine if not o.next_action),
                "pending_approvals": len(self.approvals.pending(rep_id=rep.rep_id)),
                "win_rate": lr.win_rate(), "median_response_minutes": lr.median_response_minutes(),
            })
        latest = {r.opportunity_id: r for r in self.history}
        risks = []
        for o in open_opps:
            r = latest.get(o.opportunity_id)
            if not r:
                continue
            for f in r.findings:
                if f.agent == "pipeline_analyst" and f.details.get("risk", 0) >= 40:
                    risks.append({"opportunity_id": o.opportunity_id, "rep_id": o.assigned_rep,
                                  "stage": o.pipeline_stage.value, "risk": f.details["risk"],
                                  "reasons": f.details.get("reasons", [])})
        risks.sort(key=lambda x: -x["risk"])
        return {
            "truth_label": "STAGED",
            "as_of": now.isoformat(),
            "opportunities": {"total": len(opps), "open": len(open_opps),
                              "by_stage": by_stage, "by_source": by_source},
            "unassigned": sum(1 for o in open_opps if not o.assigned_rep
                              and o.pipeline_stage not in (PipelineStage.LEAD_ENTERED, PipelineStage.DEDUPED)),
            "pending_approvals": {
                "total": len(self.approvals.pending()),
                "by_type": _count(a.action_type.value for a in self.approvals.pending()),
            },
            "pipeline_risk": risks[:25],
            "reps": per_rep,
            "team": self.learning.team_summary(),
        }

    # ------------------------------------------------------------------ internals
    async def _account_context(self, opp: Opportunity) -> dict[str, Any]:
        ctx: dict[str, Any] = {"prior_deals": [], "contact": {}}
        cid = opp.contact_id or await self.crm.find_contact(email=opp.contact.email, phone=opp.contact.phone)
        if cid:
            ctx["contact"] = await self.crm.get_contact(cid)
            ctx["prior_deals"] = [d for d in await self.crm.deals_for_contact(cid)
                                  if d.get("id") != opp.deal_id]
        return ctx

    async def _apply(self, opp: Opportunity, action: SalesAction, now: datetime) -> None:
        p = action.payload
        t = action.action_type
        crm = self.crm
        rep = self.reps.get(opp.assigned_rep or "")
        owner = rep.hubspot_owner_id if rep else None

        # ---- autonomous ------------------------------------------------
        if t is ActionType.SCORE_LEAD:
            opp.score = int(p.get("score", 0)); opp.score_breakdown = dict(p.get("breakdown", {}))
            opp.confidence = float(p.get("confidence", 0.0))
            opp.qualification = Qualification(p.get("qualification", Qualification.UNQUALIFIED.value))
        elif t is ActionType.DEDUPE_FLAG:
            if action.agent == "lead_router":
                opp.duplicate_of = p["duplicate_of"]
                if "duplicate" not in opp.tags:
                    opp.tags.append("duplicate")
        elif t is ActionType.CAPTURE_LEAD:
            if not opp.contact_id:
                opp.contact_id = await crm.upsert_contact(opp.contact)
                opp.contact.contact_id = opp.contact_id
            if not opp.deal_id:
                opp.deal_id = await crm.create_deal(opp, owner_id=owner)
        elif t is ActionType.ASSIGN_REP:
            if opp.assigned_rep:      # initial assignment only; reassign is gated
                return
            opp.assigned_rep = p["rep_id"]
            self.assigner.note_assignment(p["rep_id"])
            self.learning.get(p["rep_id"]).on_assigned()
            action.rep_id = p["rep_id"]
            rep = self.reps.get(p["rep_id"])
            if opp.deal_id and rep and rep.hubspot_owner_id:
                # first owner on an ownerless deal is not material; a *change*
                # of owner is (REASSIGN_REP, gated).
                await crm.update_deal(opp.deal_id, {"hubspot_owner_id": rep.hubspot_owner_id})
        elif t is ActionType.ADVANCE_STAGE:
            stage = PipelineStage(p["stage"])
            if stage not in AUTONOMOUS_STAGES:
                raise PermissionError(f"agents may not advance to {stage.value} autonomously")
            if opp.pipeline_stage in TERMINAL_STAGES:
                return
            opp.set_stage(stage, now)
            if opp.deal_id:
                await crm.update_deal(opp.deal_id, {"dealstage": stage.value})
        elif t is ActionType.SET_NEXT_ACTION:
            opp.next_action = p["next_action"]
            opp.next_action_due = datetime.fromisoformat(p["due"])
            if opp.deal_id:
                await crm.update_deal(opp.deal_id, {"np_next_action": opp.next_action,
                                                    "np_next_action_due": opp.next_action_due})
        elif t is ActionType.ADD_NOTE:
            target = self.opportunities.get(p.get("target_opportunity_id") or "", opp)
            await crm.add_note(deal_id=target.deal_id, contact_id=target.contact_id,
                               body=p["body"], by=action.agent)
        elif t is ActionType.ADD_TASK:
            await crm.create_task(deal_id=opp.deal_id, subject=p["subject"],
                                  due=datetime.fromisoformat(p["due"]), owner_id=owner, by=action.agent)
        elif t is ActionType.RESEARCH_ACCOUNT:
            for tag in p.get("tags", []):
                if tag not in opp.tags:
                    opp.tags.append(tag)
        elif t in (ActionType.DRAFT_OUTREACH, ActionType.DRAFT_MEETING_BRIEF,
                   ActionType.DRAFT_ESTIMATE_INPUTS, ActionType.PRIORITIZE, ActionType.STALE_FLAG,
                   ActionType.DATA_QUALITY_FLAG, ActionType.COACHING_INSIGHT):
            pass                                   # drafts / flags are surfaced, not written

        # ---- human-gated (only reachable via execute_approved) --------------
        elif t is ActionType.SEND_OUTBOUND:
            channel = action.channel.value if action.channel else p.get("channel", "email")
            await crm.log_outbound(deal_id=opp.deal_id, channel=channel, body=p.get("body", ""),
                                   by=action.approved_by or "human", human_signature=action.human_signature)
            opp.touch("outbound", channel, opp.assigned_rep, f"{channel} sent (approved)", at=now)
            if opp.pipeline_stage is PipelineStage.ASSIGNED:
                assigned_at = opp.stage_entered_at
                self._advance_signed(opp, PipelineStage.RESPONDED, now)
                if opp.assigned_rep:
                    self.learning.get(opp.assigned_rep).on_responded(assigned_at, now, channel)
                await self._push_stage(opp, action.human_signature)
            elif opp.assigned_rep:
                self.learning.get(opp.assigned_rep).on_activity(channel)
        elif t is ActionType.FINAL_PRICING:
            if p.get("amount") is not None:
                opp.deal_amount = int(p["amount"])
                if opp.deal_id:
                    await crm.update_deal(opp.deal_id, {"amount": opp.deal_amount},
                                          human_signature=action.human_signature)
            if opp.assigned_rep:
                self.learning.get(opp.assigned_rep).on_estimate()
        elif t is ActionType.SEND_PROPOSAL:
            await crm.log_outbound(deal_id=opp.deal_id, channel=p.get("channel", "email"),
                                   body=p.get("body", "proposal"), by=action.approved_by or "human",
                                   human_signature=action.human_signature)
            opp.touch("outbound", "email", opp.assigned_rep, "proposal sent (approved)", at=now)
            if opp.pipeline_stage is PipelineStage.SCOPE_ESTIMATE:
                self._advance_signed(opp, PipelineStage.FOLLOW_UP, now)
                await self._push_stage(opp, action.human_signature)
        elif t in (ActionType.CONTRACT_COMMITMENT, ActionType.DEPOSIT_REQUEST):
            if p.get("amount") is not None:
                opp.deal_amount = int(p["amount"])
            self._advance_signed(opp, PipelineStage.CONTRACT_DEPOSIT, now)
            await self._push_stage(opp, action.human_signature, amount=opp.deal_amount)
        elif t is ActionType.PRODUCTION_HANDOFF:
            self._advance_signed(opp, PipelineStage.PRODUCTION_HANDOFF, now)
            await self._push_stage(opp, action.human_signature)
        elif t is ActionType.REASSIGN_REP:
            old = opp.assigned_rep
            opp.assigned_rep = p["rep_id"]
            self.assigner.note_close(old); self.assigner.note_assignment(p["rep_id"])
            new_rep = self.reps.get(p["rep_id"])
            if opp.deal_id and new_rep and new_rep.hubspot_owner_id:
                await crm.update_deal(opp.deal_id, {"hubspot_owner_id": new_rep.hubspot_owner_id},
                                      human_signature=action.human_signature)
        elif t is ActionType.CHANGE_DEAL_AMOUNT:
            opp.deal_amount = int(p["amount"])
            if opp.deal_id:
                await crm.update_deal(opp.deal_id, {"amount": opp.deal_amount},
                                      human_signature=action.human_signature)
        elif t is ActionType.CLOSE_DEAL:
            won = bool(p.get("won"))
            if p.get("amount") is not None:
                opp.deal_amount = int(p["amount"])
            opp.lost_reason = None if won else (p.get("reason") or "unspecified")
            self._advance_signed(opp, PipelineStage.CLOSED_WON if won else PipelineStage.CLOSED_LOST, now)
            props: dict[str, Any] = {"dealstage": opp.pipeline_stage.value}
            if opp.lost_reason:
                props["closed_lost_reason"] = opp.lost_reason
            if opp.deal_id:
                await crm.update_deal(opp.deal_id, props, human_signature=action.human_signature)
            self.assigner.note_close(opp.assigned_rep)
            self.deduper.index(opp)
            if opp.assigned_rep:
                self.learning.get(opp.assigned_rep).on_closed(
                    won=won, source=opp.source, project_type=opp.project_type,
                    reason=opp.lost_reason, at=now)
        elif t in (ActionType.MERGE_CONTACT, ActionType.DELETE_RECORD, ActionType.REDACT_SENSITIVE):
            await crm.add_note(deal_id=opp.deal_id, contact_id=opp.contact_id,
                               body=f"{t.value} approved by {action.approved_by}: {p}",
                               by=action.agent)
        else:  # pragma: no cover
            raise ValueError(f"unhandled action {t}")

    def _advance_signed(self, opp: Opportunity, stage: PipelineStage, now: datetime) -> None:
        if opp.pipeline_stage in TERMINAL_STAGES:
            raise PermissionError("opportunity is closed")
        opp.set_stage(stage, now)
        if stage is PipelineStage.APPOINTMENT and opp.assigned_rep:
            self.learning.get(opp.assigned_rep).on_appointment()

    async def _push_stage(self, opp: Opportunity, signature: Optional[str], **extra: Any) -> None:
        if not opp.deal_id:
            return
        props: dict[str, Any] = {"dealstage": opp.pipeline_stage.value}
        props.update({k: v for k, v in extra.items() if v is not None})
        await self.crm.update_deal(opp.deal_id, props, human_signature=signature)

    # rep-driven stage moves that need no agent (appointment booked by the rep,
    # inbound reply received). Still recorded so the loop and learning stay true.
    def book_appointment(self, opportunity_id: str, when: datetime, by: str) -> Opportunity:
        opp = self.opportunities[opportunity_id]
        self._advance_signed(opp, PipelineStage.APPOINTMENT, self.clock())
        opp.next_action, opp.next_action_due = "Site visit / call", when
        opp.touch("outbound", "calendar", by, f"appointment booked for {when.isoformat()}", at=self.clock())
        return opp

    def start_scope(self, opportunity_id: str, by: str) -> Opportunity:
        opp = self.opportunities[opportunity_id]
        self._advance_signed(opp, PipelineStage.SCOPE_ESTIMATE, self.clock())
        opp.next_action, opp.next_action_due = "Prepare scope and estimate", None
        opp.touch("inbound", "site_visit", by, "site visit completed", at=self.clock())
        return opp

    def inbound_reply(self, opportunity_id: str, channel: str, summary: str) -> Opportunity:
        opp = self.opportunities[opportunity_id]
        opp.touch("inbound", channel, None, summary, at=self.clock())
        return opp


def _count(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out
