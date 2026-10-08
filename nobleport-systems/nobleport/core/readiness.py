"""Feature Readiness Matrix — LIVE / PILOT / SIMULATION / ARCHITECTURE."""

from enum import Enum


class Readiness(str, Enum):
    LIVE = "LIVE"
    PILOT = "PILOT"
    SIMULATION = "SIMULATION"
    ARCHITECTURE = "ARCHITECTURE"

    @property
    def rank(self) -> int:
        return {
            Readiness.LIVE: 4,
            Readiness.PILOT: 3,
            Readiness.SIMULATION: 2,
            Readiness.ARCHITECTURE: 1,
        }[self]

    def permits_state_change(self) -> bool:
        """Only LIVE and PILOT connections may mutate production state."""
        return self in (Readiness.LIVE, Readiness.PILOT)
