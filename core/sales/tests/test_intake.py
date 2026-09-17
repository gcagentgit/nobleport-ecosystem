from core.sales import intake
from core.sales.models import LeadSource, Opportunity, PipelineStage, ProjectType, SalesRep
from core.sales.tests.conftest import raw_lead


def test_all_five_channels_feed_one_intake():
    seen = set()
    for channel in ("instagram", "inbound_call", "web_form", "email", "referral_partner"):
        opp = intake.normalize(raw_lead(channel=channel))
        assert isinstance(opp, Opportunity)
        assert opp.pipeline_stage is PipelineStage.LEAD_ENTERED
        seen.add(opp.source)
    assert seen == {LeadSource.SOCIAL, LeadSource.PHONE, LeadSource.WEBSITE, LeadSource.EMAIL,
                    LeadSource.REFERRAL}


def test_messaging_channels_map_onto_canonical_sources():
    assert intake.source_for_channel("WhatsApp") is LeadSource.PHONE
    assert intake.source_for_channel("Facebook") is LeadSource.SOCIAL
    assert intake.source_for_channel("chat-widget") is LeadSource.WEBSITE
    assert intake.source_for_channel("carrier pigeon") is LeadSource.OTHER


def test_referred_by_promotes_unknown_channel_to_referral():
    opp = intake.normalize(raw_lead(channel="mystery", referred_by="Realtor Sue"))
    assert opp.source is LeadSource.REFERRAL


def test_normalize_cleans_contact_and_project_fields():
    opp = intake.normalize(raw_lead(email=" Jane@Example.COM ", phone="+1 (978) 555-1234",
                                    project_type="New Construction", timeline="3-months"))
    assert opp.contact.email == "jane@example.com"
    assert opp.contact.phone == "9785551234"
    assert opp.project_type is ProjectType.NEW_CONSTRUCTION
    assert opp.timeline.value == "3_months"
    assert opp.touches and opp.touches[0].direction == "inbound"
    assert opp.last_touch == opp.touches[0].at


def test_dedupe_matches_email_phone_and_open_address():
    d = intake.Deduper()
    first = intake.normalize(raw_lead())
    d.index(first)
    by_email = intake.normalize(raw_lead(phone=None, email="JANE@example.com", property_address=None))
    by_phone = intake.normalize(raw_lead(email=None, phone="978.555.1234", property_address=None))
    by_addr = intake.normalize(raw_lead(email="other@x.com", phone="6175550000",
                                        property_address="12 High St., Newburyport MA"))
    assert d.find(by_email).matched_on == "email"
    assert d.find(by_phone).matched_on == "phone"
    assert d.find(by_addr).matched_on == "address"
    # a closed opportunity at the same address is not a duplicate (new project)
    first.set_stage(PipelineStage.CLOSED_WON)
    d.index(first)
    assert d.find(by_addr) is None
    assert d.find(first) is None


def test_assigner_prefers_territory_then_fewest_open():
    amy = SalesRep(rep_id="rep_amy", name="Amy", territories=["newburyport"], capacity=2)
    bob = SalesRep(rep_id="rep_bob", name="Bob", capacity=2)
    a = intake.Assigner.from_reps([amy, bob])
    local = intake.normalize(raw_lead())
    assert a.choose(local) == "rep_amy"
    a.note_assignment("rep_amy"); a.note_assignment("rep_amy")
    # over capacity -> falls back to the pool with room, but territory still wins when tied
    elsewhere = intake.normalize(raw_lead(property_address="1 Main St, Worcester, MA"))
    assert a.choose(elsewhere) == "rep_bob"
    a.note_close("rep_amy")
    assert a.open_counts["rep_amy"] == 1


def test_assigner_respects_project_type_fit():
    roofer = SalesRep(rep_id="rep_roof", name="R", project_types=[ProjectType.ROOF])
    gen = SalesRep(rep_id="rep_gen", name="G")
    a = intake.Assigner.from_reps([roofer, gen])
    assert a.choose(intake.normalize(raw_lead(project_type="roof", property_address=None))) == "rep_roof"
    assert a.choose(intake.normalize(raw_lead(project_type="deck", property_address=None))) == "rep_gen"


def test_assigner_no_active_reps():
    a = intake.Assigner.from_reps([SalesRep(rep_id="x", name="X", active=False)])
    assert a.choose(intake.normalize(raw_lead())) is None
