"""Lead qualification scoring — a faithful port of ``server/leadScoring.ts``
in nobleport-gateway so the Python agents and the TypeScript intake form
agree on the number (0–100) and the tier.

    70+   HIGH   -> qualified, high confidence
    40-69 MEDIUM -> qualifying, needs a rep touch
    0-39  LOW    -> unqualified, nurture
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Opportunity, Qualification

PROJECT_TYPE_SCORES: dict[str, int] = {
    "new_construction": 25, "commercial": 25, "addition": 22, "renovation": 20,
    "foundation": 18, "roof": 16, "adu": 16, "siding": 14, "deck": 12, "hvac": 12,
    "electrical": 10, "plumbing": 10, "demolition": 8, "painting": 6, "other": 5,
}

# Essex County MA — primary market
PRIMARY_MARKET_TOWNS: tuple[str, ...] = (
    "amesbury", "andover", "beverly", "boxford", "danvers", "essex",
    "georgetown", "gloucester", "groveland", "hamilton", "haverhill",
    "ipswich", "lawrence", "lynn", "lynnfield", "manchester",
    "marblehead", "merrimac", "methuen", "middleton", "nahant",
    "newbury", "newburyport", "north andover", "peabody", "rockport",
    "rowley", "salem", "salisbury", "saugus", "swampscott", "topsfield",
    "wenham", "west newbury",
)

TIMELINE_SCORES: dict[str, int] = {
    "asap": 10, "1_month": 8, "3_months": 5, "6_months": 3, "flexible": 1,
}


@dataclass
class ScoreResult:
    score: int
    tier: str                                  # high | medium | low
    breakdown: dict[str, int] = field(default_factory=dict)
    deposit_amount_cents: int = 0
    auto_deposit: bool = False

    @property
    def qualification(self) -> Qualification:
        return {"high": Qualification.QUALIFIED,
                "medium": Qualification.QUALIFYING,
                "low": Qualification.UNQUALIFIED}[self.tier]

    @property
    def confidence(self) -> float:
        return round(self.score / 100, 2)


def _budget_points(half_budget: float) -> int:
    if half_budget >= 200_000: return 25
    if half_budget >= 100_000: return 22
    if half_budget >= 50_000: return 18
    if half_budget >= 25_000: return 14
    if half_budget >= 10_000: return 10
    if half_budget >= 5_000: return 6
    if half_budget > 0: return 3
    return 0


def _sqft_points(sqft: int) -> int:
    if sqft >= 5000: return 15
    if sqft >= 3000: return 12
    if sqft >= 2000: return 10
    if sqft >= 1000: return 7
    if sqft >= 500: return 4
    if sqft > 0: return 2
    return 0


def _location_points(location: Optional[str]) -> int:
    if not location:
        return 0
    loc = location.lower()
    if any(town in loc for town in PRIMARY_MARKET_TOWNS):
        return 15
    if "ma" in loc or "massachusetts" in loc:
        return 10
    return 5 if loc else 0


def score_lead(*, project_type: str, sqft: Optional[int], location: Optional[str],
               budget_min: Optional[int], budget_max: Optional[int],
               timeline: Optional[str], phone: Optional[str], company: Optional[str],
               description: Optional[str]) -> ScoreResult:
    breakdown = {"projectType": PROJECT_TYPE_SCORES.get(project_type, 5)}

    if budget_max:
        avg_budget = (budget_min or 0) + budget_max
    elif budget_min:
        avg_budget = budget_min * 2
    else:
        avg_budget = 0
    half_budget = avg_budget / 2
    breakdown["budget"] = _budget_points(half_budget)
    breakdown["sqft"] = _sqft_points(sqft or 0)
    breakdown["location"] = _location_points(location)
    breakdown["timeline"] = TIMELINE_SCORES.get(timeline or "flexible", 1)

    completeness = 0
    if phone: completeness += 4
    if company: completeness += 3
    if description and len(description) > 20: completeness += 3
    breakdown["completeness"] = completeness

    score = min(100, sum(breakdown.values()))
    tier = "high" if score >= 70 else "medium" if score >= 40 else "low"

    estimated_budget = half_budget or 10_000
    deposit_cents = int(min(500_000, max(50_000, round(estimated_budget * 0.1 * 100))))

    return ScoreResult(score=score, tier=tier, breakdown=breakdown,
                       deposit_amount_cents=deposit_cents, auto_deposit=tier == "high")


def score_opportunity(opp: Opportunity) -> ScoreResult:
    return score_lead(
        project_type=opp.project_type.value, sqft=opp.sqft,
        location=opp.property_address, budget_min=opp.budget_min,
        budget_max=opp.budget_max, timeline=opp.timeline.value,
        phone=opp.contact.phone, company=opp.contact.company,
        description=opp.description,
    )


def explain(result: ScoreResult) -> str:
    lines = [f"Lead Score: {result.score}/100 ({result.tier.upper()})",
             f"  Project Type: +{result.breakdown['projectType']}/25",
             f"  Budget: +{result.breakdown['budget']}/25",
             f"  Square Footage: +{result.breakdown['sqft']}/15",
             f"  Location: +{result.breakdown['location']}/15",
             f"  Timeline: +{result.breakdown['timeline']}/10",
             f"  Completeness: +{result.breakdown['completeness']}/10"]
    if result.auto_deposit:
        lines.append(f"  -> AUTO-DEPOSIT: ${result.deposit_amount_cents / 100:.2f}")
    return "\n".join(lines)
