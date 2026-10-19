import pytest
from pydantic import ValidationError

from core.sales import models as M


REQUIRED_ON_EVERY_OPPORTUNITY = ("contact_id", "deal_id", "property_address", "source", "project_type",
                                 "pipeline_stage", "last_touch", "next_action", "next_action_due",
                                 "assigned_rep", "confidence", "qualification")


def test_opportunity_carries_every_required_field():
    opp = M.Opportunity()
    for name in REQUIRED_ON_EVERY_OPPORTUNITY:
        assert name in M.Opportunity.model_fields, name
        assert hasattr(opp, name)


def test_model_is_keyed_on_rep_id_not_learner_id():
    assert "rep_id" in M.SalesRep.model_fields
    assert "rep_id" in M.SalesAction.model_fields
    for model in (M.SalesRep, M.Opportunity, M.SalesAction):
        assert "learner_id" not in model.model_fields


def test_operating_loop_order():
    names = [s.value for s in M.STAGE_ORDER]
    assert names == ["lead_entered", "deduped", "qualified", "assigned", "responded", "appointment",
                     "scope_estimate", "follow_up", "contract_deposit", "production_handoff", "closed_won"]
    assert M.next_stage(M.PipelineStage.CLOSED_WON) is None
    assert M.next_stage(M.PipelineStage.CLOSED_LOST) is None
    assert M.next_stage(M.PipelineStage.ASSIGNED) is M.PipelineStage.RESPONDED


def test_autonomous_stages_stop_before_customer_contact():
    assert M.PipelineStage.ASSIGNED in M.AUTONOMOUS_STAGES
    assert M.PipelineStage.RESPONDED not in M.AUTONOMOUS_STAGES
    assert not (M.AUTONOMOUS_STAGES & M.TERMINAL_STAGES)


def test_confidence_bounded():
    with pytest.raises(ValidationError):
        M.Opportunity(confidence=1.5)
    with pytest.raises(ValidationError):
        M.Opportunity(assigned_rep="   ")


def test_missing_required_fields_by_stage():
    opp = M.Opportunity(pipeline_stage=M.PipelineStage.ASSIGNED, source=M.LeadSource.PHONE)
    missing = M.missing_required_fields(opp)
    assert "contact_id" in missing and "assigned_rep" in missing and "next_action" in missing
    opp.contact_id, opp.assigned_rep = "c1", "rep_amy"
    opp.next_action, opp.next_action_due = "call", M.utcnow()
    assert M.missing_required_fields(opp) == []


def test_every_source_channel_lands_in_one_enum():
    assert {s.value for s in M.LeadSource} >= {"social", "phone", "website", "email", "referral"}
