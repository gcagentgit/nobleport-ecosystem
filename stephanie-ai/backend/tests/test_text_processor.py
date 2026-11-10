from pathlib import Path

from models.text_processor import TextProcessor, number_to_words

GATES = Path(__file__).resolve().parents[3] / "core" / "config" / "launch-gates.json"


def test_number_to_words():
    assert number_to_words(0) == "zero"
    assert number_to_words(21) == "twenty-one"
    assert number_to_words(4500) == "four thousand five hundred"
    assert number_to_words(1_000_001) == "one million one"


def test_normalize_expands_money_percent_and_abbreviations():
    tp = TextProcessor()
    out = tp.normalize("Dr. Smith owes $4,500.50 and 12% on 12 Main St.")
    assert "Doctor Smith" in out
    assert "four thousand five hundred dollars and fifty cents" in out
    assert "twelve percent" in out
    assert "twelve Main Street" in out


def test_long_numbers_are_read_digit_by_digit():
    tp = TextProcessor()
    assert tp.normalize("call 6175551234") == "call six one seven five five five one two three four"


def test_custom_pronunciations_longest_first():
    tp = TextProcessor()
    out = tp.apply_pronunciations("NoblePort Systems and NoblePort", {"NoblePort": "Noble Port", "NoblePort Systems": "Noble Port Inc"})
    assert out == "Noble Port Inc and Noble Port"


def test_max_length_enforced():
    tp = TextProcessor(max_length=10)
    try:
        tp.normalize("x" * 11)
    except ValueError as exc:
        assert "MAX_TEXT_LENGTH" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_compliance_uses_launch_gates():
    tp = TextProcessor(str(GATES))
    assert tp.danger_words, "launch-gates danger words should load"
    bad = tp.check_compliance("We promise guaranteed returns and passive income.")
    assert not bad.ok
    assert "guaranteed returns" in bad.flagged_terms
    assert "passive income" in bad.flagged_terms
    assert "stephanie_ai" in bad.approved_alternatives
    good = tp.check_compliance("Your permit checklist is ready for human review.")
    assert good.ok and good.flagged_terms == []


def test_compliance_matches_whole_words_only():
    tp = TextProcessor(str(GATES))
    # "ICO" is prohibited; "Mexico" must not trigger it.
    assert tp.check_compliance("Shipping to Mexico next week.").ok


def test_chunking_and_pauses():
    tp = TextProcessor()
    chunks = tp.chunk("Hello there. How are you? Fine, thanks")
    texts = [c.text for c in chunks]
    assert texts == ["Hello there.", "How are you?", "Fine,", "thanks"]
    assert chunks[0].pause_after == 0.35
    assert chunks[1].pause_after == 0.4
    assert chunks[2].pause_after == 0.15


def test_syllables():
    assert TextProcessor.syllables("permit") == 2
    assert TextProcessor.syllables("Stephanie") == 3
    assert TextProcessor.syllables("the") == 1
