"""Twilio-inspired telephony features for Stephanie.ai.

Two transports back the same feature set:

* ``SimulatedTransport`` — default. Records every call/SMS in memory so the
  UI, tests and demos work with no carrier account. Responses carry
  ``truth_label: STAGED``.
* ``TwilioRestTransport`` — selected when account SID, auth token and a
  sending number are configured. Uses the public Twilio REST API over httpx.

Outbound traffic to a real phone number is a public broadcast under the
NoblePort governance model, so ``TwilioFeatures`` refuses to dispatch through
the REST transport unless the caller has passed the human-approval gate.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional
from xml.sax.saxutils import escape

import numpy as np

from models import audio_processor as ap

log = logging.getLogger("stephanie.twilio")

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_e164(number: str) -> str:
    number = number.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not E164_RE.match(number):
        raise ValueError(f"phone number must be E.164 (e.g. +16175551234), got '{number}'")
    return number


class HumanGateError(PermissionError):
    """Raised when a real outbound action lacks human approval."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CallSession:
    session_id: str
    to_number: str
    from_number: str
    start_time: datetime
    status: str
    direction: str = "outbound"
    recording_enabled: bool = False
    recording_url: Optional[str] = None
    transcription_enabled: bool = False
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    conference_id: Optional[str] = None
    queue_id: Optional[str] = None
    forwarded_to: Optional[str] = None
    provider_sid: Optional[str] = None
    truth_label: str = "STAGED"
    events: List[Dict[str, Any]] = field(default_factory=list)

    def log(self, event: str, **data: Any) -> None:
        self.events.append({"event": event, "at": _now().isoformat(), **data})

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["start_time"] = self.start_time.isoformat()
        return d


@dataclass
class Conference:
    conference_id: str
    name: str
    max_participants: int
    participants: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conference_id": self.conference_id, "name": self.name,
            "max_participants": self.max_participants, "participants": self.participants,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class CallQueue:
    queue_id: str
    name: str
    max_size: int
    waiting: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"queue_id": self.queue_id, "name": self.name, "max_size": self.max_size, "waiting": list(self.waiting)}


@dataclass
class IvrMenu:
    ivr_id: str
    prompt: str
    options: Dict[str, str]          # digit -> destination label
    voice_id: str = "stephanie_dispatch"
    fallback: str = "operator"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class SimulatedTransport:
    name = "simulated"
    truth_label = "STAGED"

    def __init__(self) -> None:
        self.outbox: List[Dict[str, Any]] = []

    async def create_call(self, to: str, from_: str, twiml_url: str) -> Dict[str, Any]:
        sid = f"CA{uuid.uuid4().hex}"
        self.outbox.append({"kind": "call", "sid": sid, "to": to, "from": from_, "url": twiml_url})
        return {"sid": sid, "status": "queued"}

    async def send_sms(self, to: str, from_: str, body: str) -> Dict[str, Any]:
        sid = f"SM{uuid.uuid4().hex}"
        self.outbox.append({"kind": "sms", "sid": sid, "to": to, "from": from_, "body": body})
        return {"sid": sid, "status": "queued"}

    async def update_call(self, sid: str, **params: Any) -> Dict[str, Any]:
        self.outbox.append({"kind": "call_update", "sid": sid, **params})
        return {"sid": sid, "status": "in-progress", **params}


class TwilioRestTransport:
    name = "twilio_rest"
    truth_label = "LIVE"

    def __init__(self, account_sid: str, auth_token: str, base_url: str, timeout: float = 15.0):
        self.account_sid = account_sid
        self.auth = (account_sid, auth_token)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def _post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/Accounts/{self.account_sid}{path}", data=data, auth=self.auth)
            resp.raise_for_status()
            return resp.json()

    async def create_call(self, to: str, from_: str, twiml_url: str) -> Dict[str, Any]:
        return await self._post("/Calls.json", {"To": to, "From": from_, "Url": twiml_url})

    async def send_sms(self, to: str, from_: str, body: str) -> Dict[str, Any]:
        return await self._post("/Messages.json", {"To": to, "From": from_, "Body": body})

    async def update_call(self, sid: str, **params: Any) -> Dict[str, Any]:
        return await self._post(f"/Calls/{sid}.json", params)


# ---------------------------------------------------------------------------
# TwiML helpers (what Twilio fetches from our webhooks)
# ---------------------------------------------------------------------------

class TwiML:
    @staticmethod
    def say(text: str, voice: str = "Polly.Joanna") -> str:
        return f'<Say voice="{voice}">{escape(text)}</Say>'

    @staticmethod
    def play(url: str) -> str:
        return f"<Play>{escape(url)}</Play>"

    @staticmethod
    def gather(inner: str, action: str, num_digits: int = 1, timeout: int = 5) -> str:
        return f'<Gather input="dtmf" numDigits="{num_digits}" timeout="{timeout}" action="{escape(action)}">{inner}</Gather>'

    @staticmethod
    def dial(number: str) -> str:
        return f"<Dial>{escape(number)}</Dial>"

    @staticmethod
    def conference(name: str) -> str:
        return f"<Dial><Conference>{escape(name)}</Conference></Dial>"

    @staticmethod
    def enqueue(name: str) -> str:
        return f"<Enqueue>{escape(name)}</Enqueue>"

    @staticmethod
    def record(action: str, max_length: int = 600) -> str:
        return f'<Record action="{escape(action)}" maxLength="{max_length}" transcribe="true"/>'

    @staticmethod
    def stream(url: str) -> str:
        return f'<Start><Stream url="{escape(url)}"/></Start>'

    @staticmethod
    def hangup() -> str:
        return "<Hangup/>"

    @staticmethod
    def response(*verbs: str) -> str:
        return '<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(verbs) + "</Response>"


# ---------------------------------------------------------------------------
# Feature set
# ---------------------------------------------------------------------------

WebhookHandler = Callable[[Dict[str, Any]], Awaitable[Any]]


class TwilioFeatures:
    """Top-ten Twilio capabilities, Stephanie edition."""

    def __init__(self, from_number: Optional[str] = None, transport: Optional[Any] = None,
                 public_base_url: str = "http://localhost:8000", human_approval_token: Optional[str] = None):
        self.from_number = from_number or "+15555550100"
        self.transport = transport or SimulatedTransport()
        self.public_base_url = public_base_url.rstrip("/")
        self.human_approval_token = human_approval_token
        self.active_calls: Dict[str, CallSession] = {}
        self.conferences: Dict[str, Conference] = {}
        self.queues: Dict[str, CallQueue] = {}
        self.ivr_menus: Dict[str, IvrMenu] = {}
        self.webhook_handlers: Dict[str, List[WebhookHandler]] = {}
        self.message_log: List[Dict[str, Any]] = []
        self.media_streams: Dict[str, Dict[str, Any]] = {}
        self.audio_assets: Dict[str, bytes] = {}   # asset_id -> WAV bytes served to Twilio <Play>

    # -- governance ------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        return getattr(self.transport, "truth_label", "STAGED") == "LIVE"

    @property
    def truth_label(self) -> str:
        return "LIVE" if self.is_live else "STAGED"

    def check_human_gate(self, approval: Optional[str]) -> None:
        """Real outbound traffic needs a matching X-Human-Approval token."""
        if not self.is_live:
            return
        if not self.human_approval_token:
            raise HumanGateError("outbound telephony is LIVE but no STEPHANIE_HUMAN_APPROVAL_TOKEN is configured; refusing to dial")
        if approval != self.human_approval_token:
            raise HumanGateError("human approval required: supply X-Human-Approval header")

    # 1. Voice calls --------------------------------------------------------------
    async def make_voice_call(self, to_number: str, from_number: Optional[str] = None,
                              url: Optional[str] = None, human_approval: Optional[str] = None) -> CallSession:
        to_number = validate_e164(to_number)
        self.check_human_gate(human_approval)
        session_id = f"call_{uuid.uuid4().hex[:12]}"
        session = CallSession(session_id=session_id, to_number=to_number,
                              from_number=from_number or self.from_number, start_time=_now(),
                              status="initiating", truth_label=self.truth_label)
        self.active_calls[session_id] = session
        twiml_url = url or f"{self.public_base_url}/webhooks/twilio/voice?session_id={session_id}"
        result = await self.transport.create_call(to_number, session.from_number, twiml_url)
        session.provider_sid = result.get("sid")
        session.status = "connected" if not self.is_live else result.get("status", "queued")
        session.log("call_created", provider_sid=session.provider_sid, transport=self.transport.name)
        await self.trigger_webhook("call.initiated", session.to_dict())
        return session

    async def end_call(self, session_id: str) -> CallSession:
        session = self._session(session_id)
        if session.recording_enabled:
            await self.stop_recording(session_id)
        if session.queue_id:
            self.queues[session.queue_id].waiting = [s for s in self.queues[session.queue_id].waiting if s != session_id]
        session.status = "completed"
        session.log("call_completed")
        await self.trigger_webhook("call.completed", session.to_dict())
        return session

    def list_calls(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.active_calls.values()]

    # 2. SMS -------------------------------------------------------------------------
    async def send_sms(self, to_number: str, message: str, human_approval: Optional[str] = None) -> Dict[str, Any]:
        to_number = validate_e164(to_number)
        if not message.strip():
            raise ValueError("message body is empty")
        if len(message) > 1600:
            raise ValueError("message exceeds 1600 characters")
        self.check_human_gate(human_approval)
        result = await self.transport.send_sms(to_number, self.from_number, message)
        record = {
            "message_id": f"msg_{uuid.uuid4().hex[:12]}", "to": to_number, "from": self.from_number,
            "body": message, "status": result.get("status", "queued"), "provider_sid": result.get("sid"),
            "timestamp": _now().isoformat(), "direction": "outbound", "truth_label": self.truth_label,
            "segments": max(1, (len(message) + 159) // 160),
        }
        self.message_log.append(record)
        await self.trigger_webhook("sms.sent", record)
        return record

    async def receive_sms(self, from_number: str, body: str) -> Dict[str, Any]:
        record = {"message_id": f"msg_{uuid.uuid4().hex[:12]}", "to": self.from_number, "from": from_number,
                  "body": body, "status": "received", "timestamp": _now().isoformat(), "direction": "inbound",
                  "truth_label": self.truth_label}
        self.message_log.append(record)
        await self.trigger_webhook("sms.received", record)
        return record

    # 3. Call recording ----------------------------------------------------------------
    async def start_recording(self, session_id: str, fmt: str = "wav") -> bool:
        session = self._session(session_id)
        if session.status not in {"connected", "in-progress", "queued", "initiating"}:
            return False
        session.recording_enabled = True
        session.recording_url = None
        session.log("recording_started", format=fmt)
        if self.is_live and session.provider_sid:
            await self.transport.update_call(session.provider_sid, Twiml=TwiML.response(TwiML.record(
                f"{self.public_base_url}/webhooks/twilio/recording?session_id={session_id}")))
        return True

    async def stop_recording(self, session_id: str) -> Optional[str]:
        session = self._session(session_id)
        if not session.recording_enabled:
            return None
        session.recording_enabled = False
        session.recording_url = f"{self.public_base_url}/api/v1/calls/{session_id}/recording.wav"
        session.log("recording_stopped", url=session.recording_url)
        return session.recording_url

    def recording_audio(self, session_id: str) -> Optional[bytes]:
        """Assembled WAV of the media captured for the session (if any)."""
        stream = self.media_streams.get(f"stream_{session_id}")
        if not stream or not stream["frames"]:
            return None
        audio = np.concatenate(stream["frames"])
        return ap.encode_wav(audio, stream["sample_rate"])

    # 4. Real-time transcription ---------------------------------------------------------
    async def start_transcription(self, session_id: str) -> bool:
        session = self._session(session_id)
        session.transcription_enabled = True
        session.log("transcription_started")
        return True

    async def add_transcript_line(self, session_id: str, speaker: str, text: str, confidence: float = 1.0) -> Dict[str, Any]:
        session = self._session(session_id)
        line = {"at": _now().isoformat(), "speaker": speaker, "text": text, "confidence": confidence}
        session.transcript.append(line)
        await self.trigger_webhook("transcript.line", {"session_id": session_id, **line})
        return line

    async def get_transcription(self, session_id: str) -> List[Dict[str, Any]]:
        return list(self._session(session_id).transcript)

    # 5. Conference calls ------------------------------------------------------------------
    async def create_conference(self, name: str, max_participants: int = 10) -> Conference:
        conf = Conference(conference_id=f"conf_{uuid.uuid4().hex[:10]}", name=name, max_participants=max_participants)
        self.conferences[conf.conference_id] = conf
        return conf

    async def add_to_conference(self, conference_id: str, participant_number: str,
                                session_id: Optional[str] = None) -> Dict[str, Any]:
        conf = self.conferences.get(conference_id)
        if not conf:
            raise KeyError(f"unknown conference '{conference_id}'")
        if len(conf.participants) >= conf.max_participants:
            raise ValueError("conference is full")
        participant_number = validate_e164(participant_number)
        participant_id = f"p_{uuid.uuid4().hex[:8]}"
        conf.participants[participant_id] = {"number": participant_number, "muted": False, "session_id": session_id,
                                             "joined_at": _now().isoformat()}
        if session_id and session_id in self.active_calls:
            self.active_calls[session_id].conference_id = conference_id
            self.active_calls[session_id].log("joined_conference", conference_id=conference_id)
        return {"participant_id": participant_id, **conf.participants[participant_id]}

    async def mute_participant(self, conference_id: str, participant_id: str, muted: bool = True) -> bool:
        conf = self.conferences.get(conference_id)
        if not conf or participant_id not in conf.participants:
            return False
        conf.participants[participant_id]["muted"] = muted
        return True

    async def remove_from_conference(self, conference_id: str, participant_id: str) -> bool:
        conf = self.conferences.get(conference_id)
        if not conf:
            return False
        return conf.participants.pop(participant_id, None) is not None

    # 6. Call queuing ----------------------------------------------------------------------
    async def create_queue(self, queue_name: str, max_size: int = 50) -> CallQueue:
        slug = re.sub(r"[^a-z0-9]+", "_", queue_name.lower()).strip("_")
        queue = CallQueue(queue_id=f"queue_{slug}", name=queue_name, max_size=max_size)
        self.queues[queue.queue_id] = queue
        return queue

    async def add_to_queue(self, queue_id: str, session_id: str) -> int:
        queue = self.queues.get(queue_id)
        if not queue:
            raise KeyError(f"unknown queue '{queue_id}'")
        session = self._session(session_id)
        if len(queue.waiting) >= queue.max_size:
            raise ValueError("queue is full")
        if session_id not in queue.waiting:
            queue.waiting.append(session_id)
        session.queue_id = queue_id
        session.status = "queued"
        session.log("enqueued", queue_id=queue_id)
        return queue.waiting.index(session_id) + 1

    async def get_queue_position(self, queue_id: str, session_id: str) -> int:
        queue = self.queues.get(queue_id)
        if not queue or session_id not in queue.waiting:
            return 0
        return queue.waiting.index(session_id) + 1

    async def dequeue_next(self, queue_id: str) -> Optional[CallSession]:
        queue = self.queues.get(queue_id)
        if not queue or not queue.waiting:
            return None
        session_id = queue.waiting.pop(0)
        session = self._session(session_id)
        session.queue_id = None
        session.status = "connected"
        session.log("dequeued", queue_id=queue_id)
        return session

    # 7. IVR ----------------------------------------------------------------------------------
    async def create_ivr_menu(self, prompt: str, menu_options: Dict[str, str], voice_id: str = "stephanie_dispatch",
                              fallback: str = "operator") -> IvrMenu:
        for digit in menu_options:
            if digit not in "0123456789*#" or len(digit) != 1:
                raise ValueError(f"invalid IVR key '{digit}'")
        menu = IvrMenu(ivr_id=f"ivr_{uuid.uuid4().hex[:8]}", prompt=prompt, options=dict(menu_options),
                       voice_id=voice_id, fallback=fallback)
        self.ivr_menus[menu.ivr_id] = menu
        return menu

    async def handle_ivr_input(self, ivr_id: str, digits: str, session_id: Optional[str] = None) -> str:
        menu = self.ivr_menus.get(ivr_id)
        if not menu:
            raise KeyError(f"unknown IVR menu '{ivr_id}'")
        destination = menu.options.get(digits.strip()[:1], menu.fallback)
        if session_id and session_id in self.active_calls:
            self.active_calls[session_id].log("ivr_selection", ivr_id=ivr_id, digits=digits, destination=destination)
        await self.trigger_webhook("ivr.selection", {"ivr_id": ivr_id, "digits": digits, "destination": destination,
                                                     "session_id": session_id})
        return destination

    def ivr_twiml(self, ivr_id: str, session_id: Optional[str] = None, audio_url: Optional[str] = None) -> str:
        menu = self.ivr_menus[ivr_id]
        action = f"{self.public_base_url}/webhooks/twilio/ivr/{ivr_id}"
        if session_id:
            action += f"?session_id={session_id}"
        spoken = TwiML.play(audio_url) if audio_url else TwiML.say(menu.prompt)
        options = " ".join(f"Press {digit} for {label}." for digit, label in menu.options.items())
        return TwiML.response(TwiML.gather(spoken + TwiML.say(options), action, num_digits=1))

    # 8. Call forwarding ----------------------------------------------------------------------
    async def forward_call(self, session_id: str, target_number: str) -> bool:
        session = self._session(session_id)
        target_number = validate_e164(target_number)
        if session.status in {"completed", "failed"}:
            return False
        session.forwarded_to = target_number
        session.status = "forwarded"
        session.log("forwarded", to=target_number)
        if self.is_live and session.provider_sid:
            await self.transport.update_call(session.provider_sid, Twiml=TwiML.response(TwiML.dial(target_number)))
        await self.trigger_webhook("call.forwarded", session.to_dict())
        return True

    # 9. Webhooks ---------------------------------------------------------------------------------
    def register_webhook(self, event: str, handler: WebhookHandler) -> None:
        self.webhook_handlers.setdefault(event, []).append(handler)

    def unregister_webhook(self, event: str, handler: WebhookHandler) -> None:
        handlers = self.webhook_handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    async def trigger_webhook(self, event: str, data: Dict[str, Any]) -> int:
        handlers = list(self.webhook_handlers.get(event, [])) + list(self.webhook_handlers.get("*", []))
        results = await asyncio.gather(*(h({"event": event, "data": data}) for h in handlers), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.warning("webhook handler for %s failed: %s", event, r)
        return len(handlers)

    async def handle_inbound_call(self, from_number: str, call_sid: Optional[str] = None) -> CallSession:
        """Called by the /webhooks/twilio/voice endpoint for a Twilio-originated call."""
        session_id = f"call_{uuid.uuid4().hex[:12]}"
        session = CallSession(session_id=session_id, to_number=self.from_number, from_number=from_number,
                              start_time=_now(), status="connected", direction="inbound",
                              provider_sid=call_sid, truth_label=self.truth_label)
        self.active_calls[session_id] = session
        session.log("inbound_call")
        await self.trigger_webhook("call.inbound", session.to_dict())
        return session

    # 10. Media streams -----------------------------------------------------------------------------
    async def create_media_stream(self, session_id: str, sample_rate: int = 8000) -> str:
        """Register a media stream for a session; frames arrive via ``process_media_message``.

        The WebSocket itself is served by the FastAPI app at ``/ws/media-stream``
        (Twilio ``<Stream>`` connects there). No extra server is started.
        """
        self._session(session_id)
        stream_id = f"stream_{session_id}"
        self.media_streams[stream_id] = {"session_id": session_id, "sample_rate": sample_rate, "frames": [],
                                         "frames_received": 0, "started_at": _now().isoformat(), "stream_sid": None}
        self.active_calls[session_id].log("media_stream_created", stream_id=stream_id)
        return stream_id

    def media_stream_url(self, session_id: str) -> str:
        ws_base = self.public_base_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/ws/media-stream?session_id={session_id}"

    async def process_media_message(self, message: str, stream_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Handle one Twilio Media Streams JSON message. Returns a reply to send (if any)."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return None
        event = data.get("event") or data.get("type")
        if event == "start":
            stream_sid = data.get("streamSid")
            params = data.get("start", {}).get("customParameters", {})
            sid = stream_id or f"stream_{params.get('session_id', '')}"
            if sid in self.media_streams:
                self.media_streams[sid]["stream_sid"] = stream_sid
            return {"event": "started", "stream_id": sid}
        if event in {"media", "audio"}:
            payload = data.get("media", {}).get("payload") or data.get("data", "")
            if not payload or not stream_id or stream_id not in self.media_streams:
                return None
            raw = base64.b64decode(payload)
            encoding = data.get("encoding", "mulaw")
            frame = ap.mulaw_decode(raw) if encoding == "mulaw" else ap.from_int16(np.frombuffer(raw, dtype=np.int16))
            stream = self.media_streams[stream_id]
            stream["frames"].append(frame)
            stream["frames_received"] += 1
            return {"event": "ack", "frames_received": stream["frames_received"]}
        if event == "stop":
            return {"event": "stopped", "stream_id": stream_id}
        return None

    def outbound_media_messages(self, audio: np.ndarray, sample_rate: int, stream_sid: str,
                                frame_ms: int = 20) -> List[str]:
        """Encode synthesized audio as Twilio outbound media messages (8 kHz mu-law)."""
        pcm8k = ap.resample(audio, sample_rate, 8000)
        frame = int(8000 * frame_ms / 1000)
        messages = []
        for start in range(0, pcm8k.size, frame):
            chunk = pcm8k[start : start + frame]
            messages.append(json.dumps({"event": "media", "streamSid": stream_sid,
                                        "media": {"payload": base64.b64encode(ap.mulaw_encode(chunk)).decode()}}))
        messages.append(json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": "stephanie_done"}}))
        return messages

    # -- assets served to Twilio <Play> ----------------------------------------------------------
    def register_audio_asset(self, wav_bytes: bytes) -> str:
        asset_id = uuid.uuid4().hex[:12]
        self.audio_assets[asset_id] = wav_bytes
        return asset_id

    def asset_url(self, asset_id: str) -> str:
        return f"{self.public_base_url}/api/v1/audio/{asset_id}.wav"

    # -- internals ---------------------------------------------------------------------------------
    def _session(self, session_id: str) -> CallSession:
        try:
            return self.active_calls[session_id]
        except KeyError:
            raise KeyError(f"unknown call session '{session_id}'") from None
