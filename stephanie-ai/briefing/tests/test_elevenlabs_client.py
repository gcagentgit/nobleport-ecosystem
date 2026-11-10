import json

import httpx
import pytest

from briefing.elevenlabs_client import ElevenLabsClient, ElevenLabsError, VoiceSettings
from briefing.tests.fake_mp3 import fake_mp3

VOICES = {"voices": [
    {"voice_id": "v_steph", "name": "Stephanie", "category": "cloned", "labels": {"accent": "american"}},
    {"voice_id": "v_other", "name": "Rachel", "category": "premade", "labels": {}},
]}


def make_client(handler, key="sk-test-1234"):
    return ElevenLabsClient(api_key=key, transport=httpx.MockTransport(handler))


def ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["xi-api-key"] == "sk-test-1234"
    path = request.url.path
    if path == "/v1/user":
        return httpx.Response(200, json={"subscription": {"tier": "creator", "character_count": 1000,
                                                          "character_limit": 100000}, "user_id": "u1"})
    if path == "/v1/voices":
        return httpx.Response(200, json=VOICES)
    if path == "/v1/voices/v_steph":
        return httpx.Response(200, json=VOICES["voices"][0])
    if path.startswith("/v1/text-to-speech/v_steph"):
        body = json.loads(request.content)
        assert body["model_id"] == "eleven_multilingual_v2"
        assert request.url.params["output_format"] == "mp3_44100_128"
        assert body["voice_settings"].get("speed", 0.95) == 0.95
        return httpx.Response(200, content=fake_mp3(), headers={"content-type": "audio/mpeg", "request-id": "req_1"})
    return httpx.Response(404, json={"detail": "not found"})


def test_missing_key_raises(monkeypatch):
    with pytest.raises(ElevenLabsError, match="ELEVENLABS_API_KEY"):
        ElevenLabsClient(api_key="")


def test_auth_and_voices():
    c = make_client(ok_handler)
    auth = c.check_auth()
    assert auth["ok"] and auth["tier"] == "creator" and auth["characters_remaining"] == 99000
    assert [v["name"] for v in c.list_voices()] == ["Stephanie", "Rachel"]


def test_resolve_by_id_name_and_failure():
    c = make_client(ok_handler)
    assert c.resolve_voice(voice_id="v_steph")["resolved_by"] == "voice_id"
    assert c.resolve_voice(voice_name="stephanie")["voice_id"] == "v_steph"
    with pytest.raises(ElevenLabsError, match="not chosen"):
        c.resolve_voice()
    with pytest.raises(ElevenLabsError, match="no voice named"):
        c.resolve_voice(voice_name="Nobody")
    with pytest.raises(ElevenLabsError, match="404"):
        c.resolve_voice(voice_id="v_missing")


def test_synthesize_returns_mp3_and_evidence():
    c = make_client(ok_handler)
    r = c.synthesize("Hello Michael.", "v_steph", settings=VoiceSettings(speed=0.95))
    assert r.audio[:3] == b"ID3" and r.request_id == "req_1" and r.characters == 14
    assert r.model_id == "eleven_multilingual_v2" and r.content_type == "audio/mpeg"


def test_bad_key_is_401_not_fallback():
    def handler(request):
        return httpx.Response(401, json={"detail": {"status": "invalid_api_key"}})
    c = make_client(handler)
    with pytest.raises(ElevenLabsError, match="401") as exc:
        c.check_auth()
    assert exc.value.status == 401


def test_speed_range_enforced():
    with pytest.raises(ValueError):
        VoiceSettings(speed=1.5).to_payload()
