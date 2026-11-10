import base64
import json

import numpy as np

from models import audio_processor as ap


def test_health_reports_truth_labels(client):
    data = client.get("/health").json()
    assert data["status"] == "healthy"
    assert data["truth_labels"] == {"local_synthesis": "STAGED", "elevenlabs": "STAGED", "twilio": "STAGED",
                                    "human_gate": "SIMULATED_ONLY"}
    assert "Stephanie.ai" in data["disclaimer"]


def test_generate_returns_wav_and_metadata(client):
    resp = client.post("/api/v1/voice/generate", json={"text": "Your permit checklist is ready.", "emotion": "warm",
                                                       "background_audio": "office", "append_disclaimer": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["format"] == "wav"
    audio, rate = ap.decode_wav(base64.b64decode(body["audio"]))
    assert rate == body["metadata"]["sample_rate"] == 16000
    assert abs(audio.size / rate - body["metadata"]["duration"]) < 0.01
    assert body["metadata"]["truth_label"] == "STAGED"
    assert body["metadata"]["characters"] > len("Your permit checklist is ready.")


def test_generate_wav_endpoint(client):
    resp = client.post("/api/v1/voice/generate.wav", json={"text": "Hello."})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["x-truth-label"] == "STAGED"
    assert resp.content[:4] == b"RIFF"


def test_generate_blocks_prohibited_claims(client):
    resp = client.post("/api/v1/voice/generate", json={"text": "Buy now for guaranteed returns and staking rewards."})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "compliance_block"
    assert set(detail["flagged_terms"]) == {"guaranteed returns", "staking rewards"}
    assert "stephanie_ai" in detail["approved_alternatives"]


def test_generate_validation(client):
    assert client.post("/api/v1/voice/generate", json={"text": "Hi", "speed": 3.0}).status_code == 422
    assert client.post("/api/v1/voice/generate", json={"text": "Hi", "emotion": "bored"}).status_code == 422
    assert client.post("/api/v1/voice/generate", json={"text": "Hi", "voice_id": "ghost"}).status_code == 404


def test_voices_settings_pronunciations(client):
    voices = client.get("/api/v1/voices").json()
    assert {v["voice_id"] for v in voices["voices"]} >= {"stephanie_primary", "stephanie_warm"}
    assert "excited" in voices["emotions"] and len(voices["languages"]) == 10
    assert client.get("/api/v1/voices/stephanie_warm").json()["voice"]["name"] == "Stephanie Warm"
    assert client.get("/api/v1/voices/ghost").status_code == 404
    share = client.get("/api/v1/voices/stephanie_primary/share").json()
    assert "share=" in share["share_link"]
    updated = client.put("/api/v1/voice/settings", json={"stability": 0.4, "style": 0.2}).json()
    assert updated["stability"] == 0.4 and updated["style"] == 0.2 and updated["similarity_boost"] == 0.75
    assert client.put("/api/v1/voice/settings", json={"stability": 2}).status_code == 422
    added = client.post("/api/v1/voice/pronunciations", json={"pronunciations": {"Ipswich": "Ips-witch"}}).json()
    assert added["pronunciations"]["Ipswich"] == "Ips-witch"


def test_clone_and_delete_voice(client):
    t = np.arange(16000 * 2) / 16000
    sample = (0.5 * np.sin(2 * np.pi * 240.0 * t)).astype(np.float32)
    wav_b64 = base64.b64encode(ap.encode_wav(sample, 16000)).decode()
    resp = client.post("/api/v1/voice/clone", json={"audio_data": wav_b64, "name": "Field Lead"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["voice_id"] == "cloned_field_lead" and body["profile"]["gender"] == "female"
    gen = client.post("/api/v1/voice/generate", json={"text": "Hello.", "voice_id": "cloned_field_lead"})
    assert gen.status_code == 200
    assert client.delete("/api/v1/voices/cloned_field_lead").json()["deleted"] == "cloned_field_lead"
    assert client.delete("/api/v1/voices/stephanie_primary").status_code == 422
    bad = client.post("/api/v1/voice/clone", json={"audio_data": base64.b64encode(b"nope!").decode(), "name": "x"})
    assert bad.status_code == 422


def test_mix_endpoint(client):
    resp = client.post("/api/v1/voice/mix", json={"text": "Hello.", "voice_a": "stephanie_primary",
                                                  "voice_b": "stephanie_warm", "blend_ratio": 0.3})
    assert resp.status_code == 200 and resp.json()["metadata"]["blend_ratio"] == 0.3


def test_compliance_check_endpoint(client):
    assert client.post("/api/v1/voice/compliance-check", json={"text": "hello"}).json()["ok"] is True
    assert client.post("/api/v1/voice/compliance-check", json={"text": "risk-free"}).json()["flagged_terms"] == ["risk-free"]


def test_call_flow(client):
    resp = client.post("/api/v1/calls", json={"to_number": "+16175551234", "text": "Your permit is ready.",
                                               "record": True, "transcribe": True})
    assert resp.status_code == 200, resp.text
    call = resp.json()
    sid = call["session_id"]
    assert call["status"] == "connected" and call["truth_label"] == "STAGED"
    assert call["prompt_audio_url"].endswith(".wav")
    asset = client.get(call["prompt_audio_url"].split("localhost:8000")[1])
    assert asset.status_code == 200 and asset.content[:4] == b"RIFF"

    twiml = client.post(f"/webhooks/twilio/voice?session_id={sid}", data={"From": "+15550001111"})
    assert twiml.status_code == 200 and "<Play>" in twiml.text and "<Stream" in twiml.text

    assert client.post(f"/api/v1/calls/{sid}/transcription", json={"speaker": "caller", "text": "Thanks."}).status_code == 200
    assert client.get(f"/api/v1/calls/{sid}/transcription").json()["transcript"][0]["text"] == "Thanks."
    assert client.post(f"/api/v1/calls/{sid}/recording/stop").json()["recording_url"].endswith("recording.wav")
    assert client.get(f"/api/v1/calls/{sid}/recording.wav").status_code == 404  # no media captured yet
    assert client.post(f"/api/v1/calls/{sid}/forward", json={"target_number": "+16175559999"}).json()["forwarded"]
    assert client.post(f"/api/v1/calls/{sid}/end").json()["status"] == "completed"
    assert client.get("/api/v1/calls").json()["calls"][0]["session_id"] == sid
    assert client.get("/api/v1/calls/ghost").status_code == 404
    assert client.post("/api/v1/calls", json={"to_number": "617-555"}).status_code == 422


def test_inbound_voice_webhook_uses_ivr_when_present(client):
    plain = client.post("/webhooks/twilio/voice", data={"From": "+15550001111", "CallSid": "CA1"})
    assert "<Say" in plain.text and "Noble Port" in plain.text
    menu = client.post("/api/v1/ivr", json={"prompt": "Welcome to Noble Port.", "options": {"1": "estimates", "2": "permits"}}).json()
    routed = client.post("/webhooks/twilio/voice", data={"From": "+15550001111", "CallSid": "CA2"})
    assert "<Gather" in routed.text and menu["ivr_id"] in routed.text
    choice = client.post(f"/webhooks/twilio/ivr/{menu['ivr_id']}", data={"Digits": "2"})
    assert "Connecting you to permits." in choice.text
    assert client.post(f"/api/v1/ivr/{menu['ivr_id']}/input", json={"digits": "1"}).json()["destination"] == "estimates"
    prompt = client.get(f"/api/v1/ivr/{menu['ivr_id']}/prompt.wav")
    assert prompt.status_code == 200 and prompt.content[:4] == b"RIFF"
    assert client.post("/api/v1/ivr", json={"prompt": "guaranteed returns", "options": {"1": "x"}}).status_code == 422


def test_messages(client):
    resp = client.post("/api/v1/messages", json={"to_number": "+16175551234", "message": "Checklist ready.",
                                                  "append_disclaimer": True})
    assert resp.status_code == 200, resp.text
    assert "Stephanie.ai" in resp.json()["body"]
    assert client.post("/api/v1/messages", json={"to_number": "+16175551234", "message": "passive income!"}).status_code == 422
    inbound = client.post("/webhooks/twilio/sms", data={"From": "+16175550000", "Body": "hi"})
    assert "<Message>" in inbound.text
    log = client.get("/api/v1/messages").json()["messages"]
    assert [m["direction"] for m in log] == ["outbound", "inbound"]


def test_conferences_and_queues(client):
    conf = client.post("/api/v1/conferences", json={"name": "Pilot sync", "max_participants": 3}).json()
    p = client.post(f"/api/v1/conferences/{conf['conference_id']}/participants", json={"participant_number": "+16175551111"}).json()
    assert client.post(f"/api/v1/conferences/{conf['conference_id']}/participants/{p['participant_id']}/mute", json={"muted": True}).json()["muted"]
    assert client.delete(f"/api/v1/conferences/{conf['conference_id']}/participants/{p['participant_id']}").json()["removed"]
    assert client.post("/api/v1/conferences/ghost/participants", json={"participant_number": "+16175551111"}).status_code == 404

    call = client.post("/api/v1/calls", json={"to_number": "+16175551234"}).json()
    queue = client.post("/api/v1/queues", json={"name": "Permit Desk"}).json()
    pos = client.post(f"/api/v1/queues/{queue['queue_id']}/enqueue", json={"session_id": call["session_id"]}).json()
    assert pos["position"] == 1
    assert client.get(f"/api/v1/queues/{queue['queue_id']}/position/{call['session_id']}").json()["position"] == 1
    assert client.post(f"/api/v1/queues/{queue['queue_id']}/dequeue").json()["session"]["status"] == "connected"
    assert client.post(f"/api/v1/queues/{queue['queue_id']}/dequeue").json()["session"] is None


def test_websocket_stream(client):
    with client.websocket_connect("/ws/stream") as ws:
        ws.send_json({"type": "generate", "text": "Streaming test. Second segment!", "chunk_size": 1024})
        chunks = []
        while True:
            msg = ws.receive_json()
            if msg["type"] == "done":
                break
            assert msg["type"] == "audio_chunk", msg
            chunks.append(msg)
        assert chunks and chunks[-1]["final"] is True
        assert {c["segment"] for c in chunks} == {0, 1}
        assert all(len(base64.b64decode(c["data"])) <= 2048 for c in chunks)
        ws.send_json({"type": "generate", "text": "guaranteed returns"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "stop"})


def test_media_stream_websocket_records_audio(client):
    sid = client.post("/api/v1/calls", json={"to_number": "+16175551234"}).json()["session_id"]
    with client.websocket_connect(f"/ws/media-stream?session_id={sid}") as ws:
        ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1", "start": {"customParameters": {"session_id": sid}}}))
        assert ws.receive_json()["status"] == "listening"
        payload = base64.b64encode(bytes([0x7F] * 160)).decode()
        ws.send_text(json.dumps({"event": "media", "media": {"payload": payload}}))
        ws.send_text(json.dumps({"event": "stop"}))
    rec = client.get(f"/api/v1/calls/{sid}/recording.wav")
    assert rec.status_code == 200 and rec.content[:4] == b"RIFF"


def test_admin_token_guards_delete(client, monkeypatch):
    import main

    monkeypatch.setattr(main.config, "ADMIN_TOKEN", "secret")
    assert client.delete("/api/v1/voices/anything").status_code == 403
    assert client.delete("/api/v1/voices/anything", headers={"X-Admin-Token": "secret"}).status_code == 422
