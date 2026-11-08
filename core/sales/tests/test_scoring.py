"""Parity with nobleport-gateway/server/leads.test.ts."""
from core.sales import scoring
from core.sales.models import Qualification


def test_high_value_commercial_lead_in_primary_market():
    r = scoring.score_lead(project_type="commercial", sqft=5000, location="Newburyport, MA",
                           budget_min=150000, budget_max=250000, timeline="asap",
                           phone="978-555-1234", company="Acme Construction",
                           description="Full commercial build-out for new office space downtown.")
    assert r.score >= 70 and r.tier == "high" and r.auto_deposit
    assert r.breakdown["projectType"] == 25
    assert r.breakdown["sqft"] == 15
    assert r.breakdown["location"] == 15
    assert r.breakdown["timeline"] == 10
    assert r.qualification is Qualification.QUALIFIED
    assert r.confidence == round(r.score / 100, 2)


def test_low_value_lead():
    r = scoring.score_lead(project_type="painting", sqft=None, location=None, budget_min=None,
                           budget_max=None, timeline="flexible", phone=None, company=None,
                           description=None)
    assert r.tier == "low" and r.score < 40 and not r.auto_deposit
    assert r.deposit_amount_cents == 100_000            # 10% of default $10K floor -> $1,000
    assert r.qualification is Qualification.UNQUALIFIED


def test_medium_lead_and_deposit_bounds():
    r = scoring.score_lead(project_type="deck", sqft=600, location="Boston, MA", budget_min=20000,
                           budget_max=30000, timeline="3_months", phone="x", company=None,
                           description="Composite deck with stairs and lighting please")
    assert r.tier == "medium"
    assert r.deposit_amount_cents == 250_000            # 10% of $25K
    big = scoring.score_lead(project_type="commercial", sqft=None, location=None, budget_min=900000,
                             budget_max=1100000, timeline=None, phone=None, company=None, description=None)
    assert big.deposit_amount_cents == 500_000          # capped at $5,000


def test_explain_mentions_auto_deposit():
    r = scoring.score_lead(project_type="new_construction", sqft=4000, location="Salem, MA",
                           budget_min=400000, budget_max=600000, timeline="1_month",
                           phone="1", company="c", description="x" * 30)
    text = scoring.explain(r)
    assert "AUTO-DEPOSIT" in text and "HIGH" in text
