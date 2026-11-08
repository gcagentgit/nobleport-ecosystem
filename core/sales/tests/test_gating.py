"""Autonomy contract: research/monitor/prioritize/draft run alone; sending,
pricing, commitments and material CRM changes PARK at the *existing*
governance gate without a human signature."""
from core.gateway.config import settings
from core.gateway.governance import GovernanceGate
from core.sales import gating
from core.sales.models import ActionType, Gate, SalesAction


def _gate():
    return GovernanceGate(settings.launch_gates_path)


def test_every_action_type_has_a_policy():
    assert set(gating.POLICY) == set(ActionType)


def test_autonomous_groups():
    for t in (ActionType.RESEARCH_ACCOUNT, ActionType.STALE_FLAG, ActionType.PRIORITIZE,
              ActionType.DRAFT_OUTREACH, ActionType.DRAFT_ESTIMATE_INPUTS, ActionType.DRAFT_MEETING_BRIEF,
              ActionType.ADD_NOTE, ActionType.ADD_TASK, ActionType.SET_NEXT_ACTION, ActionType.CAPTURE_LEAD,
              ActionType.ASSIGN_REP):
        assert t in gating.AUTONOMOUS_ACTIONS, t


def test_human_gated_groups_cover_the_four_categories():
    g = gating.GATED_GROUPS
    assert ActionType.SEND_OUTBOUND in g["outbound_communications"]
    assert ActionType.FINAL_PRICING in g["final_pricing_estimates"]
    assert ActionType.CONTRACT_COMMITMENT in g["contractual_commitments"]
    assert {ActionType.REASSIGN_REP, ActionType.MERGE_CONTACT, ActionType.DELETE_RECORD,
            ActionType.CLOSE_DEAL, ActionType.CHANGE_DEAL_AMOUNT} <= g["material_crm_changes"]
    for group in g.values():
        assert group <= gating.HUMAN_GATED_ACTIONS


def test_classify_overrides_agent_claim():
    a = SalesAction(action_type=ActionType.SEND_OUTBOUND, agent="outreach_agent",
                    gate=Gate.AUTONOMOUS, approval_level="L0")
    gating.classify(a)
    assert a.gate is Gate.HUMAN_GATED and a.approval_level == "L3"


def test_outbound_send_parks_at_governance_gate_without_signature():
    a = gating.classify(SalesAction(action_type=ActionType.SEND_OUTBOUND, agent="outreach_agent",
                                    opportunity_id="opp_1", payload={"body": "Hi Jane"}))
    outcome = _gate().evaluate(gating.to_envelope(a))
    assert outcome.decision == "PARK" and outcome.requires_human
    a.human_signature = "sig-rep-1"
    assert _gate().evaluate(gating.to_envelope(a)).decision == "PERMIT"


def test_final_pricing_parks_even_though_price_estimate_tool_is_l1():
    a = gating.classify(SalesAction(action_type=ActionType.FINAL_PRICING, agent="estimate_agent"))
    env = gating.to_envelope(a)
    assert env.action == "gcagent.price_estimate" and env.human_approval_required
    assert _gate().evaluate(env).decision == "PARK"


def test_contract_and_deposit_are_l4():
    for t in (ActionType.CONTRACT_COMMITMENT, ActionType.DEPOSIT_REQUEST):
        env = gating.to_envelope(gating.classify(SalesAction(action_type=t, agent="x")))
        assert env.action == "kuzo.capture_customer_approval"
        out = _gate().evaluate(env)
        assert out.decision == "PARK" and out.level == "L4"


def test_research_and_drafts_permit_autonomously():
    for t in (ActionType.RESEARCH_ACCOUNT, ActionType.DRAFT_OUTREACH, ActionType.PRIORITIZE,
              ActionType.ADD_NOTE, ActionType.CAPTURE_LEAD):
        env = gating.to_envelope(gating.classify(SalesAction(action_type=t, agent="x")))
        assert _gate().evaluate(env).decision == "PERMIT", t


def test_prohibited_claim_in_draft_still_blocked_by_gateway():
    a = gating.classify(SalesAction(action_type=ActionType.DRAFT_OUTREACH, agent="outreach_agent",
                                    rationale="promise guaranteed returns on the addition"))
    assert _gate().evaluate(gating.to_envelope(a)).decision == "BLOCK"
