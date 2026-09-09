"""Text normalisation, compliance screening and chunking for speech synthesis."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Normalisation tables
# ---------------------------------------------------------------------------

ABBREVIATIONS: Dict[str, str] = {
    "Dr.": "Doctor",
    "Mr.": "Mister",
    "Mrs.": "Missus",
    "Ms.": "Miz",
    "Jr.": "Junior",
    "Sr.": "Senior",
    "St.": "Street",
    "Ave.": "Avenue",
    "Blvd.": "Boulevard",
    "Rd.": "Road",
    "Ste.": "Suite",
    "etc.": "etcetera",
    "e.g.": "for example",
    "i.e.": "that is",
    "vs.": "versus",
    "approx.": "approximately",
    "sq. ft.": "square feet",
    "sq ft": "square feet",
    "GC": "general contractor",
    "AWO": "authorized work order",
    "AHJ": "authority having jurisdiction",
    "NoblePort": "Noble Port",
}

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"), (100, "hundred")]


def number_to_words(n: int) -> str:
    """Spell out a non-negative integer in English."""
    if n < 0:
        return "minus " + number_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, rest = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[rest]}" if rest else "")
    for value, name in _SCALES:
        if n >= value:
            head, rest = divmod(n, value)
            words = f"{number_to_words(head)} {name}"
            return words + (f" {number_to_words(rest)}" if rest else "")
    return str(n)


def _money_to_words(match: re.Match) -> str:
    dollars = int(match.group(1).replace(",", ""))
    cents = match.group(2)
    text = f"{number_to_words(dollars)} dollar{'s' if dollars != 1 else ''}"
    if cents and int(cents) > 0:
        c = int(cents)
        text += f" and {number_to_words(c)} cent{'s' if c != 1 else ''}"
    return text


def _percent_to_words(match: re.Match) -> str:
    return f"{match.group(1)} percent"


def _int_to_words(match: re.Match) -> str:
    raw = match.group(0).replace(",", "")
    # Phone numbers / long identifiers are read digit-by-digit.
    if len(raw) > 6:
        return " ".join(_ONES[int(d)] for d in raw)
    return number_to_words(int(raw))


_MONEY_RE = re.compile(r"\$(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?%")
_INT_RE = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?![\w.])|(?<![\w.])\d+(?![\w.])")
_WS_RE = re.compile(r"\s+")


@dataclass
class ComplianceResult:
    ok: bool
    flagged_terms: List[str] = field(default_factory=list)
    approved_alternatives: Dict[str, str] = field(default_factory=dict)


@dataclass
class TextChunk:
    index: int
    text: str
    pause_after: float  # seconds of silence after this chunk


class TextProcessor:
    """Prepares text for the voice engine.

    Pipeline: custom pronunciation → abbreviation expansion → numbers/money →
    whitespace normalisation. ``check_compliance`` screens the *original* text
    against the ecosystem danger-word list so a prohibited claim never reaches a
    speaker.
    """

    def __init__(self, launch_gates_path: Optional[str] = None, max_length: int = 5000):
        self.max_length = max_length
        self.danger_words: List[str] = []
        self.approved_alternatives: Dict[str, str] = {}
        self.custom_pronunciations: Dict[str, str] = {}
        if launch_gates_path:
            self._load_launch_gates(Path(launch_gates_path))

    # -- setup ---------------------------------------------------------------
    def _load_launch_gates(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            gates = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        danger = gates.get("danger_words", {})
        self.danger_words = [w for w in danger.get("prohibited_in_public_materials", []) if w]
        self.approved_alternatives = dict(danger.get("approved_alternatives", {}))

    # -- compliance -----------------------------------------------------------
    def check_compliance(self, text: str) -> ComplianceResult:
        lowered = text.lower()
        flagged = []
        for term in self.danger_words:
            pattern = r"(?<![\w])" + re.escape(term.lower()) + r"(?![\w])"
            if re.search(pattern, lowered):
                flagged.append(term)
        return ComplianceResult(
            ok=not flagged,
            flagged_terms=flagged,
            approved_alternatives=self.approved_alternatives if flagged else {},
        )

    # -- normalisation --------------------------------------------------------
    def apply_pronunciations(self, text: str, custom: Optional[Dict[str, str]] = None) -> str:
        table = dict(self.custom_pronunciations)
        if custom:
            table.update(custom)
        # Longest keys first so "NoblePort Systems" beats "NoblePort".
        for word in sorted(table, key=len, reverse=True):
            text = re.sub(r"(?<![\w])" + re.escape(word) + r"(?![\w])", table[word], text)
        return text

    def expand_abbreviations(self, text: str) -> str:
        for abbr in sorted(ABBREVIATIONS, key=len, reverse=True):
            full = ABBREVIATIONS[abbr]
            if abbr.endswith("."):
                text = re.sub(r"(?<![\w])" + re.escape(abbr), full, text)
            else:
                text = re.sub(r"(?<![\w])" + re.escape(abbr) + r"(?![\w])", full, text)
        return text

    def expand_numbers(self, text: str) -> str:
        text = _MONEY_RE.sub(_money_to_words, text)
        text = _PERCENT_RE.sub(_percent_to_words, text)
        text = _INT_RE.sub(_int_to_words, text)
        return text

    def normalize(self, text: str, custom_pronunciations: Optional[Dict[str, str]] = None) -> str:
        if len(text) > self.max_length:
            raise ValueError(f"text exceeds MAX_TEXT_LENGTH ({self.max_length} characters)")
        text = self.apply_pronunciations(text, custom_pronunciations)
        text = self.expand_abbreviations(text)
        text = self.expand_numbers(text)
        text = _WS_RE.sub(" ", text).strip()
        return text

    # -- segmentation ---------------------------------------------------------
    _SENTENCE_RE = re.compile(r"[^.!?;:,\n]+[.!?;:,\n]?")

    def chunk(self, text: str, max_words: int = 12) -> List[TextChunk]:
        """Split text into prosodic chunks with a pause length for each."""
        chunks: List[TextChunk] = []
        for piece in self._SENTENCE_RE.findall(text):
            piece = piece.strip()
            if not piece:
                continue
            terminator = piece[-1]
            pause = {".": 0.35, "!": 0.35, "?": 0.4, ";": 0.25, ":": 0.25, ",": 0.15, "\n": 0.4}.get(
                terminator, 0.1
            )
            words = piece.split()
            for i in range(0, len(words), max_words):
                sub = " ".join(words[i : i + max_words])
                last = i + max_words >= len(words)
                chunks.append(TextChunk(index=len(chunks), text=sub, pause_after=pause if last else 0.08))
        return chunks

    @staticmethod
    def words(text: str) -> Iterable[str]:
        return re.findall(r"[A-Za-z0-9'’-]+", text)

    @staticmethod
    def syllables(word: str) -> int:
        """Cheap English syllable estimate (vowel groups, silent trailing e)."""
        w = word.lower().strip("'’-")
        if not w:
            return 0
        groups = re.findall(r"[aeiouy]+", w)
        count = len(groups)
        # Silent trailing "e" (permit-e → no), but "-ie" / "-le" / "-ee" keep their beat.
        if w.endswith("e") and not w.endswith(("le", "ee", "ie")) and count > 1:
            count -= 1
        return max(1, count)
