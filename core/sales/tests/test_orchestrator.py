from datetime import timedelta

import pytest

from core.sales.approvals import ApprovalError
from core.sales.crm import CrmWriteRefused
from core.sales.models import ActionStatus, ActionType, PipelineStage, Qualification
from core.sales.tests.conftest import raw_lead


async def _to_assigned(orch):
    r = await orch.intake(raw_lead())
    return orch.opportunities[r.opportunity_id], r


async def test_intake_runs_lead_to_assigned_autonomously(orch):
    opp, r = await _to_assigned(orch)
    assert r.stage_before is PipelineStage.LEAD_ENTERED and r.stage_after is PipelineStage.ASSIGNED
    assert opp.contact_id and opp.deal_id and opp.assigned_rep == "rep_amy"
    assert opp.qualification is Qualification.QUALIFIED and opp.score >= 70 and opp.confidence >= 0.7
    assert opp.next_action and opp.next_action_due == orch.clock() + timedelta(minutes=15)
    assert r.parked == [] and r.vetoed == []
    deal = orch.crm.deals[opp.deal_id]
    assert deal["dealstage"] == "assigned" and deal["np_lead_source"] == "social"
    assert deal["np_property_address"] == "12 High Street, Newburyport, MA"
    assert deal["hubspot_owner_id"] == "101"


async def test_immediate_response_is_drafted_but_parked_until_signed(orch, clock):
    opp, _ = await _to_assigned(orch)
    r = await orch.run(opp)
    assert "outreach_agent" in r.agents
    sends = [a for a in r.parked if a.action_type is ActionType.SEND_OUTBOUND]
    assert len(sends) == 1 and sends[0].status is ActionStatus.PARKED
    assert opp.pipeline_stage is PipelineStage.ASSIGNED          # nothing went out
    assert orch.crm.outbound == []
    with pytest.raises(ApprovalError):
        await orch.approve(sends[0].action_id, signature="", approver="rep_amy")
    clock.advance(minutes=7)
    done = await orch.approve(sends[0].action_id, signature="sig-amy", approver="rep_amy")
    assert done.status is ActionStatus.EXECUTED
    assert opp.pipeline_stage is PipelineStage.RESPONDED
    assert orch.crm.outbound[0]["channel"] == "sms" and orch.crm.outbound[0]["signature"] == "sig-amy"
    assert orch.crm.deals[opp.deal_id]["dealstage"] == "responded"
    assert orch.learning.get("rep_amy").response_minutes == [7.0]
    env = done.payload["gateway_envelope"]
    assert env["action"] == "kuzo.send_customer_update" and env["human_signature"] == "sig-amy"


async def test_reject_leaves_opportunity_untouched(orch):
    opp, _ = await _to_assigned(orch)
    r = await orch.run(opp)
    send = next(a for a in r.parked if a.action_type is ActionType.SEND_OUTBOUND)
    orch.reject(send.action_id, reason="wrong tone", by="rep_amy")
    assert send.status is ActionStatus.REJECTED
    assert opp.pipeline_stage is PipelineStage.ASSIGNED and orch.crm.outbound == []
    with pytest.raises(ApprovalError):
        await orch.approve(send.action_id, signature="s", approver="rep_amy")


async def test_compliance_veto_blocks_sms_without_consent(orch):
    r = await orch.intake(raw_lead(consent_sms=False, consent_email=False, channel="web_form"))
    opp = orch.opportunities[r.opportunity_id]
    r2 = await orch.run(opp)
    # preferred channel falls back to a phone call, which needs no consent -> parked, not vetoed
    assert [a.channel.value for a in r2.parked if a.action_type is ActionType.SEND_OUTBOUND] == ["phone"]
    # force an sms proposal through the veto path
    opp.contact.consent_sms = False
    from core.sales.models import OutboundChannel, SalesAction
    from core.sales import agents as A
    sms = SalesAction(action_type=ActionType.SEND_OUTBOUND, agent="outreach_agent",
                      opportunity_id=opp.opportunity_id, channel=OutboundChannel.SMS)
    compliance = await A.compliance_data_agent(A.AgentContext(opp=opp, now=orch.clock(), config=orch.config))
    assert "sms" in compliance.blocking


async def test_full_operating_loop_to_closed_won_and_learning(orch, clock):
    opp, _ = await _to_assigned(orch)
    r = await orch.run(opp)
    send = next(a for a in r.parked if a.action_type is ActionType.SEND_OUTBOUND)
    await orch.approve(send.action_id, signature="s1", approver="rep_amy")
    orch.inbound_reply(opp.opportunity_id, "sms", "Sounds good, come Thursday")
    orch.book_appointment(opp.opportunity_id, clock.advance(days=2), by="rep_amy")
    assert opp.pipeline_stage is PipelineStage.APPOINTMENT
    r = await orch.run(opp)
    assert "meeting_prep" in r.agents
    orch.start_scope(opp.opportunity_id, by="rep_amy")
    r = await orch.run(opp, inputs={"call_transcript": "Walked the site. Will send scope tomorrow. Budget $140k."})
    parked = {a.action_type: a for a in r.parked}
    assert {ActionType.FINAL_PRICING, ActionType.SEND_PROPOSAL, ActionType.CHANGE_DEAL_AMOUNT} <= set(parked)
    assert any(t["subject"].startswith("Will send scope") for t in orch.crm.tasks)
    # pricing is signed by the estimator, then the proposal by the rep
    parked[ActionType.FINAL_PRICING].payload["amount"] = 142_500
    await orch.approve(parked[ActionType.FINAL_PRICING].action_id, signature="s-est", approver="estimator")
    assert opp.deal_amount == 142_500 and orch.crm.deals[opp.deal_id]["amount"] == 142_500
    await orch.approve(parked[ActionType.SEND_PROPOSAL].action_id, signature="s2", approver="rep_amy")
    assert opp.pipeline_stage is PipelineStage.FOLLOW_UP
    # contract + deposit, handoff, closed won — each needs a signature
    from core.sales.models import SalesAction
    contract = orch.approvals.park(SalesAction(action_type=ActionType.CONTRACT_COMMITMENT, agent="human",
                                               opportunity_id=opp.opportunity_id, rep_id="rep_amy"))
    await orch.approve(contract.action_id, signature="s3", approver="owner")
    assert opp.pipeline_stage is PipelineStage.CONTRACT_DEPOSIT
    handoff = orch.approvals.park(SalesAction(action_type=ActionType.PRODUCTION_HANDOFF, agent="human",
                                              opportunity_id=opp.opportunity_id, rep_id="rep_amy"))
    await orch.approve(handoff.action_id, signature="s4", approver="ops")
    assert opp.pipeline_stage is PipelineStage.PRODUCTION_HANDOFF
    await orch.record_outcome(opp.opportunity_id, won=True, reason=None, signature="s5", by="owner")
    assert opp.pipeline_stage is PipelineStage.CLOSED_WON and not opp.is_open
    lr = orch.learning.get("rep_amy")
    assert lr.won == 1 and lr.appointments == 1 and lr.estimates == 1 and lr.win_rate() == 1.0
    assert lr.won_by_source == {"social": 1} and lr.best_source() == "social"
    assert orch.crm.deals[opp.deal_id]["dealstage"] == "closed_won"


async def test_closed_lost_needs_signature_and_records_reason(orch):
    opp, _ = await _to_assigned(orch)
    with pytest.raises(CrmWriteRefused):
        await orch.crm.update_deal(opp.deal_id, {"dealstage": "closed_lost"})
    await orch.record_outcome(opp.opportunity_id, won=False, reason="went with another GC",
                              signature="s", by="rep_amy")
    assert opp.pipeline_stage is PipelineStage.CLOSED_LOST and opp.lost_reason == "went with another GC"
    assert orch.learning.get("rep_amy").lost_reasons == {"went with another GC": 1}
    assert orch.assigner.open_counts["rep_amy"] == 0


async def test_agents_cannot_advance_past_assigned(orch):
    from core.sales.models import SalesAction
    opp, _ = await _to_assigned(orch)
    bad = SalesAction(action_type=ActionType.ADVANCE_STAGE, agent="rogue",
                      opportunity_id=opp.opportunity_id, payload={"stage": "closed_won"})
    with pytest.raises(PermissionError):
        await orch._apply(opp, bad, orch.clock())
    assert opp.pipeline_stage is PipelineStage.ASSIGNED


async def test_execute_approved_refuses_unsigned(orch):
    from core.sales.models import SalesAction
    opp, _ = await _to_assigned(orch)
    a = SalesAction(action_type=ActionType.SEND_OUTBOUND, agent="x", opportunity_id=opp.opportunity_id,
                    status=ActionStatus.APPROVED)
    with pytest.raises(PermissionError):
        await orch.execute_approved(a)


async def test_duplicate_intake_routes_to_existing_and_parks_merge(orch):
    opp, _ = await _to_assigned(orch)
    r = await orch.intake(raw_lead(channel="email", name="Jane D", phone=None))
    dup = orch.opportunities[r.opportunity_id]
    assert dup.duplicate_of == opp.opportunity_id and "duplicate" in dup.tags
    assert dup.pipeline_stage is PipelineStage.DEDUPED and dup.assigned_rep is None
    assert [a.action_type for a in r.parked] == [ActionType.MERGE_CONTACT]
    assert any(n["deal_id"] == opp.deal_id and "Repeat inbound" in n["body"] for n in orch.crm.notes)


async def test_sweep_and_command_view(orch, clock):
    opp, _ = await _to_assigned(orch)
    await orch.intake(raw_lead(channel="inbound_call", email="bob@x.com", phone="6175550001",
                               property_address="5 Elm St, Worcester, MA", project_type="deck",
                               budget_min=15000, budget_max=20000))
    clock.advance(days=3)
    results = await orch.sweep()
    assert len(results) == 2
    view = orch.command_view()
    assert view["truth_label"] == "STAGED"
    assert view["opportunities"]["open"] == 2
    assert view["opportunities"]["by_source"] == {"social": 1, "phone": 1}
    assert view["pending_approvals"]["by_type"].get("send_outbound") == 2
    assert view["pipeline_risk"] and view["pipeline_risk"][0]["risk"] >= 40
    reps = {r["rep_id"]: r for r in view["reps"]}
    assert reps["rep_amy"]["open"] == 1 and reps["rep_bob"]["open"] == 1
    assert reps["rep_amy"]["overdue"] == 1 and reps["rep_amy"]["pending_approvals"] == 1
    assert view["team"]["leads_assigned"] == 2


async def test_no_active_rep_leaves_lead_qualified_and_unassigned(clock):
    from core.sales.crm import InMemoryCrm
    from core.sales.models import SalesRep
    from core.sales.orchestrator import SalesOrchestrator
    o = SalesOrchestrator(InMemoryCrm(), [SalesRep(rep_id="x", name="X", active=False)], clock=clock)
    r = await o.intake(raw_lead())
    opp = o.opportunities[r.opportunity_id]
    assert opp.pipeline_stage is PipelineStage.QUALIFIED and opp.assigned_rep is None
    assert o.command_view()["unassigned"] == 1
