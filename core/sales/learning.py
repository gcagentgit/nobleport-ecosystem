"""Rep learning loop — closed won/lost feeds back into per-rep stats.

Keyed on ``rep_id``. The sales_coach reads this to surface response speed,
conversion and coaching opportunities; the orchestrator's command view rolls
it up across the whole team.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Optional

from pydantic import BaseModel, Field

from .models import LeadSource, ProjectType


class RepLearning(BaseModel):
    rep_id: str
    leads_assigned: int = 0
    responded: int = 0
    appointments: int = 0
    estimates: int = 0
    won: int = 0
    lost: int = 0
    response_minutes: list[float] = Field(default_factory=list)
    won_by_source: dict[str, int] = Field(default_factory=dict)
    lost_by_source: dict[str, int] = Field(default_factory=dict)
    won_by_project_type: dict[str, int] = Field(default_factory=dict)
    lost_reasons: dict[str, int] = Field(default_factory=dict)
    activity: dict[str, int] = Field(default_factory=dict)   # touches by channel
    updated_at: Optional[datetime] = None

    # --- recording -------------------------------------------------------
    def on_assigned(self) -> None:
        self.leads_assigned += 1

    def on_responded(self, assigned_at: datetime, responded_at: datetime, channel: str) -> None:
        self.responded += 1
        minutes = max(0.0, (responded_at - assigned_at).total_seconds() / 60)
        self.response_minutes.append(round(minutes, 1))
        self.activity[channel] = self.activity.get(channel, 0) + 1
        self.updated_at = responded_at

    def on_activity(self, channel: str) -> None:
        self.activity[channel] = self.activity.get(channel, 0) + 1

    def on_appointment(self) -> None:
        self.appointments += 1

    def on_estimate(self) -> None:
        self.estimates += 1

    def on_closed(self, *, won: bool, source: LeadSource, project_type: ProjectType,
                  reason: Optional[str], at: datetime) -> None:
        if won:
            self.won += 1
            self.won_by_source[source.value] = self.won_by_source.get(source.value, 0) + 1
            self.won_by_project_type[project_type.value] = (
                self.won_by_project_type.get(project_type.value, 0) + 1)
        else:
            self.lost += 1
            self.lost_by_source[source.value] = self.lost_by_source.get(source.value, 0) + 1
            if reason:
                self.lost_reasons[reason] = self.lost_reasons.get(reason, 0) + 1
        self.updated_at = at

    # --- derived --------------------------------------------------------
    @property
    def closed(self) -> int:
        return self.won + self.lost

    def win_rate(self) -> Optional[float]:
        return round(self.won / self.closed, 3) if self.closed else None

    def response_rate(self) -> Optional[float]:
        return round(self.responded / self.leads_assigned, 3) if self.leads_assigned else None

    def median_response_minutes(self) -> Optional[float]:
        return round(median(self.response_minutes), 1) if self.response_minutes else None

    def best_source(self) -> Optional[str]:
        if not self.won_by_source:
            return None
        return max(sorted(self.won_by_source), key=lambda s: self.won_by_source[s])


class LearningStore:
    """In-memory store of RepLearning by rep_id (Postgres: sales_rep_learning)."""

    def __init__(self):
        self._by_rep: dict[str, RepLearning] = {}

    def get(self, rep_id: str) -> RepLearning:
        if rep_id not in self._by_rep:
            self._by_rep[rep_id] = RepLearning(rep_id=rep_id)
        return self._by_rep[rep_id]

    def all(self) -> list[RepLearning]:
        return [self._by_rep[k] for k in sorted(self._by_rep)]

    def team_summary(self) -> dict:
        reps = self.all()
        won = sum(r.won for r in reps)
        lost = sum(r.lost for r in reps)
        all_resp = [m for r in reps for m in r.response_minutes]
        return {
            "reps": len(reps),
            "leads_assigned": sum(r.leads_assigned for r in reps),
            "responded": sum(r.responded for r in reps),
            "won": won, "lost": lost,
            "win_rate": round(won / (won + lost), 3) if won + lost else None,
            "median_response_minutes": round(median(all_resp), 1) if all_resp else None,
        }
