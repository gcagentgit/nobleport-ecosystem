import base64
import json

import numpy as np
import pytest

from services.twilio_features import (HumanGateError, SimulatedTransport, TwiML, TwilioFeatures, validate_e164)


@pytest.fixture
def twilio():
    return TwilioFeatures(from_number="+15550001000", public_base_url="https://voice.example.com")


def test_validate_e164():
    assert validate_e164("+1 (617) 555-1234") == "+16175551234"
    with pytest.raises(ValueError):
        validate_e164("617-555-1234")


async def test_call_lifecycle_with_webhooks(twilio):
    events = []

    async def handler(payload):
        events.append(payload["event"])

    twilio.register_webhook("*", handler)
    session = await twilio.make_voice_call("+16175551234")
    assert session.status == "connected" and session.truth_label == "STAGED"
    assert session.provider_sid.startswith("CA")
    assert await twilio.start_recording(session.session_id)
    url = await twilio.stop_recording(session.session_id)
    assert url.endswith(f"/api/v1/calls/{session.session_id}/recording.wav")
    assert await twilio.forward_call(session.session_id, "+16175559999")
    ended = await twilio.end_call(session.session_id)
    assert ended.status == "completed"
    assert events == ["call.initiated", "call.forwarded", "call.completed"]


async def test_sms_validation_and_log(twilio):
    record = await twilio.send_sms("+16175551234", "Permit checklist ready.")
    assert record["status"] == "queued" and record["segments"] == 1
    assert twilio.message_log[-1] is record
    with pytest.raises(ValueError):
        await twilio.send_sms("+16175551234", "   ")
    with pytest.raises(ValueError):
        await twilio.send_sms("+16175551234", "x" * 1601)
    inbound = await twilio.receive_sms("+16175550000", "hello")
    assert inbound["direction"] == "inbound"


async def test_transcription(twilio):
    session = await twilio.make_voice_call("+16175551234")
    await twilio.start_transcription(session.session_id)
    await twilio.add_transcript_line(session.session_id, "caller", "I need a permit update.")
    lines = await twilio.get_transcription(session.session_id)
    assert lines[0]["text"] == "I need a permit update."


async def test_conference(twilio):
    conf = await twilio.create_conference("Ipswich pilot", max_participants=2)
    p1 = await twilio.add_to_conference(conf.conference_id, "+16175551111")
    await twilio.add_to_conference(conf.conference_id, "+16175552222")
    with pytest.raises(ValueError):
        await twilio.add_to_conference(conf.conference_id, "+16175553333")
    assert await twilio.mute_participant(conf.conference_id, p1["participant_id"])
    assert conf.participants[p1["participant_id"]]["muted"]
    assert await twilio.remove_from_conference(conf.conference_id, p1["participant_id"])
    assert not await twilio.mute_participant(conf.conference_id, "ghost")


async def test_queue_positions(twilio):
    queue = await twilio.create_queue("Permit Desk", max_size=2)
    assert queue.queue_id == "queue_permit_desk"
    a = await twilio.make_voice_call("+16175551111")
    b = await twilio.make_voice_call("+16175552222")
    assert await twilio.add_to_queue(queue.queue_id, a.session_id) == 1
    assert await twilio.add_to_queue(queue.queue_id, b.session_id) == 2
    assert await twilio.get_queue_position(queue.queue_id, b.session_id) == 2
    c = await twilio.make_voice_call("+16175553333")
    with pytest.raises(ValueError):
        await twilio.add_to_queue(queue.queue_id, c.session_id)
    nxt = await twilio.dequeue_next(queue.queue_id)
    assert nxt.session_id == a.session_id and nxt.status == "connected"
    assert await twilio.get_queue_position(queue.queue_id, b.session_id) == 1


async def test_ivr_menu_and_twiml(twilio):
    menu = await twilio.create_ivr_menu("Welcome to Noble Port.", {"1": "estimates", "2": "permits"})
    assert await twilio.handle_ivr_input(menu.ivr_id, "2") == "permits"
    assert await twilio.handle_ivr_input(menu.ivr_id, "9") == "operator"
    twiml = twilio.ivr_twiml(menu.ivr_id, "call_x")
    assert twiml.startswith('<?xml version="1.0"')
    assert "<Gather" in twiml and "Press 1 for estimates." in twiml
    assert f"/webhooks/twilio/ivr/{menu.ivr_id}?session_id=call_x" in twiml
    with pytest.raises(ValueError):
        await twilio.create_ivr_menu("x", {"12": "bad"})


def test_twiml_escapes():
    assert TwiML.say("Tom & Jerry <3") == '<Say voice="Polly.Joanna">Tom &amp; Jerry &lt;3</Say>'


async def test_media_stream_capture_and_recording(twilio):
    session = await twilio.make_voice_call("+16175551234")
    stream_id = await twilio.create_media_stream(session.session_id)
    assert twilio.media_stream_url(session.session_id).startswith("wss://voice.example.com/ws/media-stream")
    start = await twilio.process_media_message(json.dumps({"event": "start", "streamSid": "MZ1"}), stream_id)
    assert start["event"] == "started"
    payload = base64.b64encode(bytes([0xFF] * 160)).decode()
    ack = await twilio.process_media_message(json.dumps({"event": "media", "media": {"payload": payload}}), stream_id)
    assert ack["frames_received"] == 1
    assert await twilio.process_media_message("not json", stream_id) is None
    wav = twilio.recording_audio(session.session_id)
    assert wav and wav[:4] == b"RIFF"
    out = twilio.outbound_media_messages(np.zeros(1600, dtype=np.float32), 16000, "MZ1")
    assert len(out) == 6 and json.loads(out[-1])["event"] == "mark"


class LiveStub:
    name = "stub_live"
    truth_label = "LIVE"

    def __init__(self):
        self.calls = []

    async def create_call(self, to, from_, twiml_url):
        self.calls.append(("call", to, twiml_url))
        return {"sid": "CA123", "status": "queued"}

    async def send_sms(self, to, from_, body):
        self.calls.append(("sms", to, body))
        return {"sid": "SM123", "status": "queued"}

    async def update_call(self, sid, **params):
        self.calls.append(("update", sid, params))
        return {"sid": sid}


async def test_human_gate_blocks_live_outbound_without_token():
    stub = LiveStub()
    gated = TwilioFeatures(from_number="+15550001000", transport=stub, human_approval_token="approve-me")
    with pytest.raises(HumanGateError):
        await gated.make_voice_call("+16175551234")
    with pytest.raises(HumanGateError):
        await gated.send_sms("+16175551234", "hi", human_approval="wrong")
    assert stub.calls == []
    session = await gated.make_voice_call("+16175551234", human_approval="approve-me")
    assert session.truth_label == "LIVE" and session.status == "queued"
    assert await gated.forward_call(session.session_id, "+16175559999")
    assert stub.calls[-1][0] == "update" and "<Dial>" in stub.calls[-1][2]["Twiml"]


async def test_live_transport_without_configured_token_fails_closed():
    gated = TwilioFeatures(transport=LiveStub(), human_approval_token=None)
    with pytest.raises(HumanGateError):
        await gated.send_sms("+16175551234", "hi", human_approval="anything")


def test_simulated_transport_records_outbox():
    transport = SimulatedTransport()
    assert transport.outbox == []
