import json
from datetime import date
from pathlib import Path

import httpx

from briefing import build_briefing, verify_voice
from briefing.elevenlabs_client import ElevenLabsClient
from briefing.tests.fake_mp3 import fake_mp3
from briefing.tests.test_elevenlabs_client import ok_handler

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "briefing-2026-09-13.md"


def live_factory():
    return ElevenLabsClient(api_key="sk-test-1234", transport=httpx.MockTransport(ok_handler))


def test_verify_without_key_fails_closed(tmp_path):
    ev = verify_voice.run_automated(None, None, None, None, "hello", evidence_dir=tmp_path)
    assert ev["steps"]["key_present"]["status"] == "fail"
    assert ev["verdict"].startswith("NOT VERIFIED")
    assert verify_voice.exit_code(ev) == 1
    assert list(tmp_path.glob("voice-verification-*.json"))


def test_verify_full_chain_then_phone_test(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test-1234")
    ev = verify_voice.run_automated("v_steph", None, None, 0.95, "Hello Michael, this is a test.",
                                    evidence_dir=tmp_path, client_factory=live_factory)
    steps = ev["steps"]
    assert [steps[s]["status"] for s in ("key_present", "auth", "voice_resolved", "synthesis")] == ["pass"] * 4
    assert steps["synthesis"]["sha256"] and Path(steps["synthesis"]["mp3_path"]).is_file()
    assert steps["synthesis"]["delivery"]["words"] == 6
    assert ev["verdict"].startswith("SYNTHESIS OK") and verify_voice.exit_code(ev) == 2

    ev2 = verify_voice.record_phone_test("pass", "iPhone 15 Pro", "clear, correct voice", evidence_dir=tmp_path)
    assert ev2["steps"]["phone_playback"]["status"] == "pass"
    assert ev2["verdict"].startswith("VERIFIED") and verify_voice.exit_code(ev2) == 0
    assert verify_voice.load_latest_evidence(tmp_path)["verdict"].startswith("VERIFIED")


def test_verify_unchosen_voice_lists_account_voices(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test-1234")
    ev = verify_voice.run_automated(None, None, None, None, "hi", evidence_dir=tmp_path, client_factory=live_factory)
    assert ev["steps"]["voice_resolved"]["status"] == "fail"
    assert "Stephanie (v_steph)" in ev["steps"]["voice_resolved"]["reason"]
    assert ev["steps"]["synthesis"]["status"] == "skipped"


def test_parse_and_compose_script():
    brief = build_briefing.parse_briefing(SAMPLE.read_text())
    assert brief["headline"].startswith("Two things need you")
    assert [s["title"] for s in brief["sections"]] == ["Needs attention", "Resolved", "Voice status"]
    script = build_briefing.compose_script(brief, "Michael", date(2026, 9, 13))
    assert script.startswith("Good morning, Michael. This is Stephanie with your Noble Port briefing for Sunday, September 13.")
    assert "forty-eight thousand five hundred dollars" in script
    assert "A-H-J" in script and "nine AM" in script
    assert script.rstrip().endswith("nothing has been sent or signed.")


def test_build_live_page(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test-1234")
    out = tmp_path / "brief.html"
    meta = build_briefing.build(SAMPLE, out, date(2026, 9, 13), "Michael", "v_steph", None, None, None,
                                allow_staged=False, no_audio=False, allow_flagged=False, client_factory=live_factory)
    page = out.read_text()
    assert meta["status"] == "live" and meta["label"] == "LIVE"
    assert 'src="data:audio/mpeg;base64,' in page
    assert "Stephanie's voice · ElevenLabs · Stephanie (v_steph)" in page
    assert 'id="playBtn"' in page and "disabled" not in page.split('id="playBtn"')[1][:20]
    assert out.with_suffix(".mp3").is_file() and json.loads(out.with_suffix(".json").read_text())["voice_id"] == "v_steph"


def test_build_without_key_has_no_audio_and_says_so(tmp_path):
    out = tmp_path / "brief.html"
    meta = build_briefing.build(SAMPLE, out, date(2026, 9, 13), "Michael", None, None, None, None,
                                allow_staged=False, no_audio=False, allow_flagged=False)
    page = out.read_text()
    assert meta["status"] == "none"
    assert "<audio" not in page and 'id="playBtn" disabled' in page
    assert "No voice on this page" in page and "ELEVENLABS_API_KEY is not set" in page


def test_build_staged_is_labelled(tmp_path):
    out = tmp_path / "brief.html"
    meta = build_briefing.build(SAMPLE, out, date(2026, 9, 13), "Michael", None, None, None, None,
                                allow_staged=True, no_audio=False, allow_flagged=False)
    page = out.read_text()
    assert meta["status"] == "staged"
    assert "NOT Stephanie" in page and 'data:audio/wav;base64,' in page


def test_build_blocks_prohibited_terms(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("# Guaranteed returns for everyone\n- passive income")
    try:
        build_briefing.build(bad, tmp_path / "x.html", date.today(), "M", None, None, None, None, False, True, False)
    except SystemExit as exc:
        assert "guaranteed returns" in str(exc)
    else:
        raise AssertionError("expected compliance block")
