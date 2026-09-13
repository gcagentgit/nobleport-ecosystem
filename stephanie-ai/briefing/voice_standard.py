"""Stephanie Voice Profile & Delivery Parameters — the parts that are code.

The written standard (v1.0) sets a 145 words-per-minute delivery target, a
pause map, trade-term pronunciations, numeric handling rules and preset
scripts. This module encodes the measurable pieces so a synthesis run can be
scored against them instead of eyeballed.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

# Reuse the backend text processor (numbers → words, abbreviations, compliance screen).
_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from models.text_processor import TextProcessor  # noqa: E402

TARGET_WPM = 145
WPM_TOLERANCE = 10          # ±10 WPM passes calibration
LAUNCH_GATES = _BACKEND.parent.parent / "core" / "config" / "launch-gates.json"

# Trade-term pronunciation guide (spelled the way the voice should say them).
TRADE_TERMS: Dict[str, str] = {
    "AHJ": "A-H-J",
    "AWO": "A-W-O",
    "GC": "G-C",
    "RFI": "R-F-I",
    "RFP": "R-F-P",
    "CO": "C-O",
    "HVAC": "H-vack",
    "sq ft": "square feet",
    "sq. ft.": "square feet",
    "lin ft": "linear feet",
    "NoblePort": "Noble Port",
    "Ipswich": "Ips-witch",
    "Newburyport": "Newbury-port",
    "USDC": "U-S-D-C",
    "NBPT": "N-B-P-T",
    "ERC-3643": "E-R-C thirty-six forty-three",
    "zkSBT": "zee-kay S-B-T",
    "Stephanie.ai": "Stephanie A-I",
}

# Pause map: punctuation → SSML-free "breath" cues ElevenLabs respects (ellipsis / line breaks).
PAUSE_MAP = {"section": "\n\n", "item": "\n", "beat": " … "}

PRESET_SCRIPTS: Dict[str, str] = {
    "calibration": (
        "Good morning, Michael. This is Stephanie with your NoblePort briefing for today. "
        "Three items need you before noon, two items closed overnight, and the Ipswich permit "
        "checklist is ready for human review. The estimate for twelve Main Street totals "
        "forty-eight thousand five hundred dollars. I will read the details now."
    ),
    "greeting": "Good morning, Michael. This is Stephanie. Here is your NoblePort morning briefing.",
    "sign_off": "That is the briefing. Every item above is staged for your review; nothing has been sent or signed.",
}

_WORD_RE = re.compile(r"[A-Za-z0-9'’-]+")


@dataclass
class DeliveryScore:
    words: int
    duration_s: float
    wpm: float
    target_wpm: int = TARGET_WPM
    tolerance: int = WPM_TOLERANCE

    @property
    def within_target(self) -> bool:
        return abs(self.wpm - self.target_wpm) <= self.tolerance

    def suggested_speed(self, current_speed: float = 1.0) -> float:
        """ElevenLabs speed multiplier that would land on the target (clamped 0.7–1.2)."""
        if self.wpm <= 0:
            return current_speed
        return round(max(0.7, min(1.2, current_speed * self.target_wpm / self.wpm)), 2)

    def as_dict(self) -> dict:
        return {"words": self.words, "duration_s": round(self.duration_s, 2), "wpm": round(self.wpm, 1),
                "target_wpm": self.target_wpm, "tolerance": self.tolerance, "within_target": self.within_target,
                "suggested_speed": self.suggested_speed()}


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def score_delivery(text: str, duration_s: float) -> DeliveryScore:
    words = word_count(text)
    wpm = (words / duration_s * 60.0) if duration_s > 0 else 0.0
    return DeliveryScore(words=words, duration_s=duration_s, wpm=wpm)


def prepare_script(text: str, processor: Optional[TextProcessor] = None) -> str:
    """Apply the standard's pronunciation guide and numeric rules to briefing text."""
    tp = processor or TextProcessor(str(LAUNCH_GATES) if LAUNCH_GATES.is_file() else None, max_length=20000)
    text = tp.apply_pronunciations(text, TRADE_TERMS)
    text = tp.expand_abbreviations(text)
    text = tp.expand_numbers(text)
    # Collapse whitespace inside paragraphs but keep paragraph breaks (the pause map).
    paragraphs = [re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    return "\n\n".join(p for p in paragraphs if p)


def compliance_issues(text: str) -> List[str]:
    tp = TextProcessor(str(LAUNCH_GATES) if LAUNCH_GATES.is_file() else None, max_length=20000)
    return tp.check_compliance(text).flagged_terms
