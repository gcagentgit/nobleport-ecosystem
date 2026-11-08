import json

import httpx
import pytest

from steph_email.notify import (DestinationError, ElevenLabsVoice, Notifier, SimulatedTransport, TwilioTransport,
                                VoiceError, normalize_phone, twiml_play, twiml_say)


def test_normalize_phone():
    assert normalize_phone("(978) 555-1234") == "+19785551234"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    with pytest.raises(ValueError):
        normalize_phone("12345")


def test_simulated_sms_is_logged_as_staged(db, settings, transport):
    n = Notifier(db, settings, transport=transport, voice=None)
    rec = n.send_sms("hello", purpose="test")
    assert rec["truth_label"] == "STAGED" and rec["transport"] == "simulated" and rec["to_number"] == "+19785551234"
    assert transport.outbox[-1]["body"] == "hello"
    assert db.list_notifications()[0]["purpose"] == "test" and not n.is_live


def test_destination_guard_only_owner(db, settings, transport):
    n = Notifier(db, settings, transport=transport, voice=None)
    with pytest.raises(DestinationError):
        n.send_sms("hi", to="+15555550199")
    settings.owner_phone = ""
    with pytest.raises(DestinationError):
        n.send_sms("hi")
    assert transport.outbox == [] and db.list_notifications() == []


def test_sms_validation(db, settings, transport):
    n = Notifier(db, settings, transport=transport, voice=None)
    with pytest.raises(ValueError):
        n.send_sms("   ")
    rec = n.send_sms("x" * 2000)
    assert len(rec["body"]) == 1600


def test_place_call_and_twiml(db, settings, transport):
    n = Notifier(db, settings, transport=transport, voice=None)
    rec = n.place_call("http://test.local/webhooks/twilio/brief/2026-09-14", purpose="brief")
    assert rec["kind"] == "call" and transport.outbox[-1]["url"].endswith("2026-09-14")
    assert twiml_say("Good <morning>") == '<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="Polly.Joanna">Good &lt;morning&gt;</Say></Response>'
    assert "<Play>http://x/a.mp3</Play>" in twiml_play("http://x/a.mp3")
    assert "<Play>" in n.twiml_for_brief("script", "http://x/a.mp3") and "<Say" in n.twiml_for_brief("script", None)


def test_twilio_transport_posts_form_and_raises_on_error():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url); seen["body"] = request.content.decode(); seen["auth"] = request.headers.get("authorization", "")
        if "Calls" in seen["url"]:
            return httpx.Response(400, json={"message": "bad number"})
        return httpx.Response(201, json={"sid": "SM123", "status": "queued"})

    t = TwilioTransport("AC1", "tok", transport=httpx.MockTransport(handler))
    assert t.truth_label == "LIVE"
    assert t.send_sms("+19785551234", "+15555550100", "hi") == {"sid": "SM123", "status": "queued"}
    assert seen["url"] == "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json"
    assert "To=%2B19785551234" in seen["body"] and "Body=hi" in seen["body"] and seen["auth"].startswith("Basic ")
    with pytest.raises(Exception) as exc:
        t.create_call("+19785551234", "+15555550100", "http://x")
    assert "400" in str(exc.value)


def _eleven(handler):
    return ElevenLabsVoice("key", "voice1", transport=httpx.MockTransport(handler))


def test_elevenlabs_synthesis_and_metadata():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("xi-api-key")))
        if request.url.path == "/v1/user":
            return httpx.Response(200, json={"subscription": {"tier": "creator", "character_count": 10, "character_limit": 100000}})
        if request.url.path == "/v1/voices/voice1":
            return httpx.Response(200, json={"voice_id": "voice1", "name": "Stephanie", "category": "cloned"})
        body = json.loads(request.content)
        assert body["model_id"] == "eleven_multilingual_v2" and body["text"] == "hello" and request.url.params["output_format"] == "mp3_44100_128"
        return httpx.Response(200, content=b"ID3mp3bytes", headers={"content-type": "audio/mpeg"})

    v = _eleven(handler)
    assert v.check_auth()["tier"] == "creator"
    assert v.get_voice()["name"] == "Stephanie"
    assert v.synthesize("hello") == b"ID3mp3bytes"
    assert all(c[2] == "key" for c in calls)
    with pytest.raises(ValueError):
        v.synthesize("hello", speed=2.0)
    with pytest.raises(ValueError):
        v.synthesize("  ")


def test_elevenlabs_errors_are_loud():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user":
            return httpx.Response(401, json={"detail": "bad key"})
        if request.url.path.startswith("/v1/voices/"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"")

    v = _eleven(handler)
    with pytest.raises(VoiceError, match="401"):
        v.check_auth()
    with pytest.raises(VoiceError, match="not found"):
        v.get_voice()
    with pytest.raises(VoiceError, match="empty audio"):
        v.synthesize("x")
    with pytest.raises(VoiceError):
        ElevenLabsVoice("", "v")
    with pytest.raises(VoiceError):
        ElevenLabsVoice("k", "")


def test_synthesize_without_voice_option_raises(db, settings, transport, tmp_path):
    n = Notifier(db, settings, transport=transport, voice=None)
    with pytest.raises(VoiceError):
        n.synthesize("script", tmp_path / "a.mp3")
    st = n.voice_status()
    assert not st.configured and not st.verified and st.source == "none"


def test_verify_voice_not_configured_writes_evidence(db, settings, transport):
    n = Notifier(db, settings, transport=transport, voice=None)
    ev = n.verify_voice(sample_text="hi")
    assert not ev["verified"] and ev["steps"]["key_present"] is False and "not configured" in ev["error"]
    assert (settings.evidence_dir / "voice-verification.json").exists()


def test_verify_voice_full_flow_then_human_playback(db, settings, transport):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user":
            return httpx.Response(200, json={"subscription": {"tier": "creator"}})
        if request.url.path.startswith("/v1/voices/"):
            return httpx.Response(200, json={"voice_id": "voice1", "name": "Stephanie"})
        return httpx.Response(200, content=b"mp3", headers={"content-type": "audio/mpeg"})

    n = Notifier(db, settings, transport=transport, voice=_eleven(handler))
    ev = n.verify_voice(sample_text="Good morning")
    assert ev["steps"] == {"key_present": True, "auth_ok": True, "voice_resolved": True, "synthesis_ok": True, "playback_confirmed": False}
    assert not ev["verified"] and (settings.evidence_dir / "voice-sample.mp3").read_bytes() == b"mp3"
    assert n.voice_status().configured and not n.voice_status().verified
    ev = n.verify_voice(sample_text="Good morning", confirm_playback=True, device="iPhone 15", note="clear")
    assert ev["verified"] and ev["playback"]["device"] == "iPhone 15"
    assert n.voice_status().verified and n.load_evidence()["verified"]


def test_verify_voice_records_synthesis_failure(db, settings, transport):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user":
            return httpx.Response(200, json={"subscription": {}})
        return httpx.Response(404)

    n = Notifier(db, settings, transport=transport, voice=_eleven(handler))
    ev = n.verify_voice(sample_text="x", confirm_playback=True)
    assert ev["steps"]["auth_ok"] and not ev["steps"]["voice_resolved"] and not ev["verified"] and "not found" in ev["error"]


def test_default_transport_selection(db, tmp_path):
    from steph_email.config import Settings
    s = Settings(data_dir=tmp_path)
    assert isinstance(Notifier(db, s).transport, SimulatedTransport)
    s = Settings(data_dir=tmp_path, twilio_account_sid="AC", twilio_auth_token="t", twilio_from_number="+15555550100",
                 elevenlabs_api_key="k", elevenlabs_voice_id="v")
    n = Notifier(db, s)
    assert isinstance(n.transport, TwilioTransport) and isinstance(n.voice, ElevenLabsVoice) and n.is_live
