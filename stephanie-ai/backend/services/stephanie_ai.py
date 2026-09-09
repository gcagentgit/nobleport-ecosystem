"""Stephanie.ai — the orchestration service that ties voice and telephony together."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from config import StephanieConfig
from models import audio_processor as ap
from models.text_processor import TextProcessor
from models.voice_engine import ElevenLabsSynthesizer, LocalFormantSynthesizer, StephanieVoiceEngine
from services.elevenlabs_features import ElevenLabsFeatures
from services.twilio_features import SimulatedTransport, TwilioFeatures, TwilioRestTransport

log = logging.getLogger("stephanie.ai")

# Approved public description of Stephanie.ai (core/config/launch-gates.json →
# danger_words.approved_alternatives.stephanie_ai). Used as the default
# disclaimer that satisfies the ``proper_disclaimers`` launch requirement.
DEFAULT_DISCLAIMER = (
    "Stephanie.ai is NoblePort's executive orchestration assistant for intake, routing, "
    "document preparation, workflow tracking, and human-gated decision support. "
    "This is an automated message; a licensed person reviews every decision."
)


class ComplianceError(ValueError):
    def __init__(self, flagged: List[str], alternatives: Dict[str, str]):
        super().__init__(f"text contains prohibited public-material terms: {', '.join(flagged)}")
        self.flagged = flagged
        self.alternatives = alternatives


class StephanieAI:
    """Main Stephanie AI service combining all features."""

    def __init__(self, config: Optional[StephanieConfig] = None):
        self.config = config or StephanieConfig()
        self.text_processor = TextProcessor(self.config.LAUNCH_GATES_PATH, self.config.MAX_TEXT_LENGTH)
        self.disclaimer = self.text_processor.approved_alternatives.get("stephanie_ai", DEFAULT_DISCLAIMER)

        if self.config.elevenlabs_enabled:
            synthesizer = ElevenLabsSynthesizer(self.config.ELEVENLABS_API_KEY, self.config.ELEVENLABS_BASE_URL,
                                                self.config.ELEVENLABS_MODEL_ID)
        else:
            synthesizer = LocalFormantSynthesizer(self.text_processor)

        library_path = str(Path(self.config.DATA_DIR) / "voice_library.json")
        self.voice_engine = StephanieVoiceEngine(sample_rate=self.config.SAMPLE_RATE, synthesizer=synthesizer,
                                                 text_processor=self.text_processor, library_path=library_path)
        self.elevenlabs = ElevenLabsFeatures(self.voice_engine)

        if self.config.twilio_enabled:
            transport = TwilioRestTransport(self.config.TWILIO_ACCOUNT_SID, self.config.TWILIO_AUTH_TOKEN,
                                            self.config.TWILIO_BASE_URL)
        else:
            transport = SimulatedTransport()
        self.twilio = TwilioFeatures(from_number=self.config.TWILIO_PHONE_NUMBER, transport=transport,
                                     public_base_url=self.config.PUBLIC_BASE_URL,
                                     human_approval_token=self.config.HUMAN_APPROVAL_TOKEN)
        self.conversation_history: List[Dict[str, Any]] = []
        self.started_at = datetime.now(timezone.utc)

    # -- status --------------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        labels = self.config.truth_labels()
        return {
            "service": "Stephanie.ai Voice",
            "brand": "NoblePort Systems",
            "status": "healthy",
            "synthesizer": self.voice_engine.synthesizer.name,
            "truth_labels": labels,
            "telephony_transport": self.twilio.transport.name,
            "sample_rate": self.config.SAMPLE_RATE,
            "voices": len(self.voice_engine.voice_profiles),
            "active_calls": len(self.twilio.active_calls),
            "uptime_s": (datetime.now(timezone.utc) - self.started_at).total_seconds(),
            "disclaimer": self.disclaimer,
        }

    # -- voice ----------------------------------------------------------------------
    def screen_text(self, text: str, allow_flagged: bool = False) -> None:
        result = self.text_processor.check_compliance(text)
        if not result.ok and not allow_flagged:
            raise ComplianceError(result.flagged_terms, result.approved_alternatives)

    async def generate_voice(self, text: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        settings = dict(settings or {})
        if not text or not text.strip():
            raise ValueError("text is required")
        self.screen_text(text, settings.get("allow_flagged", False))
        if settings.get("append_disclaimer"):
            text = f"{text.rstrip()} {self.disclaimer}"

        voice_id = settings.get("voice_id") or self.config.DEFAULT_VOICE_ID
        emotion = settings.get("emotion", "neutral")
        speed = float(settings.get("speed", 1.0))
        language = (settings.get("language") or "en-US")[:2].lower()

        defaults = self.elevenlabs.voice_settings

        def pick(key: str, default: float) -> float:
            value = settings.get(key)
            return float(default if value is None else value)

        audio = self.voice_engine.generate_speech(
            text, voice_id=voice_id, emotion=emotion, speed=speed, language=language,
            stability=pick("stability", defaults.stability),
            similarity_boost=pick("similarity_boost", defaults.similarity_boost),
            style=pick("style", defaults.style),
            emotion_intensity=pick("emotion_intensity", 1.0),
            custom_pronunciations=settings.get("custom_pronunciations"),
            seed=settings.get("seed"),
        )
        if settings.get("enhance_audio", True):
            audio = self.elevenlabs.enhance_audio(audio)
        background = settings.get("background_audio")
        if isinstance(background, str) and background:
            bed = self.elevenlabs.ambient_bed(background, ap.duration_seconds(audio, self.config.SAMPLE_RATE) + 0.5)
            audio = self.elevenlabs.add_background_audio(audio, bed, float(settings.get("background_volume", 0.1)))
        elif isinstance(background, np.ndarray):
            audio = self.elevenlabs.add_background_audio(audio, background, float(settings.get("background_volume", 0.1)))

        duration = ap.duration_seconds(audio, self.config.SAMPLE_RATE)
        if duration > self.config.MAX_AUDIO_DURATION:
            raise ValueError(f"generated audio ({duration:.0f}s) exceeds MAX_AUDIO_DURATION")

        profile = self.voice_engine.get_profile(voice_id)
        return {
            "audio": audio,
            "metadata": {
                "voice_id": voice_id,
                "voice_name": profile.name,
                "emotion": emotion,
                "speed": speed,
                "language": language,
                "duration": round(duration, 3),
                "sample_rate": self.config.SAMPLE_RATE,
                "synthesizer": self.voice_engine.synthesizer.name,
                "truth_label": self.voice_engine.synthesizer.truth_label,
                "characters": len(text),
            },
        }

    async def clone_voice(self, audio_sample: np.ndarray, name: str, sample_rate: Optional[int] = None,
                          description: str = "") -> Dict[str, Any]:
        profile = self.elevenlabs.instant_voice_cloning(audio_sample, name, sample_rate, description)
        return {"voice_id": profile.voice_id, "name": profile.name, "status": "cloned",
                "truth_label": profile.truth_label, "profile": profile.to_dict()}

    # -- telephony ------------------------------------------------------------------
    async def make_call(self, to_number: str, text: Optional[str] = None, voice_id: Optional[str] = None,
                        human_approval: Optional[str] = None, record: bool = False,
                        transcribe: bool = False) -> Dict[str, Any]:
        asset_url = None
        voice_meta = None
        if text:
            voice_data = await self.generate_voice(text, {"voice_id": voice_id, "append_disclaimer": True})
            asset_id = self.twilio.register_audio_asset(ap.encode_wav(voice_data["audio"], self.config.SAMPLE_RATE))
            asset_url = self.twilio.asset_url(asset_id)
            voice_meta = voice_data["metadata"]
        session = await self.twilio.make_voice_call(to_number, human_approval=human_approval)
        if asset_url:
            session.log("voice_prompt_registered", asset_url=asset_url)
        if record:
            await self.twilio.start_recording(session.session_id)
        if transcribe:
            await self.twilio.start_transcription(session.session_id)
        stream_id = await self.twilio.create_media_stream(session.session_id)
        return {"session_id": session.session_id, "status": session.status, "truth_label": session.truth_label,
                "stream_id": stream_id, "media_stream_url": self.twilio.media_stream_url(session.session_id),
                "prompt_audio_url": asset_url, "voice": voice_meta}

    async def send_message(self, to_number: str, message: str, human_approval: Optional[str] = None,
                           append_disclaimer: bool = False) -> Dict[str, Any]:
        self.screen_text(message)
        if append_disclaimer:
            message = f"{message.rstrip()}\n\n{self.disclaimer}"
        return await self.twilio.send_sms(to_number, message, human_approval=human_approval)

    async def voice_webhook_twiml(self, session_id: Optional[str], from_number: Optional[str],
                                  call_sid: Optional[str]) -> str:
        """TwiML for Twilio's voice webhook: play the prompt or greet an inbound caller."""
        from services.twilio_features import TwiML

        if session_id and session_id in self.twilio.active_calls:
            session = self.twilio.active_calls[session_id]
            prompt = next((e for e in reversed(session.events) if e["event"] == "voice_prompt_registered"), None)
            verbs = [TwiML.play(prompt["asset_url"])] if prompt else [TwiML.say("This is Stephanie from Noble Port.")]
            if session.transcription_enabled or session.recording_enabled:
                verbs.append(TwiML.stream(self.twilio.media_stream_url(session_id)))
            return TwiML.response(*verbs)
        session = await self.twilio.handle_inbound_call(from_number or "+10000000000", call_sid)
        greeting = "Thank you for calling Noble Port Systems. This is Stephanie, the automated assistant."
        menu = next(iter(self.twilio.ivr_menus.values()), None)
        if menu:
            return self.twilio.ivr_twiml(menu.ivr_id, session.session_id)
        return TwiML.response(TwiML.say(greeting), TwiML.say(self.disclaimer))

    # -- conversation -----------------------------------------------------------------
    async def process_conversation(self, text: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = dict(context or {})
        entry = {"input": text, "timestamp": datetime.now(timezone.utc).isoformat(), "context": context}
        self.conversation_history.append(entry)
        response = await self.generate_voice(text, context)
        entry["response_metadata"] = response["metadata"]
        return response

    def get_available_voices(self) -> List[Dict[str, Any]]:
        return [
            {"voice_id": p.voice_id, "name": p.name, "gender": p.gender, "language": p.language,
             "accent": p.accent, "description": p.description, "cloned": p.cloned, "truth_label": p.truth_label}
            for p in self.voice_engine.voice_profiles.values()
        ]

    def brand_intro_wav(self) -> Optional[bytes]:
        path = Path(self.config.BRAND_INTRO_PATH)
        return path.read_bytes() if path.is_file() else None
