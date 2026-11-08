import pytest

from core.sales import crm
from core.sales.intake import normalize
from core.sales.learning import LearningStore, RepLearning
from core.sales.models import Contact, PipelineStage
from core.sales.tests.conftest import T0, raw_lead


def test_deal_property_map_covers_required_opportunity_fields():
    for f in ("pipeline_stage", "property_address", "source", "project_type", "next_action",
              "next_action_due", "assigned_rep", "confidence", "qualification", "last_touch"):
        assert f in crm.DEAL_PROPERTY_MAP


def test_deal_properties_serialize_enums_dates_and_owner():
    opp = normalize(raw_lead())
    opp.set_stage(PipelineStage.ASSIGNED, now=T0)
    opp.assigned_rep = "rep_amy"
    props = crm.deal_properties(opp, owner_id="101")
    assert props["dealstage"] == "assigned" and props["hubspot_owner_id"] == "101"
    assert props["np_lead_source"] == "social" and props["np_project_type"] == "addition"
    assert props["notes_last_contacted"] == T0.isoformat()
    assert props["dealname"].startswith("Jane Doe — addition — 12 High Street")


def test_custom_stage_map():
    opp = normalize(raw_lead())
    opp.set_stage(PipelineStage.APPOINTMENT)
    props = crm.deal_properties(opp, stage_map={**crm.DEFAULT_STAGE_MAP, PipelineStage.APPOINTMENT: "1234567"})
    assert props["dealstage"] == "1234567"


def test_material_update_detection():
    assert crm.is_material_deal_update({"amount": 1})
    assert crm.is_material_deal_update({"hubspot_owner_id": "5"})
    assert crm.is_material_deal_update({"dealstage": "closed_won"})
    assert not crm.is_material_deal_update({"dealstage": "responded"})
    assert not crm.is_material_deal_update({"np_next_action": "call"})


async def test_in_memory_crm_refuses_material_writes_without_signature():
    c = crm.InMemoryCrm()
    opp = normalize(raw_lead())
    opp.contact_id = await c.upsert_contact(opp.contact)
    did = await c.create_deal(opp)
    with pytest.raises(crm.CrmWriteRefused):
        await c.update_deal(did, {"amount": 5})
    await c.update_deal(did, {"amount": 5}, human_signature="sig")
    assert c.deals[did]["amount"] == 5 and c.deals[did]["_last_signature"] == "sig"
    with pytest.raises(crm.CrmWriteRefused):
        await c.log_outbound(deal_id=did, channel="sms", body="x", by="a", human_signature="")


async def test_in_memory_upsert_dedupes_contact_by_email():
    c = crm.InMemoryCrm()
    a = await c.upsert_contact(Contact(name="Jane Doe", email="j@x.com"))
    b = await c.upsert_contact(Contact(name="Jane D", email="j@x.com", phone="9785551234"))
    assert a == b and c.contacts[a]["phone"] == "9785551234"
    assert await c.find_contact(email=None, phone="9785551234") == a


def test_contact_properties_split_name():
    props = crm.contact_properties(Contact(name="Jane Q Doe", email="j@x.com", consent_sms=True))
    assert props["firstname"] == "Jane" and props["lastname"] == "Q Doe"
    assert props["np_consent_sms"] is True


def test_hubspot_client_is_staged_without_token():
    h = crm.HubSpotCrm(None)
    assert not h.configured
    with pytest.raises(RuntimeError):
        h._client()


def test_rep_learning_metrics():
    from datetime import timedelta
    from core.sales.models import LeadSource, ProjectType
    lr = RepLearning(rep_id="rep_amy")
    lr.on_assigned(); lr.on_assigned()
    lr.on_responded(T0, T0 + timedelta(minutes=5), "sms")
    lr.on_responded(T0, T0 + timedelta(minutes=25), "email")
    lr.on_closed(won=True, source=LeadSource.REFERRAL, project_type=ProjectType.ROOF, reason=None, at=T0)
    lr.on_closed(won=False, source=LeadSource.SOCIAL, project_type=ProjectType.ROOF, reason="price", at=T0)
    assert lr.median_response_minutes() == 15.0 and lr.response_rate() == 1.0
    assert lr.win_rate() == 0.5 and lr.best_source() == "referral"
    store = LearningStore()
    store._by_rep["rep_amy"] = lr
    team = store.team_summary()
    assert team["won"] == 1 and team["win_rate"] == 0.5 and team["median_response_minutes"] == 15.0
