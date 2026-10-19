import pytest

from core.sales import agents as A
from core.sales.intake import Assigner, Deduper, normalize
from core.sales.learning import RepLearning
from core.sales.models import ActionType, Gate, PipelineStage, SalesRep
from core.sales.tests.conftest import T0, raw_lead

EXPECTED = {
    "lead_router", "followup_agent", "outreach_agent", "meeting_prep", "account_research",
    "pipeline_analyst", "estimate_agent", "call_recap_agent", "sales_coach", "compliance_data_agent",
}


def ctx(opp, **kw):
    base = dict(opp=opp, now=T0, config=A.SalesConfig(prohibited_terms=("guaranteed returns",)),
                rep=SalesRep(rep_id="rep_amy", name="Amy Chen"), deduper=Deduper(),
                assigner=Assigner.from_reps([SalesRep(rep_id="rep_amy", name="Amy Chen")]))
    base.update(kw)
    return A.AgentContext(**base)


def test_ten_sales_agents_registered():
    assert A.AGENT_NAMES == EXPECTED
    assert len(A.SALES_AGENTS) == 10


def test_selection_by_stage():
    routing = {a.name for a in A.select(PipelineStage.LEAD_ENTERED)}
    assert "lead_router" in routing and "outreach_agent" not in routing
    assigned = {a.name for a in A.select(PipelineStage.ASSIGNED)}
    assert {"followup_agent", "outreach_agent", "pipeline_analyst", "compliance_data_agent"} <= assigned
    assert "lead_router" not in assigned
    scope = {a.name for a in A.select(PipelineStage.SCOPE_ESTIMATE)}
    assert {"estimate_agent", "meeting_prep"} <= scope
    assert "call_recap_agent" not in scope
    assert "call_recap_agent" in {a.name for a in A.select(PipelineStage.SCOPE_ESTIMATE,
                                                            {"call_transcript": "..."})}
    assert {a.name for a in A.select(PipelineStage.CLOSED_WON)} == {"compliance_data_agent"}


async def test_lead_router_scores_captures_assigns_and_stages():
    opp = normalize(raw_lead())
    f = await A.lead_router(ctx(opp))
    types = [a.action_type for a in f.actions]
    assert types.count(ActionType.ADVANCE_STAGE) == 3
    assert ActionType.SCORE_LEAD in types and ActionType.CAPTURE_LEAD in types
    assert ActionType.ASSIGN_REP in types and ActionType.SET_NEXT_ACTION in types
    assert all(a.gate is Gate.AUTONOMOUS for a in f.actions)
    nxt = next(a for a in f.actions if a.action_type is ActionType.SET_NEXT_ACTION)
    assert "Immediate response" in nxt.payload["next_action"]


async def test_lead_router_duplicate_parks_merge():
    d = Deduper()
    first = normalize(raw_lead())
    d.index(first)
    dup = normalize(raw_lead(channel="web_form"))
    f = await A.lead_router(ctx(dup, deduper=d))
    assert f.details["duplicate"]
    types = {a.action_type for a in f.actions}
    assert ActionType.MERGE_CONTACT in types and ActionType.CAPTURE_LEAD not in types
    merge = next(a for a in f.actions if a.action_type is ActionType.MERGE_CONTACT)
    assert merge.gate is Gate.HUMAN_GATED


async def test_lead_router_disqualifies_unreachable_lead():
    opp = normalize(raw_lead(email=None, phone=None))
    f = await A.lead_router(ctx(opp))
    assert "Disqualified" in f.summary
    assert ActionType.ASSIGN_REP not in {a.action_type for a in f.actions}


async def test_outreach_drafts_are_autonomous_and_send_is_gated():
    opp = normalize(raw_lead())
    opp.set_stage(PipelineStage.ASSIGNED)
    f = await A.outreach_agent(ctx(opp))
    draft = next(a for a in f.actions if a.action_type is ActionType.DRAFT_OUTREACH)
    send = next(a for a in f.actions if a.action_type is ActionType.SEND_OUTBOUND)
    assert draft.gate is Gate.AUTONOMOUS and send.gate is Gate.HUMAN_GATED
    assert set(draft.payload["drafts"]) == {"sms", "email", "social"}
    assert "Jane" in draft.payload["drafts"]["sms"] and "Amy Chen" in draft.payload["drafts"]["sms"]
    assert draft.payload["claims_screened"] is True
    assert send.channel.value == "sms" and send.payload["to"] == "9785551234"


async def test_outreach_without_consent_falls_back_to_phone_call():
    opp = normalize(raw_lead(consent_sms=False, consent_email=False, channel="web_form"))
    opp.set_stage(PipelineStage.FOLLOW_UP)
    f = await A.outreach_agent(ctx(opp))
    assert f.details["preferred_channel"] == "phone"


async def test_outreach_no_channel_when_do_not_contact():
    opp = normalize(raw_lead())
    opp.contact.do_not_contact = True
    opp.set_stage(PipelineStage.ASSIGNED)
    f = await A.outreach_agent(ctx(opp))
    assert ActionType.SEND_OUTBOUND not in {a.action_type for a in f.actions}


def test_screen_copy_flags_prohibited_terms():
    assert A.screen_copy("We offer Guaranteed Returns!", ("guaranteed returns",)) == ["guaranteed returns"]
    assert A.screen_copy("Plain follow-up", ("guaranteed returns",)) == []


async def test_followup_flags_stale_and_overdue():
    from datetime import timedelta
    opp = normalize(raw_lead())
    opp.set_stage(PipelineStage.RESPONDED, now=T0 - timedelta(days=3))
    opp.last_touch = T0 - timedelta(days=3)
    opp.next_action, opp.next_action_due = "call", T0 - timedelta(hours=1)
    f = await A.followup_agent(ctx(opp))
    types = {a.action_type for a in f.actions}
    assert ActionType.STALE_FLAG in types and ActionType.ADD_TASK in types
    assert f.details["stale"] and f.details["overdue"]


async def test_followup_recommends_close_after_max_unanswered():
    opp = normalize(raw_lead())
    opp.set_stage(PipelineStage.FOLLOW_UP)
    for i in range(6):
        opp.touch("outbound", "sms", "rep_amy", f"try {i}", at=T0)
    f = await A.followup_agent(ctx(opp))
    close = next(a for a in f.actions if a.action_type is ActionType.CLOSE_DEAL)
    assert close.gate is Gate.HUMAN_GATED and close.payload["reason"] == "unresponsive"


async def test_pipeline_analyst_risk_reasons():
    from datetime import timedelta
    opp = normalize(raw_lead())
    opp.set_stage(PipelineStage.CONTRACT_DEPOSIT, now=T0 - timedelta(days=20))
    opp.confidence = 0.2
    f = await A.pipeline_analyst(ctx(opp))
    reasons = f.details["reasons"]
    assert "missing_next_action" in reasons and "no_deal_amount" in reasons
    assert "low_confidence_late_stage" in reasons and any(r.startswith("stalled_in_stage") for r in reasons)
    assert f.details["risk"] >= 40
    assert ActionType.ADD_TASK in {a.action_type for a in f.actions}


async def test_estimate_agent_drafts_inputs_and_gates_pricing():
    opp = normalize(raw_lead())
    opp.set_stage(PipelineStage.SCOPE_ESTIMATE)
    f = await A.estimate_agent(ctx(opp, inputs={"must_haves": ["skylights"]}))
    by_type = {a.action_type: a for a in f.actions}
    inputs = by_type[ActionType.DRAFT_ESTIMATE_INPUTS].payload["estimate_inputs"]
    assert "Framing + roof tie-in" in inputs["scope_lines"]
    assert inputs["exclusions"] and inputs["allowances"] and inputs["customer_must_haves"] == ["skylights"]
    assert by_type[ActionType.DRAFT_ESTIMATE_INPUTS].gate is Gate.AUTONOMOUS
    assert by_type[ActionType.FINAL_PRICING].gate is Gate.HUMAN_GATED
    assert by_type[ActionType.SEND_PROPOSAL].gate is Gate.HUMAN_GATED


async def test_meeting_prep_brief_has_open_questions_and_history():
    opp = normalize(raw_lead(budget_min=None, budget_max=None, timeline="flexible"))
    opp.set_stage(PipelineStage.APPOINTMENT)
    f = await A.meeting_prep(ctx(opp, account={"prior_deals": [{"id": "d9", "dealstage": "closed_won"}]}))
    brief = next(a for a in f.actions if a.action_type is ActionType.DRAFT_MEETING_BRIEF).payload["brief"]
    assert "Budget range and financing" in brief["open_questions"]
    assert "Target start / completion dates" in brief["open_questions"]
    assert brief["prior_deals"] and brief["history"]


async def test_account_research_tags_repeat_and_referred():
    opp = normalize(raw_lead(referred_by="Realtor Sue", company="Acme"))
    f = await A.account_research(ctx(opp, account={"prior_deals": [{"id": "d1"}], "contact": {}}))
    assert {"repeat_customer", "referred", "commercial_account"} <= set(f.details["suggested_tags"])


async def test_call_recap_extracts_tasks_next_action_and_amount():
    opp = normalize(raw_lead())
    opp.set_stage(PipelineStage.SCOPE_ESTIMATE)
    transcript = ("Met Jane at the house. She wants the addition done before winter. "
                  "I will send the revised scope tomorrow. Need to schedule the engineer visit next week. "
                  "She mentioned a budget around $140k for the whole job.")
    f = await A.call_recap_agent(ctx(opp, inputs={"call_transcript": transcript}))
    types = [a.action_type for a in f.actions]
    assert types.count(ActionType.ADD_TASK) == 2
    assert ActionType.SET_NEXT_ACTION in types
    amt = next(a for a in f.actions if a.action_type is ActionType.CHANGE_DEAL_AMOUNT)
    assert amt.payload["amount"] == 140_000 and amt.gate is Gate.HUMAN_GATED


async def test_sales_coach_surfaces_slow_response_and_low_win_rate():
    from datetime import timedelta
    lr = RepLearning(rep_id="rep_amy")
    for _ in range(4):
        lr.on_assigned()
    lr.on_responded(T0, T0 + timedelta(minutes=90), "sms")
    for _ in range(5):
        lr.on_closed(won=False, source=normalize(raw_lead()).source,
                     project_type=normalize(raw_lead()).project_type, reason="price", at=T0)
    opp = normalize(raw_lead())
    f = await A.sales_coach(ctx(opp, learning=lr))
    insights = " ".join(f.details["insights"])
    assert "median first response 90.0 min" in insights
    assert "win rate 0%" in insights
    assert "25% of assigned leads" in insights


async def test_sales_coach_insufficient_data():
    f = await A.sales_coach(ctx(normalize(raw_lead()), learning=RepLearning(rep_id="rep_amy")))
    assert f.actions == [] and "insufficient" in f.summary


async def test_compliance_blocks_channels_without_consent_and_flags_sensitive():
    opp = normalize(raw_lead(consent_sms=False, consent_email=False,
                             description="my SSN is 123-45-6789, DOB on file"))
    opp.set_stage(PipelineStage.ASSIGNED)
    f = await A.compliance_data_agent(ctx(opp))
    assert set(f.blocking) == {"sms", "email"}
    assert set(f.details["sensitive"]) == {"ssn", "dob"}
    types = {a.action_type for a in f.actions}
    assert ActionType.DATA_QUALITY_FLAG in types and ActionType.REDACT_SENSITIVE in types
    assert "assigned_rep" in f.details["missing"]


async def test_compliance_do_not_contact_blocks_everything():
    opp = normalize(raw_lead())
    opp.contact.do_not_contact = True
    f = await A.compliance_data_agent(ctx(opp))
    assert set(f.blocking) == {"sms", "email", "social", "phone"}


def test_detect_sensitive_card_numbers():
    assert A.detect_sensitive("card 4111 1111 1111 1111 please") == ["card_number"]
    assert A.detect_sensitive("phone 978-555-1234") == []
