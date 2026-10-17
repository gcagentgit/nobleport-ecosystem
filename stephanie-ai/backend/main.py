"""Stephanie.ai Voice — FastAPI backend.

    cd stephanie-ai/backend
    python main.py                       # or: uvicorn main:app --host 0.0.0.0 --port 8000

REST under /api/v1, WebSockets at /ws/stream (browser/desktop streaming) and
/ws/media-stream (Twilio <Stream>). Every response that depends on an external
provider carries a ``truth_label`` (LIVE / STAGED).
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import uvicorn
from fastapi import (Depends, FastAPI, Form, Header, HTTPException, Query, Request, Response, WebSocket,
                     WebSocketDisconnect, status)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import StephanieConfig
from models import audio_processor as ap
from models.voice_engine import EMOTION_PARAMS
from services.elevenlabs_features import ElevenLabsFeatures
from services.stephanie_ai import ComplianceError, StephanieAI
from services.twilio_features import HumanGateError

config = StephanieConfig()
logging.basicConfig(level=logging.DEBUG if config.DEBUG else logging.INFO)
log = logging.getLogger("stephanie.api")

app = FastAPI(
    title="Stephanie.ai Voice",
    description="NoblePort Systems voice generation and telephony layer. "
                "Combines ElevenLabs-style synthesis features with Twilio-style telephony "
                "behind NoblePort's human-gated governance model.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

stephanie = StephanieAI(config)
app.state.stephanie = stephanie


def get_stephanie() -> StephanieAI:
    return app.state.stephanie


def require_admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    if not config.ADMIN_TOKEN:
        return  # no admin token configured → open in dev
    if x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin token required")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class VoiceGenerationRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    voice_id: str = "stephanie_primary"
    emotion: str = "neutral"
    speed: float = Field(1.0, ge=0.5, le=2.0)
    enhance_audio: bool = True
    language: str = "en-US"
    stability: Optional[float] = Field(None, ge=0.0, le=1.0)
    similarity_boost: Optional[float] = Field(None, ge=0.0, le=1.0)
    style: Optional[float] = Field(None, ge=0.0, le=1.0)
    emotion_intensity: float = Field(1.0, ge=0.0, le=1.5)
    background_audio: Optional[str] = Field(None, description="office | jobsite | hold_music")
    background_volume: float = Field(0.1, ge=0.0, le=1.0)
    custom_pronunciations: Optional[Dict[str, str]] = None
    append_disclaimer: bool = False
    output_format: str = Field("wav", pattern="^(wav|mp3)$")
    seed: Optional[int] = None


class VoiceCloneRequest(BaseModel):
    audio_data: str = Field(..., description="Base64 WAV (preferred) or raw float32 PCM")
    name: str = Field(..., min_length=1, max_length=60)
    description: str = ""


class VoiceSettingsRequest(BaseModel):
    stability: Optional[float] = Field(None, ge=0.0, le=1.0)
    similarity_boost: Optional[float] = Field(None, ge=0.0, le=1.0)
    style: Optional[float] = Field(None, ge=0.0, le=1.0)
    use_speaker_boost: Optional[bool] = None


class PronunciationRequest(BaseModel):
    pronunciations: Dict[str, str]


class MixRequest(BaseModel):
    text: str = Field(..., min_length=1)
    voice_a: str
    voice_b: str
    blend_ratio: float = Field(0.5, ge=0.0, le=1.0)
    emotion: str = "neutral"


class CallRequest(BaseModel):
    to_number: str
    text: Optional[str] = None
    voice_id: Optional[str] = None
    record: bool = False
    transcribe: bool = False


class MessageRequest(BaseModel):
    to_number: str
    message: str = Field(..., min_length=1, max_length=1600)
    append_disclaimer: bool = False


class ConferenceRequest(BaseModel):
    name: str
    max_participants: int = Field(10, ge=2, le=250)


class ParticipantRequest(BaseModel):
    participant_number: str
    session_id: Optional[str] = None


class MuteRequest(BaseModel):
    muted: bool = True


class QueueRequest(BaseModel):
    name: str
    max_size: int = Field(50, ge=1, le=1000)


class EnqueueRequest(BaseModel):
    session_id: str


class IvrRequest(BaseModel):
    prompt: str
    options: Dict[str, str]
    voice_id: str = "stephanie_dispatch"
    fallback: str = "operator"


class IvrInputRequest(BaseModel):
    digits: str
    session_id: Optional[str] = None


class ForwardRequest(BaseModel):
    target_number: str


class TranscriptLineRequest(BaseModel):
    speaker: str
    text: str
    confidence: float = Field(1.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ComplianceError):
        return HTTPException(status_code=422, detail={
            "error": "compliance_block", "message": str(exc), "flagged_terms": exc.flagged,
            "approved_alternatives": exc.alternatives,
        })
    if isinstance(exc, HumanGateError):
        return HTTPException(status_code=403, detail={"error": "human_gate", "message": str(exc)})
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'\""))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    log.exception("unhandled error")
    return HTTPException(status_code=500, detail=str(exc))


def _audio_payload(audio: np.ndarray, sample_rate: int, output_format: str = "wav") -> Dict[str, Any]:
    wav = ap.encode_wav(audio, sample_rate)
    if output_format == "mp3":
        mp3 = ap.encode_mp3(wav)
        if mp3:
            return {"audio": base64.b64encode(mp3).decode(), "format": "mp3", "encoding": "base64"}
        log.warning("ffmpeg not available; returning wav instead of mp3")
    return {"audio": base64.b64encode(wav).decode(), "format": "wav", "encoding": "base64"}


# ---------------------------------------------------------------------------
# Voice generation
# ---------------------------------------------------------------------------

@app.post("/api/v1/voice/generate")
async def generate_voice(request: VoiceGenerationRequest, svc: StephanieAI = Depends(get_stephanie)):
    """Generate speech from text (ElevenLabs-style settings, NoblePort compliance screen)."""
    try:
        result = await svc.generate_voice(request.text, request.model_dump(exclude={"text", "output_format"}))
    except Exception as exc:  # noqa: BLE001 — mapped to HTTP below
        raise _http_error(exc)
    payload = _audio_payload(result["audio"], result["metadata"]["sample_rate"], request.output_format)
    return {"status": "success", **payload, "metadata": result["metadata"]}


@app.post("/api/v1/voice/generate.wav")
async def generate_voice_wav(request: VoiceGenerationRequest, svc: StephanieAI = Depends(get_stephanie)):
    """Same as /generate but streams the WAV bytes directly (handy for curl / <audio>)."""
    try:
        result = await svc.generate_voice(request.text, request.model_dump(exclude={"text", "output_format"}))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
    return Response(content=ap.encode_wav(result["audio"], result["metadata"]["sample_rate"]), media_type="audio/wav",
                    headers={"X-Truth-Label": result["metadata"]["truth_label"]})


@app.post("/api/v1/voice/clone")
async def clone_voice(request: VoiceCloneRequest, svc: StephanieAI = Depends(get_stephanie)):
    try:
        audio, rate = ap.decode_audio_bytes(base64.b64decode(request.audio_data))
        return await svc.clone_voice(audio, request.name, rate, request.description)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.post("/api/v1/voice/mix")
async def mix_voices(request: MixRequest, svc: StephanieAI = Depends(get_stephanie)):
    try:
        svc.screen_text(request.text)
        audio = svc.elevenlabs.mix_voice_profiles(request.text, request.voice_a, request.voice_b,
                                                  request.blend_ratio, emotion=request.emotion)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
    return {"status": "success", **_audio_payload(audio, svc.config.SAMPLE_RATE),
            "metadata": {"voice_a": request.voice_a, "voice_b": request.voice_b, "blend_ratio": request.blend_ratio,
                         "sample_rate": svc.config.SAMPLE_RATE, "duration": ap.duration_seconds(audio, svc.config.SAMPLE_RATE)}}


@app.post("/api/v1/voice/compliance-check")
async def compliance_check(request: Dict[str, str], svc: StephanieAI = Depends(get_stephanie)):
    result = svc.text_processor.check_compliance(request.get("text", ""))
    return {"ok": result.ok, "flagged_terms": result.flagged_terms, "approved_alternatives": result.approved_alternatives}


@app.get("/api/v1/voice/brand-intro.wav")
async def brand_intro(svc: StephanieAI = Depends(get_stephanie)):
    data = svc.brand_intro_wav()
    if not data:
        raise HTTPException(status_code=404, detail="brand intro asset not present")
    return Response(content=data, media_type="audio/wav")


# ---------------------------------------------------------------------------
# Voices, settings, pronunciation
# ---------------------------------------------------------------------------

@app.get("/api/v1/voices")
async def get_voices(svc: StephanieAI = Depends(get_stephanie)):
    return {"voices": svc.get_available_voices(), "emotions": ElevenLabsFeatures.emotions(),
            "languages": ElevenLabsFeatures.supported_languages()}


@app.get("/api/v1/voices/{voice_id}")
async def get_voice(voice_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return svc.elevenlabs.manage_voice_library("get", voice_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.get("/api/v1/voices/{voice_id}/share")
async def share_voice(voice_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return svc.elevenlabs.manage_voice_library("share", voice_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.delete("/api/v1/voices/{voice_id}", dependencies=[Depends(require_admin)])
async def delete_voice(voice_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return svc.elevenlabs.manage_voice_library("delete", voice_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.get("/api/v1/voice/settings")
async def get_voice_settings(svc: StephanieAI = Depends(get_stephanie)):
    return svc.elevenlabs.settings_dict()


@app.put("/api/v1/voice/settings")
async def update_voice_settings(request: VoiceSettingsRequest, svc: StephanieAI = Depends(get_stephanie)):
    try:
        svc.elevenlabs.adjust_voice_settings(request.stability, request.similarity_boost, request.style,
                                             request.use_speaker_boost)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
    return svc.elevenlabs.settings_dict()


@app.post("/api/v1/voice/pronunciations")
async def add_pronunciations(request: PronunciationRequest, svc: StephanieAI = Depends(get_stephanie)):
    return {"pronunciations": svc.elevenlabs.register_pronunciations(request.pronunciations)}


@app.get("/api/v1/voice/pronunciations")
async def list_pronunciations(svc: StephanieAI = Depends(get_stephanie)):
    return {"pronunciations": dict(svc.text_processor.custom_pronunciations)}


# ---------------------------------------------------------------------------
# Telephony — calls & SMS
# ---------------------------------------------------------------------------

@app.post("/api/v1/calls")
async def make_call(request: CallRequest, svc: StephanieAI = Depends(get_stephanie),
                    x_human_approval: Optional[str] = Header(default=None)):
    try:
        return await svc.make_call(request.to_number, request.text, request.voice_id, x_human_approval,
                                   request.record, request.transcribe)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.get("/api/v1/calls")
async def list_calls(svc: StephanieAI = Depends(get_stephanie)):
    return {"calls": svc.twilio.list_calls(), "truth_label": svc.twilio.truth_label}


@app.get("/api/v1/calls/{session_id}")
async def get_call(session_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return svc.twilio._session(session_id).to_dict()
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.post("/api/v1/calls/{session_id}/end")
async def end_call(session_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return (await svc.twilio.end_call(session_id)).to_dict()
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.post("/api/v1/calls/{session_id}/recording/start")
async def start_recording(session_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return {"session_id": session_id, "recording": await svc.twilio.start_recording(session_id)}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.post("/api/v1/calls/{session_id}/recording/stop")
async def stop_recording(session_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return {"session_id": session_id, "recording_url": await svc.twilio.stop_recording(session_id)}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.get("/api/v1/calls/{session_id}/recording.wav")
async def get_recording(session_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        svc.twilio._session(session_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
    data = svc.twilio.recording_audio(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="no media captured for this session")
    return Response(content=data, media_type="audio/wav")


@app.post("/api/v1/calls/{session_id}/transcription/start")
async def start_transcription(session_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return {"session_id": session_id, "transcription": await svc.twilio.start_transcription(session_id)}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.post("/api/v1/calls/{session_id}/transcription")
async def add_transcript_line(session_id: str, request: TranscriptLineRequest, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return await svc.twilio.add_transcript_line(session_id, request.speaker, request.text, request.confidence)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.get("/api/v1/calls/{session_id}/transcription")
async def get_transcription(session_id: str, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return {"session_id": session_id, "transcript": await svc.twilio.get_transcription(session_id)}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.post("/api/v1/calls/{session_id}/forward")
async def forward_call(session_id: str, request: ForwardRequest, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return {"session_id": session_id, "forwarded": await svc.twilio.forward_call(session_id, request.target_number)}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.post("/api/v1/messages")
async def send_message(request: MessageRequest, svc: StephanieAI = Depends(get_stephanie),
                       x_human_approval: Optional[str] = Header(default=None)):
    try:
        return await svc.send_message(request.to_number, request.message, x_human_approval, request.append_disclaimer)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.get("/api/v1/messages")
async def list_messages(svc: StephanieAI = Depends(get_stephanie)):
    return {"messages": svc.twilio.message_log, "truth_label": svc.twilio.truth_label}


# ---------------------------------------------------------------------------
# Telephony — conferences, queues, IVR
# ---------------------------------------------------------------------------

@app.post("/api/v1/conferences")
async def create_conference(request: ConferenceRequest, svc: StephanieAI = Depends(get_stephanie)):
    return (await svc.twilio.create_conference(request.name, request.max_participants)).to_dict()


@app.get("/api/v1/conferences")
async def list_conferences(svc: StephanieAI = Depends(get_stephanie)):
    return {"conferences": [c.to_dict() for c in svc.twilio.conferences.values()]}


@app.post("/api/v1/conferences/{conference_id}/participants")
async def add_participant(conference_id: str, request: ParticipantRequest, svc: StephanieAI = Depends(get_stephanie)):
    try:
        return await svc.twilio.add_to_conference(conference_id, request.participant_number, request.session_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)


@app.post("/api/v1/conferences/{conference_id}/participants/{participant_id}/mute")
async def mute_participant(conference_id: str, participant_id: str, request: MuteRequest,
                           svc: StephanieAI = Depends(get_stephanie)):
    ok = await svc.twilio.mute_participant(conference_id, participant_id, request.muted)
    if not ok:
        raise HTTPException(status_code=404, detail="conference or participant not found")
    return {"participant_id": participant_id, "muted": request.muted}


@app.delete("/api/v1/conferences/{conference_id}/participants/{participant_id}")
async def remove_participant(conference_id: str, participant_id: str, svc: StephanieAI = Depends(get_stephanie)):
    if not await svc.twilio.remove_from_conference(conference_id, participant_id):
        raise HTTPException(status_code=404, detail="conference or participant not found")
    return {"removed": participant_id}


@app.post("/api/v1/queues")
async def create_queue(request: QueueRequest, svc: StephanieAI = Depends(get_stephanie)):
    return (await svc.twilio.create_queue(request.name, request.max_size)).to_dict()


@app.get("/api/v1/queues")
async def list_queues(svc: StephanieAI = Depends(get_stephanie)):
    return {"queues": [q.to_dict() for q in svc.twilio.queues.values()]}


@app.post("/api/v1/queues/{queue_id}/enqueue")
async def enqueue(queue_id: str, request: EnqueueRequest, svc: StephanieAI = Depends(get_stephanie)):
    try:
        position = await svc.twilio.add_to_queue(queue_id, request.session_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
    return {"queue_id": queue_id, "session_id": request.session_id, "position": position}


@app.get("/api/v1/queues/{queue_id}/position/{session_id}")
async def queue_position(queue_id: str, session_id: str, svc: StephanieAI = Depends(get_stephanie)):
    return {"queue_id": queue_id, "session_id": session_id,
            "position": await svc.twilio.get_queue_position(queue_id, session_id)}


@app.post("/api/v1/queues/{queue_id}/dequeue")
async def dequeue(queue_id: str, svc: StephanieAI = Depends(get_stephanie)):
    session = await svc.twilio.dequeue_next(queue_id)
    return {"queue_id": queue_id, "session": session.to_dict() if session else None}


@app.post("/api/v1/ivr")
async def create_ivr(request: IvrRequest, svc: StephanieAI = Depends(get_stephanie)):
    try:
        svc.screen_text(request.prompt)
        menu = await svc.twilio.create_ivr_menu(request.prompt, request.options, request.voice_id, request.fallback)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
    return menu.to_dict()


@app.get("/api/v1/ivr")
async def list_ivr(svc: StephanieAI = Depends(get_stephanie)):
    return {"menus": [m.to_dict() for m in svc.twilio.ivr_menus.values()]}


@app.post("/api/v1/ivr/{ivr_id}/input")
async def ivr_input(ivr_id: str, request: IvrInputRequest, svc: StephanieAI = Depends(get_stephanie)):
    try:
        destination = await svc.twilio.handle_ivr_input(ivr_id, request.digits, request.session_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc)
    return {"ivr_id": ivr_id, "digits": request.digits, "destination": destination}


@app.get("/api/v1/ivr/{ivr_id}/prompt.wav")
async def ivr_prompt_audio(ivr_id: str, svc: StephanieAI = Depends(get_stephanie)):
    """Synthesize the IVR prompt + options in Stephanie's voice."""
    menu = svc.twilio.ivr_menus.get(ivr_id)
    if not menu:
        raise HTTPException(status_code=404, detail="unknown IVR menu")
    spoken = menu.prompt + " " + " ".join(f"Press {d} for {label}." for d, label in menu.options.items())
    result = await svc.generate_voice(spoken, {"voice_id": menu.voice_id})
    return Response(content=ap.encode_wav(result["audio"], svc.config.SAMPLE_RATE), media_type="audio/wav")


# ---------------------------------------------------------------------------
# Twilio webhooks (TwiML) and served audio assets
# ---------------------------------------------------------------------------

@app.api_route("/webhooks/twilio/voice", methods=["GET", "POST"])
async def twilio_voice_webhook(request: Request, session_id: Optional[str] = Query(default=None),
                               svc: StephanieAI = Depends(get_stephanie)):
    form = await request.form() if request.method == "POST" else {}
    twiml = await svc.voice_webhook_twiml(session_id, form.get("From"), form.get("CallSid"))
    return Response(content=twiml, media_type="application/xml")


@app.post("/webhooks/twilio/ivr/{ivr_id}")
async def twilio_ivr_webhook(ivr_id: str, Digits: str = Form(default=""), session_id: Optional[str] = Query(default=None),
                             svc: StephanieAI = Depends(get_stephanie)):
    from services.twilio_features import TwiML

    try:
        destination = await svc.twilio.handle_ivr_input(ivr_id, Digits, session_id)
    except KeyError:
        return Response(content=TwiML.response(TwiML.say("Sorry, that menu is unavailable."), TwiML.hangup()),
                        media_type="application/xml")
    twiml = TwiML.response(TwiML.say(f"Connecting you to {destination}."))
    return Response(content=twiml, media_type="application/xml")


@app.post("/webhooks/twilio/sms")
async def twilio_sms_webhook(From: str = Form(...), Body: str = Form(default=""), svc: StephanieAI = Depends(get_stephanie)):
    from services.twilio_features import TwiML

    await svc.twilio.receive_sms(From, Body)
    reply = "Thanks for texting Noble Port. Stephanie has logged your message for human review."
    return Response(content=TwiML.response(f"<Message>{reply}</Message>"), media_type="application/xml")


@app.post("/webhooks/twilio/recording")
async def twilio_recording_webhook(session_id: Optional[str] = Query(default=None), RecordingUrl: str = Form(default=""),
                                   svc: StephanieAI = Depends(get_stephanie)):
    if session_id and session_id in svc.twilio.active_calls:
        session = svc.twilio.active_calls[session_id]
        session.recording_url = RecordingUrl or session.recording_url
        session.log("provider_recording", url=RecordingUrl)
    return Response(content="<Response/>", media_type="application/xml")


@app.get("/api/v1/audio/{asset_id}.wav")
async def audio_asset(asset_id: str, svc: StephanieAI = Depends(get_stephanie)):
    data = svc.twilio.audio_assets.get(asset_id)
    if not data:
        raise HTTPException(status_code=404, detail="unknown audio asset")
    return Response(content=data, media_type="audio/wav")


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------

@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    """Real-time voice streaming for the desktop client.

    Client → {"type": "generate", "text": ..., "voice_id": ..., "emotion": ..., "speed": ...}
    Server → {"type": "audio_chunk", "data": <base64 pcm_s16le>, "sample_rate": ..., "segment": ..., "final": ...}
             {"type": "done"} | {"type": "error", "message": ...}
    """
    await websocket.accept()
    svc: StephanieAI = websocket.app.state.stephanie
    try:
        while True:
            data = await websocket.receive_json()
            kind = data.get("type")
            if kind == "stop":
                break
            if kind != "generate":
                await websocket.send_json({"type": "error", "message": "unknown message type"})
                continue
            text = data.get("text", "")
            try:
                svc.screen_text(text)
                for chunk in svc.elevenlabs.stream_voice(
                    text, voice_id=data.get("voice_id", "stephanie_primary"), emotion=data.get("emotion", "neutral"),
                    speed=float(data.get("speed", 1.0)), chunk_samples=int(data.get("chunk_size", 4096)),
                ):
                    await websocket.send_json({
                        "type": "audio_chunk", "data": base64.b64encode(chunk["pcm"]).decode(),
                        "sample_rate": chunk["sample_rate"], "encoding": chunk["encoding"],
                        "segment": chunk["segment"], "segments_total": chunk["segments_total"],
                        "text": chunk["text"], "final": chunk["final"],
                    })
                await websocket.send_json({"type": "done", "truth_label": svc.voice_engine.synthesizer.truth_label})
            except Exception as exc:  # noqa: BLE001
                await websocket.send_json({"type": "error", "message": str(exc)})
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


@app.websocket("/ws/media-stream")
async def websocket_media_stream(websocket: WebSocket, session_id: Optional[str] = Query(default=None)):
    """Twilio Media Streams endpoint (bidirectional 8 kHz mu-law)."""
    await websocket.accept()
    svc: StephanieAI = websocket.app.state.stephanie
    stream_id = f"stream_{session_id}" if session_id else None
    if stream_id and stream_id not in svc.twilio.media_streams and session_id in svc.twilio.active_calls:
        await svc.twilio.create_media_stream(session_id)
    try:
        while True:
            message = await websocket.receive_text()
            reply = await svc.twilio.process_media_message(message, stream_id)
            if reply and reply.get("event") == "stopped":
                break
            if reply and reply.get("event") == "started":
                await websocket.send_json({"event": "stephanie", "status": "listening", "stream_id": reply["stream_id"]})
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check(svc: StephanieAI = Depends(get_stephanie)):
    return svc.status()


@app.get("/")
async def root():
    return {"service": "Stephanie.ai Voice", "docs": "/docs", "health": "/health"}


if __name__ == "__main__":
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=config.DEBUG)
