"""Stephanie.ai voice engine.

Two synthesis providers sit behind one interface:

* ``LocalFormantSynthesizer`` — always available. A deterministic, numpy-only
  formant/prosody synthesizer that turns text into a speech-shaped signal
  (syllable envelopes, sentence intonation, per-voice F0 and formants). It is
  labelled **STAGED**: it demonstrates the pipeline end-to-end, it is not a
  neural voice.
* ``ElevenLabsSynthesizer`` — used automatically when ``ELEVENLABS_API_KEY`` is
  set. Requests raw PCM from the ElevenLabs text-to-speech API so the rest of
  the pipeline (emotion, enhancement, mixing, telephony) is provider-agnostic.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Tuple

import numpy as np

from models import audio_processor as ap
from models.text_processor import TextProcessor

log = logging.getLogger("stephanie.voice_engine")


# ---------------------------------------------------------------------------
# Voice profiles
# ---------------------------------------------------------------------------

@dataclass
class VoiceProfile:
    voice_id: str
    name: str
    gender: str
    age_range: str
    accent: str
    speaking_rate: float = 1.0
    pitch: float = 1.0            # multiplier on the base F0
    emotion: str = "neutral"
    neural_voice: bool = False    # True only when backed by a neural provider
    language: str = "en-US"
    description: str = ""
    base_f0_hz: float = 210.0
    formants_hz: Tuple[float, float, float] = (650.0, 1700.0, 2600.0)
    breathiness: float = 0.06
    provider_voice_id: Optional[str] = None   # e.g. ElevenLabs voice id
    cloned: bool = False
    truth_label: str = "STAGED"

    def to_dict(self) -> Dict:
        return asdict(self)


DEFAULT_PROFILES: List[VoiceProfile] = [
    VoiceProfile(
        voice_id="stephanie_primary", name="Stephanie", gender="female", age_range="25-35",
        accent="American (Boston)", speaking_rate=1.0, pitch=1.0, emotion="neutral",
        language="en-US", base_f0_hz=210.0,
        description="NoblePort executive orchestration assistant — default voice.",
    ),
    VoiceProfile(
        voice_id="stephanie_warm", name="Stephanie Warm", gender="female", age_range="25-35",
        accent="American (Boston)", speaking_rate=0.92, pitch=1.06, emotion="warm",
        language="en-US", base_f0_hz=215.0, formants_hz=(620.0, 1650.0, 2550.0), breathiness=0.1,
        description="Softer, slower delivery for client intake and customer portal prompts.",
    ),
    VoiceProfile(
        voice_id="stephanie_professional", name="Stephanie Professional", gender="female",
        age_range="30-40", accent="American (General)", speaking_rate=1.05, pitch=0.96,
        emotion="neutral", language="en-US", base_f0_hz=200.0, formants_hz=(680.0, 1750.0, 2700.0),
        breathiness=0.03,
        description="Crisp, even delivery for estimates, permit status and audit read-backs.",
    ),
    VoiceProfile(
        voice_id="stephanie_dispatch", name="Stephanie Dispatch", gender="female",
        age_range="30-40", accent="American (General)", speaking_rate=1.15, pitch=1.02,
        emotion="neutral", language="en-US", base_f0_hz=212.0, breathiness=0.02,
        description="Fast, high-clarity delivery for jobsite IVR menus and queue announcements.",
    ),
]


EMOTION_PARAMS: Dict[str, Dict[str, float]] = {
    "neutral":  {"pitch": 1.00, "tempo": 1.00, "energy": 1.00, "range": 1.00},
    "warm":     {"pitch": 1.03, "tempo": 0.95, "energy": 0.95, "range": 1.10},
    "happy":    {"pitch": 1.08, "tempo": 1.08, "energy": 1.15, "range": 1.35},
    "sad":      {"pitch": 0.92, "tempo": 0.85, "energy": 0.75, "range": 0.60},
    "angry":    {"pitch": 1.04, "tempo": 1.20, "energy": 1.40, "range": 0.90},
    "excited":  {"pitch": 1.12, "tempo": 1.25, "energy": 1.45, "range": 1.50},
    "calm":     {"pitch": 0.96, "tempo": 0.90, "energy": 0.85, "range": 0.75},
    "surprised": {"pitch": 1.10, "tempo": 1.05, "energy": 1.20, "range": 1.60},
}

SUPPORTED_LANGUAGES: Dict[str, Dict] = {
    "en": {"name": "English", "rate": 1.00, "f0": 1.00},
    "es": {"name": "Spanish", "rate": 1.08, "f0": 1.02},
    "fr": {"name": "French", "rate": 1.02, "f0": 1.04},
    "de": {"name": "German", "rate": 0.95, "f0": 0.98},
    "it": {"name": "Italian", "rate": 1.06, "f0": 1.03},
    "pt": {"name": "Portuguese", "rate": 1.04, "f0": 1.01},
    "pl": {"name": "Polish", "rate": 0.98, "f0": 1.00},
    "hi": {"name": "Hindi", "rate": 1.00, "f0": 1.03},
    "ar": {"name": "Arabic", "rate": 0.97, "f0": 0.99},
    "zh": {"name": "Chinese (Mandarin)", "rate": 0.94, "f0": 1.05},
}


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

@dataclass
class SynthesisRequest:
    text: str
    profile: VoiceProfile
    emotion: str = "neutral"
    speed: float = 1.0
    language: str = "en"
    stability: float = 0.75
    similarity_boost: float = 0.75
    style: float = 0.0
    emotion_intensity: float = 1.0
    seed: Optional[int] = None


class Synthesizer(Protocol):
    name: str
    truth_label: str

    def synthesize(self, request: SynthesisRequest, sample_rate: int) -> np.ndarray: ...


class LocalFormantSynthesizer:
    """Deterministic formant synthesizer — STAGED stand-in for a neural voice."""

    name = "local_formant"
    truth_label = "STAGED"

    _VOWEL_FORMANT_SCALE = [
        (1.00, 1.00, 1.00),  # neutral
        (0.55, 1.35, 1.05),  # /i/
        (1.15, 0.75, 0.95),  # /o/
        (1.25, 1.05, 1.00),  # /a/
        (0.65, 0.65, 0.90),  # /u/
        (0.85, 1.20, 1.10),  # /e/
    ]

    def __init__(self, text_processor: Optional[TextProcessor] = None):
        self.text_processor = text_processor or TextProcessor()

    # ------------------------------------------------------------------
    def synthesize(self, request: SynthesisRequest, sample_rate: int) -> np.ndarray:
        profile = request.profile
        emo = EMOTION_PARAMS.get(request.emotion, EMOTION_PARAMS["neutral"])
        intensity = float(np.clip(request.emotion_intensity, 0.0, 1.5))
        lang = SUPPORTED_LANGUAGES.get(request.language[:2].lower(), SUPPORTED_LANGUAGES["en"])

        def blend(v: float) -> float:
            return 1.0 + (v - 1.0) * intensity

        f0 = profile.base_f0_hz * profile.pitch * blend(emo["pitch"]) * lang["f0"]
        tempo = profile.speaking_rate * request.speed * blend(emo["tempo"]) * lang["rate"]
        energy = blend(emo["energy"])
        pitch_range = blend(emo["range"]) * (1.0 + request.style * 0.5)
        jitter = 0.02 * (1.0 - request.stability) + 0.004
        seed = request.seed if request.seed is not None else self._seed_for(request.text, profile.voice_id)
        rng = np.random.default_rng(seed)

        chunks = self.text_processor.chunk(request.text)
        pieces: List[np.ndarray] = [ap.silence(0.05, sample_rate)]
        for chunk in chunks:
            pieces.append(self._render_chunk(chunk.text, chunk.pause_after, f0, tempo, energy,
                                             pitch_range, jitter, profile, sample_rate, rng))
        pieces.append(ap.silence(0.08, sample_rate))
        audio = np.concatenate(pieces) if pieces else ap.silence(0.1, sample_rate)
        return ap.fade(ap.normalize(audio, 0.85), sample_rate)

    # ------------------------------------------------------------------
    @staticmethod
    def _seed_for(text: str, voice_id: str) -> int:
        digest = hashlib.sha256(f"{voice_id}|{text}".encode()).digest()
        return int.from_bytes(digest[:4], "little")

    def _render_chunk(self, text: str, pause_after: float, f0: float, tempo: float, energy: float,
                      pitch_range: float, jitter: float, profile: VoiceProfile, sr: int,
                      rng: np.random.Generator) -> np.ndarray:
        words = list(self.text_processor.words(text))
        if not words:
            return ap.silence(pause_after, sr)
        is_question = text.rstrip().endswith("?")
        syllable_total = sum(self.text_processor.syllables(w) for w in words)
        out: List[np.ndarray] = []
        syl_index = 0
        for wi, word in enumerate(words):
            n_syl = self.text_processor.syllables(word)
            stressed = 0 if n_syl <= 2 else 1
            for si in range(n_syl):
                progress = syl_index / max(1, syllable_total - 1)
                # Sentence contour: declarative falls, question rises at the end.
                contour = (0.06 - 0.16 * progress) if not is_question else (-0.04 + 0.22 * progress ** 2)
                contour *= pitch_range
                stress = 0.10 * pitch_range if si == stressed else 0.0
                syl_f0 = f0 * (1.0 + contour + stress) * (1.0 + rng.normal(0.0, jitter))
                base_dur = 0.19 if si == stressed else 0.14
                dur = max(0.05, base_dur / tempo + rng.normal(0.0, 0.012))
                vowel = self._VOWEL_FORMANT_SCALE[(sum(map(ord, word)) + si) % len(self._VOWEL_FORMANT_SCALE)]
                amp = energy * (1.0 if si == stressed else 0.8)
                out.append(self._syllable(syl_f0, dur, vowel, amp, profile, sr, rng))
                syl_index += 1
            out.append(ap.silence(0.045 / tempo, sr))
        out.append(ap.silence(pause_after / tempo, sr))
        return np.concatenate(out)

    def _syllable(self, f0: float, dur: float, vowel: Tuple[float, float, float], amp: float,
                  profile: VoiceProfile, sr: int, rng: np.random.Generator) -> np.ndarray:
        n = max(8, int(dur * sr))
        t = np.arange(n, dtype=np.float32) / sr
        # Glottal source: harmonic series with 1/k roll-off, slight vibrato.
        vibrato = 1.0 + 0.006 * np.sin(2 * np.pi * 5.5 * t)
        phase = 2 * np.pi * f0 * np.cumsum(vibrato) / sr
        source = np.zeros(n, dtype=np.float32)
        for k in range(1, 12):
            source += (1.0 / k) * np.sin(k * phase).astype(np.float32)
        # Vocal tract: emphasise three formants via FFT-domain resonances.
        spectrum = np.fft.rfft(source)
        freqs = np.fft.rfftfreq(n, d=1.0 / sr)
        response = np.full(freqs.shape, 0.08, dtype=np.float32)
        for (centre, scale), bw, gain in zip(zip(profile.formants_hz, vowel), (80.0, 120.0, 160.0), (1.0, 0.7, 0.4)):
            fc = centre * scale
            response += gain * np.exp(-0.5 * ((freqs - fc) / bw) ** 2).astype(np.float32)
        response *= np.exp(-freqs / 6000.0).astype(np.float32)
        voiced = np.fft.irfft(spectrum * response, n=n).astype(np.float32)
        # Aspiration noise adds breathiness.
        noise = rng.normal(0.0, 1.0, n).astype(np.float32)
        noise = ap.remove_noise(noise, sr, noise_floor=0.0, cutoff_hz=5000.0)
        signal = voiced + profile.breathiness * noise * (np.max(np.abs(voiced)) + 1e-6)
        # Syllable envelope: quick attack, sustained, gentle release.
        attack = max(1, int(0.02 * sr))
        release = max(1, int(0.05 * sr))
        env = np.ones(n, dtype=np.float32)
        env[:attack] = np.linspace(0.0, 1.0, attack, dtype=np.float32)
        env[-release:] *= np.linspace(1.0, 0.0, release, dtype=np.float32)[: min(release, n)]
        signal = signal * env
        peak = float(np.max(np.abs(signal))) or 1.0
        return (signal / peak * 0.6 * amp).astype(np.float32)


class ElevenLabsSynthesizer:
    """ElevenLabs text-to-speech via HTTP (LIVE only when an API key exists)."""

    name = "elevenlabs"
    truth_label = "LIVE"

    _PCM_RATES = (16000, 22050, 24000, 44100)

    def __init__(self, api_key: str, base_url: str, model_id: str, timeout: float = 60.0):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_id = model_id
        self.timeout = timeout

    def synthesize(self, request: SynthesisRequest, sample_rate: int) -> np.ndarray:
        import httpx  # local import keeps the module importable without httpx

        voice_id = request.profile.provider_voice_id or request.profile.voice_id
        pcm_rate = sample_rate if sample_rate in self._PCM_RATES else 24000
        emo = EMOTION_PARAMS.get(request.emotion, EMOTION_PARAMS["neutral"])
        payload = {
            "text": request.text,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": float(request.stability),
                "similarity_boost": float(request.similarity_boost),
                "style": float(np.clip(request.style + (emo["range"] - 1.0) * 0.3, 0.0, 1.0)),
                "use_speaker_boost": True,
                "speed": float(np.clip(request.speed * emo["tempo"], 0.7, 1.2)),
            },
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                f"{self.base_url}/text-to-speech/{voice_id}",
                params={"output_format": f"pcm_{pcm_rate}"},
                headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()
        pcm = np.frombuffer(resp.content, dtype=np.int16)
        audio = ap.from_int16(pcm)
        return ap.resample(audio, pcm_rate, sample_rate)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class StephanieVoiceEngine:
    """Voice profile registry + synthesis front door."""

    def __init__(self, sample_rate: int = 44100, synthesizer: Optional[Synthesizer] = None,
                 text_processor: Optional[TextProcessor] = None, library_path: Optional[str] = None):
        self.sample_rate = sample_rate
        self.text_processor = text_processor or TextProcessor()
        self.synthesizer: Synthesizer = synthesizer or LocalFormantSynthesizer(self.text_processor)
        self.voice_profiles: Dict[str, VoiceProfile] = {}
        self.library_path = Path(library_path) if library_path else None
        self.initialize_voices()

    # -- registry -------------------------------------------------------------
    def initialize_voices(self) -> None:
        for profile in DEFAULT_PROFILES:
            self.voice_profiles[profile.voice_id] = VoiceProfile(**profile.to_dict())
        self._load_library()

    def _load_library(self) -> None:
        if not self.library_path or not self.library_path.is_file():
            return
        try:
            for item in json.loads(self.library_path.read_text()):
                item["formants_hz"] = tuple(item.get("formants_hz", (650.0, 1700.0, 2600.0)))
                profile = VoiceProfile(**item)
                self.voice_profiles[profile.voice_id] = profile
        except (OSError, ValueError, TypeError) as exc:
            log.warning("could not load voice library %s: %s", self.library_path, exc)

    def save_library(self) -> None:
        if not self.library_path:
            return
        self.library_path.parent.mkdir(parents=True, exist_ok=True)
        cloned = [p.to_dict() for p in self.voice_profiles.values() if p.cloned]
        self.library_path.write_text(json.dumps(cloned, indent=2))

    def get_profile(self, voice_id: str) -> VoiceProfile:
        try:
            return self.voice_profiles[voice_id]
        except KeyError:
            raise KeyError(f"unknown voice_id '{voice_id}'") from None

    def list_voices(self) -> List[Dict]:
        return [p.to_dict() for p in self.voice_profiles.values()]

    def delete_voice(self, voice_id: str) -> bool:
        profile = self.voice_profiles.get(voice_id)
        if not profile or not profile.cloned:
            return False
        del self.voice_profiles[voice_id]
        self.save_library()
        return True

    # -- synthesis --------------------------------------------------------------
    def generate_speech(self, text: str, voice_id: str = "stephanie_primary", emotion: str = "neutral",
                        speed: float = 1.0, language: str = "en", stability: float = 0.75,
                        similarity_boost: float = 0.75, style: float = 0.0, emotion_intensity: float = 1.0,
                        custom_pronunciations: Optional[Dict[str, str]] = None,
                        seed: Optional[int] = None) -> np.ndarray:
        if emotion not in EMOTION_PARAMS:
            raise ValueError(f"unknown emotion '{emotion}' (choose from {sorted(EMOTION_PARAMS)})")
        if not 0.5 <= speed <= 2.0:
            raise ValueError("speed must be between 0.5 and 2.0")
        profile = self.get_profile(voice_id)
        processed = self.text_processor.normalize(text, custom_pronunciations)
        if not processed:
            raise ValueError("text is empty after normalisation")
        request = SynthesisRequest(
            text=processed, profile=profile, emotion=emotion, speed=speed, language=language,
            stability=stability, similarity_boost=similarity_boost, style=style,
            emotion_intensity=emotion_intensity, seed=seed,
        )
        return self.synthesizer.synthesize(request, self.sample_rate)

    def preprocess_text(self, text: str) -> str:
        return self.text_processor.normalize(text)

    # -- cloning ----------------------------------------------------------------
    def extract_voice_features(self, audio: np.ndarray, sample_rate: Optional[int] = None) -> Dict:
        sr = sample_rate or self.sample_rate
        pitch = ap.estimate_pitch_hz(audio, sr)
        if pitch <= 0:
            pitch = 200.0
        gender = "female" if pitch > 165 else "male"
        # Rough speaking-rate proxy: envelope peaks per second.
        env = np.abs(audio)
        win = max(1, int(sr * 0.05))
        smooth = np.convolve(env, np.ones(win) / win, mode="same") if env.size else env
        thresh = float(np.max(smooth)) * 0.4 if smooth.size else 0.0
        peaks = int(np.sum((smooth[1:-1] > thresh) & (smooth[1:-1] > smooth[:-2]) & (smooth[1:-1] >= smooth[2:]))) if smooth.size > 2 else 0
        seconds = ap.duration_seconds(audio, sr) or 1.0
        syllables_per_sec = peaks / seconds
        speaking_rate = float(np.clip(syllables_per_sec / 4.0, 0.7, 1.4)) if peaks else 1.0
        return {
            "gender": gender,
            "age_range": "25-45",
            "accent": "unknown",
            "speaking_rate": speaking_rate,
            "pitch_hz": float(pitch),
            "pitch": float(pitch) / 200.0,
            "language": "en-US",
            "rms": ap.rms(audio),
            "duration_s": seconds,
        }

    def clone_voice(self, audio: np.ndarray, name: str, sample_rate: Optional[int] = None,
                    description: str = "") -> VoiceProfile:
        if audio.size < (sample_rate or self.sample_rate):
            raise ValueError("voice sample must be at least one second long")
        slug = "".join(ch if ch.isalnum() else "_" for ch in name.strip().lower()).strip("_") or "voice"
        voice_id = f"cloned_{slug}"
        features = self.extract_voice_features(audio, sample_rate)
        profile = VoiceProfile(
            voice_id=voice_id, name=name.strip(), gender=features["gender"],
            age_range=features["age_range"], accent=features["accent"],
            speaking_rate=features["speaking_rate"], pitch=1.0, emotion="neutral",
            neural_voice=False, language=features["language"], description=description,
            base_f0_hz=float(np.clip(features["pitch_hz"], 80.0, 400.0)), cloned=True,
            truth_label="STAGED",
        )
        self.voice_profiles[voice_id] = profile
        self.save_library()
        return profile
