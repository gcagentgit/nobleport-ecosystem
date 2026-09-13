"""SMS and voice delivery, with ElevenLabs as the voice option.

Transports
* ``SimulatedTransport`` (default) records every SMS / call in memory and in
  the notifications table with ``truth_label: STAGED``. Nothing leaves the box.
* ``TwilioTransport`` uses Twilio's REST API when account SID, auth token and a
  sending number are configured. ``truth_label: LIVE``.

Governance: the engine will only ever text or call the single configured
owner number (``STEPH_EMAIL_OWNER_PHONE``). Any other destination is refused,
so a bug or a bad rule can never turn this into a broadcast tool.

Voice: when ``ELEVENLABS_API_KEY`` and a voice id are set, the brief script is
synthesised to MP3 and served at ``/audio/<file>`` for Twilio's ``<Play>``.
Without ElevenLabs a call falls back to Twilio's own ``<Say>`` voice and the
record says so. There is no silent substitution.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from xml.sax.saxutils import escape

import httpx

from .config import Settings
from .db import Database

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")


class NotifyError(RuntimeError):
    pass


class DestinationError(NotifyError):
    """Raised when something tries to notify a number other than the owner's."""


class VoiceError(NotifyError):
    pass


def normalize_phone(number: str) -> str:
    cleaned = re.sub(r"[\s\-().]", "", number or "")
    if cleaned and not cleaned.startswith("+") and len(cleaned) == 10:
        cleaned = "+1" + cleaned
    if not E164_RE.match(cleaned):
        raise ValueError(f"phone number must be E.164 (e.g. +19785551234), got {number!r}")
    return cleaned


# ---------------------------------------------------------------- transports
class Transport(Protocol):
    name: str
    truth_label: str
    def send_sms(self, to: str, from_: str, body: str) -> dict: ...
    def create_call(self, to: str, from_: str, twiml_url: str) -> dict: ...


class SimulatedTransport:
    name = "simulated"
    truth_label = "STAGED"

    def __init__(self) -> None:
        self.outbox: list[dict] = []

    def send_sms(self, to: str, from_: str, body: str) -> dict:
        sid = f"SM{uuid.uuid4().hex}"
        self.outbox.append({"kind": "sms", "sid": sid, "to": to, "from": from_, "body": body})
        return {"sid": sid, "status": "simulated"}

    def create_call(self, to: str, from_: str, twiml_url: str) -> dict:
        sid = f"CA{uuid.uuid4().hex}"
        self.outbox.append({"kind": "call", "sid": sid, "to": to, "from": from_, "url": twiml_url})
        return {"sid": sid, "status": "simulated"}


class TwilioTransport:
    name = "twilio"
    truth_label = "LIVE"

    def __init__(self, account_sid: str, auth_token: str, base_url: str = "https://api.twilio.com/2010-04-01",
                 timeout: float = 15.0, transport: httpx.BaseTransport | None = None):
        self.account_sid = account_sid
        self._client = httpx.Client(auth=(account_sid, auth_token), timeout=timeout, transport=transport)
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, data: dict) -> dict:
        try:
            resp = self._client.post(f"{self.base_url}/Accounts/{self.account_sid}{path}", data=data)
        except httpx.HTTPError as exc:
            raise NotifyError(f"twilio network error: {exc}") from exc
        if resp.status_code >= 400:
            raise NotifyError(f"twilio {path} failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def send_sms(self, to: str, from_: str, body: str) -> dict:
        return self._post("/Messages.json", {"To": to, "From": from_, "Body": body})

    def create_call(self, to: str, from_: str, twiml_url: str) -> dict:
        return self._post("/Calls.json", {"To": to, "From": from_, "Url": twiml_url})


# ---------------------------------------------------------------- ElevenLabs
class ElevenLabsVoice:
    source = "elevenlabs"

    def __init__(self, api_key: str, voice_id: str, model_id: str = "eleven_multilingual_v2",
                 base_url: str = "https://api.elevenlabs.io/v1", timeout: float = 90.0,
                 transport: httpx.BaseTransport | None = None):
        if not api_key:
            raise VoiceError("ELEVENLABS_API_KEY is not set")
        if not voice_id:
            raise VoiceError("STEPH_EMAIL_ELEVENLABS_VOICE_ID is not set — pick Stephanie's voice id first")
        self.voice_id = voice_id
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport, headers={"xi-api-key": api_key})

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = self._client.request(method, f"{self.base_url}{path}", **kw)
        except httpx.HTTPError as exc:
            raise VoiceError(f"network error calling ElevenLabs {path}: {exc}") from exc
        if resp.status_code == 401:
            raise VoiceError("ElevenLabs rejected the API key (401)")
        if resp.status_code == 404:
            raise VoiceError(f"voice {self.voice_id!r} not found in this ElevenLabs account (404)")
        if resp.status_code >= 400:
            raise VoiceError(f"ElevenLabs {method} {path} failed with HTTP {resp.status_code}: {resp.text[:200]}")
        return resp

    def check_auth(self) -> dict:
        data = self._request("GET", "/user").json()
        sub = data.get("subscription") or {}
        return {"ok": True, "tier": sub.get("tier"), "character_count": sub.get("character_count"),
                "character_limit": sub.get("character_limit")}

    def get_voice(self) -> dict:
        v = self._request("GET", f"/voices/{self.voice_id}").json()
        return {"voice_id": v.get("voice_id"), "name": v.get("name"), "category": v.get("category")}

    def synthesize(self, text: str, speed: float | None = None) -> bytes:
        if not text.strip():
            raise ValueError("text is empty")
        settings: dict[str, Any] = {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0, "use_speaker_boost": True}
        if speed is not None:
            if not 0.7 <= speed <= 1.2:
                raise ValueError("ElevenLabs speed must be between 0.7 and 1.2")
            settings["speed"] = speed
        resp = self._request("POST", f"/text-to-speech/{self.voice_id}", params={"output_format": "mp3_44100_128"},
                             json={"text": text, "model_id": self.model_id, "voice_settings": settings},
                             headers={"accept": "audio/mpeg"})
        if not resp.content:
            raise VoiceError("ElevenLabs returned an empty audio body")
        return resp.content


# ---------------------------------------------------------------- TwiML
def twiml_say(text: str, voice: str = "Polly.Joanna") -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say voice="{voice}">{escape(text)}</Say></Response>'


def twiml_play(url: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Play>{escape(url)}</Play></Response>'


# ---------------------------------------------------------------- notifier
@dataclass
class VoiceStatus:
    configured: bool
    verified: bool
    source: str
    detail: str


class Notifier:
    def __init__(self, db: Database, settings: Settings, transport: Transport | None = None,
                 voice: ElevenLabsVoice | None = None):
        self.db = db
        self.settings = settings
        self.transport = transport or self._default_transport()
        self.voice = voice if voice is not None else self._default_voice()

    def _default_transport(self) -> Transport:
        s = self.settings
        if s.twilio_configured:
            return TwilioTransport(s.twilio_account_sid, s.twilio_auth_token, s.twilio_base_url)
        return SimulatedTransport()

    def _default_voice(self) -> ElevenLabsVoice | None:
        s = self.settings
        if s.elevenlabs_configured:
            return ElevenLabsVoice(s.elevenlabs_api_key, s.elevenlabs_voice_id, s.elevenlabs_model_id, s.elevenlabs_base_url)
        return None

    # ---- state
    @property
    def is_live(self) -> bool:
        return getattr(self.transport, "truth_label", "STAGED") == "LIVE"

    @property
    def truth_label(self) -> str:
        return "LIVE" if self.is_live else "STAGED"

    @property
    def owner_phone(self) -> str:
        return normalize_phone(self.settings.owner_phone) if self.settings.owner_phone else ""

    @property
    def from_number(self) -> str:
        return self.settings.twilio_from_number or "+15555550100"

    def guard_destination(self, to: str) -> str:
        if not self.owner_phone:
            raise DestinationError("STEPH_EMAIL_OWNER_PHONE is not set; the engine only notifies the owner's number")
        to_n = normalize_phone(to)
        if to_n != self.owner_phone:
            raise DestinationError(f"refusing to notify {to_n}: the engine only contacts the configured owner phone")
        return to_n

    # ---- SMS
    def send_sms(self, body: str, *, purpose: str = "alert", related_message_id: int | None = None,
                 to: str | None = None) -> dict:
        if not body.strip():
            raise ValueError("SMS body is empty")
        body = body[:1600]
        to_n = self.guard_destination(to or self.owner_phone)
        result = self.transport.send_sms(to_n, self.from_number, body)
        record = {"kind": "sms", "purpose": purpose, "to_number": to_n, "body": body,
                  "status": result.get("status", "queued"), "provider_sid": result.get("sid", ""),
                  "transport": self.transport.name, "truth_label": self.truth_label, "related_message_id": related_message_id}
        record["id"] = self.db.log_notification(**record)
        return record

    # ---- voice call
    def place_call(self, twiml_url: str, *, purpose: str = "brief", body: str = "", to: str | None = None) -> dict:
        to_n = self.guard_destination(to or self.owner_phone)
        result = self.transport.create_call(to_n, self.from_number, twiml_url)
        record = {"kind": "call", "purpose": purpose, "to_number": to_n, "body": body or twiml_url,
                  "status": result.get("status", "queued"), "provider_sid": result.get("sid", ""),
                  "transport": self.transport.name, "truth_label": self.truth_label, "related_message_id": None}
        record["id"] = self.db.log_notification(**record)
        return record

    # ---- voice synthesis
    def synthesize(self, script: str, out_path: Path) -> dict:
        """Render ``script`` with ElevenLabs. Raises VoiceError when the option is not configured."""
        if self.voice is None:
            raise VoiceError("ElevenLabs is not configured (set STEPH_EMAIL_ELEVENLABS_API_KEY and _VOICE_ID)")
        started = time.perf_counter()
        audio = self.voice.synthesize(script)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        return {"path": str(out_path), "bytes": len(audio), "source": self.voice.source, "voice_id": self.voice.voice_id,
                "latency_ms": int((time.perf_counter() - started) * 1000)}

    def twiml_for_brief(self, script: str, audio_url: str | None) -> str:
        return twiml_play(audio_url) if audio_url else twiml_say(script)

    # ---- verification evidence (mirrors the briefing module's 5-step check)
    def evidence_path(self) -> Path:
        return self.settings.evidence_dir / "voice-verification.json"

    def load_evidence(self) -> dict:
        p = self.evidence_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def voice_status(self) -> VoiceStatus:
        ev = self.load_evidence()
        if self.voice is None:
            return VoiceStatus(False, False, "twilio-say" if self.is_live else "none",
                               "ElevenLabs not configured; calls would use Twilio's built-in voice")
        verified = bool(ev.get("verified"))
        return VoiceStatus(True, verified, "elevenlabs",
                           "verified on the phone" if verified else "synthesis untested or playback not confirmed")

    def verify_voice(self, *, sample_text: str, confirm_playback: bool = False, device: str = "", note: str = "") -> dict:
        """Run the checks that can run here; record the human playback result when given.

        Steps: key present → auth ok → voice resolves → synthesis produces audio →
        playback confirmed by a human. ``verified`` is true only when all five pass.
        """
        ev = self.load_evidence()
        steps = ev.get("steps", {})
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if self.voice is None:
            steps.update({"key_present": False, "auth_ok": False, "voice_resolved": False, "synthesis_ok": False})
            ev.update({"steps": steps, "verified": False, "checked_at": now,
                       "error": "ElevenLabs not configured (STEPH_EMAIL_ELEVENLABS_API_KEY / _VOICE_ID)"})
        else:
            steps["key_present"] = True
            try:
                auth = self.voice.check_auth()
                steps["auth_ok"] = True
                ev["account"] = auth
                voice = self.voice.get_voice()
                steps["voice_resolved"] = True
                ev["voice"] = voice
                sample = self.settings.evidence_dir / "voice-sample.mp3"
                result = self.synthesize(sample_text, sample)
                steps["synthesis_ok"] = result["bytes"] > 0
                ev["sample"] = result
                ev.pop("error", None)
            except (VoiceError, ValueError) as exc:
                ev["error"] = str(exc)
                for k in ("auth_ok", "voice_resolved", "synthesis_ok"):
                    steps.setdefault(k, False)
            ev["checked_at"] = now
        if confirm_playback:
            steps["playback_confirmed"] = True
            ev["playback"] = {"device": device, "note": note, "at": now}
        else:
            steps.setdefault("playback_confirmed", False)
        ev["steps"] = steps
        ev["verified"] = all(steps.get(k) for k in ("key_present", "auth_ok", "voice_resolved", "synthesis_ok", "playback_confirmed"))
        self.settings.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_path().write_text(json.dumps(ev, indent=2))
        return ev
