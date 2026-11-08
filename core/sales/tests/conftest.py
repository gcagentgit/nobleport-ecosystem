from datetime import datetime, timezone

import pytest

from core.sales.crm import InMemoryCrm
from core.sales.models import RawLead, SalesRep
from core.sales.orchestrator import SalesOrchestrator

T0 = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        from datetime import timedelta
        self.now = self.now + timedelta(**kw)
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def reps():
    return [
        SalesRep(rep_id="rep_amy", name="Amy Chen", territories=["newburyport", "salem"],
                 hubspot_owner_id="101", capacity=5),
        SalesRep(rep_id="rep_bob", name="Bob Ruiz", hubspot_owner_id="102", capacity=5),
    ]


@pytest.fixture
def orch(reps, clock):
    return SalesOrchestrator(InMemoryCrm(), reps, clock=clock,
                             prohibited_terms=("guaranteed returns", "risk-free"))


def raw_lead(**kw) -> RawLead:
    base = dict(channel="instagram", name="Jane Doe", phone="(978) 555-1234", email="jane@example.com",
                property_address="12 High Street, Newburyport, MA", project_type="addition",
                budget_min=100_000, budget_max=150_000, timeline="asap", consent_sms=True,
                consent_email=True,
                description="Second-floor addition over the garage, need it before winter.",
                received_at=T0)
    base.update(kw)
    return RawLead(**base)
