from briefing import voice_standard as vs


def test_prepare_script_applies_trade_terms_and_numbers():
    out = vs.prepare_script("The AHJ needs the GC at 12 Main St. Estimate $4,500 for HVAC.\n\nNext item.")
    assert "A-H-J" in out and "G-C" in out and "H-vack" in out
    assert "twelve Main Street" in out
    assert "four thousand five hundred dollars" in out
    assert "\n\n" in out


def test_delivery_score_and_speed_suggestion():
    s = vs.score_delivery("one two three four five six seven eight nine ten", 4.0)   # 150 wpm
    assert s.words == 10 and abs(s.wpm - 150) < 0.1 and s.within_target
    slow = vs.score_delivery(" ".join(["w"] * 100), 60.0)                             # 100 wpm
    assert not slow.within_target and slow.suggested_speed() == 1.2                  # clamped
    fast = vs.score_delivery(" ".join(["w"] * 200), 60.0)                             # 200 wpm
    assert fast.suggested_speed() == 0.72


def test_compliance_issues_use_launch_gates():
    assert vs.compliance_issues("We promise guaranteed returns.") == ["guaranteed returns"]
    assert vs.compliance_issues("Your permit checklist is ready.") == []
