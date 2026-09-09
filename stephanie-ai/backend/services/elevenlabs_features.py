"""ElevenLabs-inspired feature set for Stephanie.ai.

Ten capabilities, each implemented on top of the shared voice engine and the
numpy audio processor. When an ElevenLabs API key is configured the engine's
synthesizer is the real ElevenLabs client; otherwise everything runs on the
STAGED local synthesizer. Either way these features behave the same.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import asdict, dataclass
from typing import Dict, Generator, Iterable, List, Optional

import numpy as np

from models import audio_processor as ap
from models.voice_engine import EMOTION_PARAMS, SUPPORTED_LANGUAGES, StephanieVoiceEngine, VoiceProfile


@dataclass
class VoiceSettings:
    stability: float = 0.75
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True

    def validated(self) -> "VoiceSettings":
        for name in ("stability", "similarity_boost", "style"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        return self


class ElevenLabsFeatures:
    """Top-ten ElevenLabs capabilities, Stephanie edition."""

    def __init__(self, engine: StephanieVoiceEngine, share_secret: str = "stephanie-voice"):
        self.engine = engine
        self.voice_settings = VoiceSettings()
        self.share_secret = share_secret.encode()

    @property
    def sample_rate(self) -> int:
        return self.engine.sample_rate

    # 1. Instant voice cloning ------------------------------------------------
    def instant_voice_cloning(self, audio_sample: np.ndarray, name: str,
                              sample_rate: Optional[int] = None, description: str = "") -> VoiceProfile:
        return self.engine.clone_voice(audio_sample, name, sample_rate, description)

    # 2. Multilingual synthesis -----------------------------------------------
    def multilingual_synthesis(self, text: str, target_language: str = "en",
                               voice_id: str = "stephanie_primary", **kwargs) -> np.ndarray:
        lang = target_language[:2].lower()
        if lang not in SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported language '{target_language}' (choose from {sorted(SUPPORTED_LANGUAGES)})")
        return self.engine.generate_speech(text, voice_id=voice_id, language=lang, **self._settings_kwargs(kwargs))

    @staticmethod
    def supported_languages() -> List[Dict[str, str]]:
        return [{"code": code, "name": meta["name"]} for code, meta in SUPPORTED_LANGUAGES.items()]

    # 3. Voice settings control -----------------------------------------------
    def adjust_voice_settings(self, stability: Optional[float] = None, similarity: Optional[float] = None,
                              style: Optional[float] = None, use_speaker_boost: Optional[bool] = None) -> VoiceSettings:
        current = self.voice_settings
        self.voice_settings = VoiceSettings(
            stability=current.stability if stability is None else stability,
            similarity_boost=current.similarity_boost if similarity is None else similarity,
            style=current.style if style is None else style,
            use_speaker_boost=current.use_speaker_boost if use_speaker_boost is None else use_speaker_boost,
        ).validated()
        return self.voice_settings

    def _settings_kwargs(self, overrides: Dict) -> Dict:
        merged = {
            "stability": self.voice_settings.stability,
            "similarity_boost": self.voice_settings.similarity_boost,
            "style": self.voice_settings.style,
        }
        merged.update({k: v for k, v in overrides.items() if v is not None})
        return merged

    # 4. Real-time streaming --------------------------------------------------
    def stream_voice(self, text: str, voice_id: str = "stephanie_primary", emotion: str = "neutral",
                     speed: float = 1.0, words_per_chunk: int = 6, chunk_samples: int = 4096,
                     **kwargs) -> Generator[Dict, None, None]:
        """Yield PCM16 chunks as soon as each text segment is synthesised."""
        chunks = self.engine.text_processor.chunk(self.engine.preprocess_text(text), max_words=words_per_chunk)
        total = len(chunks)
        for segment in chunks:
            audio = self.engine.generate_speech(segment.text, voice_id=voice_id, emotion=emotion, speed=speed,
                                                **self._settings_kwargs(kwargs))
            audio = np.concatenate([audio, ap.silence(segment.pause_after, self.sample_rate)])
            pcm = ap.to_int16(audio)
            for start in range(0, pcm.size, chunk_samples):
                piece = pcm[start : start + chunk_samples]
                yield {
                    "segment": segment.index,
                    "segments_total": total,
                    "text": segment.text,
                    "sample_rate": self.sample_rate,
                    "encoding": "pcm_s16le",
                    "pcm": piece.tobytes(),
                    "final": segment.index == total - 1 and start + chunk_samples >= pcm.size,
                }

    # 5. Voice library management --------------------------------------------
    def manage_voice_library(self, action: str, voice_id: Optional[str] = None) -> Dict:
        if action == "list":
            return {"voices": self.engine.list_voices()}
        if not voice_id:
            raise ValueError("voice_id is required for this action")
        if action == "get":
            return {"voice": self.engine.get_profile(voice_id).to_dict()}
        if action == "delete":
            if not self.engine.delete_voice(voice_id):
                raise ValueError("only cloned voices can be deleted")
            return {"deleted": voice_id}
        if action == "share":
            self.engine.get_profile(voice_id)  # raises for unknown voices
            return {"voice_id": voice_id, "share_link": self.generate_share_link(voice_id)}
        raise ValueError(f"unknown library action '{action}'")

    def generate_share_link(self, voice_id: str, base_url: str = "https://stephanie.nobleport.ai") -> str:
        token = hmac.new(self.share_secret, voice_id.encode(), hashlib.sha256).hexdigest()[:16]
        return f"{base_url}/voice/{voice_id}?share={token}"

    def verify_share_link(self, voice_id: str, token: str) -> bool:
        expected = hmac.new(self.share_secret, voice_id.encode(), hashlib.sha256).hexdigest()[:16]
        return hmac.compare_digest(expected, token)

    # 6. Emotion control ------------------------------------------------------
    def apply_emotion(self, audio: np.ndarray, emotion: str, intensity: float = 0.5) -> np.ndarray:
        """Post-process an existing signal toward an emotion (energy + tempo).

        Pitch is applied at synthesis time by the engine; here we shape what
        can be shaped after the fact so the feature also works on uploaded or
        provider audio.
        """
        if emotion not in EMOTION_PARAMS:
            raise ValueError(f"unknown emotion '{emotion}'")
        params = EMOTION_PARAMS[emotion]
        intensity = float(np.clip(intensity, 0.0, 1.0))
        energy = 1.0 + (params["energy"] - 1.0) * intensity
        tempo = 1.0 + (params["tempo"] - 1.0) * intensity
        shaped = ap.change_speed(audio, tempo) if abs(tempo - 1.0) > 1e-3 else audio
        return np.clip(shaped * energy, -1.0, 1.0).astype(np.float32)

    @staticmethod
    def emotions() -> List[str]:
        return sorted(EMOTION_PARAMS)

    # 7. Audio quality enhancement -------------------------------------------
    def enhance_audio(self, audio: np.ndarray, sample_rate: Optional[int] = None) -> np.ndarray:
        sr = sample_rate or self.sample_rate
        audio = ap.remove_noise(audio, sr)
        audio = ap.compress_dynamic_range(audio)
        audio = ap.apply_eq(audio, sr)
        if self.voice_settings.use_speaker_boost:
            audio = ap.apply_eq(audio, sr, low=0.95, mid=1.1, high=1.0)
        return ap.normalize(audio, 0.9)

    # 8. Custom pronunciation -------------------------------------------------
    def customize_pronunciation(self, text: str, custom_dict: Dict[str, str]) -> str:
        return self.engine.text_processor.apply_pronunciations(text, custom_dict)

    def register_pronunciations(self, custom_dict: Dict[str, str]) -> Dict[str, str]:
        self.engine.text_processor.custom_pronunciations.update(custom_dict)
        return dict(self.engine.text_processor.custom_pronunciations)

    # 9. Voice mixing ---------------------------------------------------------
    def mix_voices(self, voice1: np.ndarray, voice2: np.ndarray, blend_ratio: float = 0.5) -> np.ndarray:
        if not 0.0 <= blend_ratio <= 1.0:
            raise ValueError("blend_ratio must be between 0 and 1")
        return ap.mix(voice1, voice2, blend_ratio)

    def mix_voice_profiles(self, text: str, voice_a: str, voice_b: str, blend_ratio: float = 0.5, **kwargs) -> np.ndarray:
        a = self.engine.generate_speech(text, voice_id=voice_a, **kwargs)
        b = self.engine.generate_speech(text, voice_id=voice_b, **kwargs)
        return self.mix_voices(a, b, blend_ratio)

    # 10. Background audio ----------------------------------------------------
    def add_background_audio(self, voice: np.ndarray, background: np.ndarray, volume: float = 0.1) -> np.ndarray:
        if not 0.0 <= volume <= 1.0:
            raise ValueError("volume must be between 0 and 1")
        return ap.add_background(voice, background, volume)

    def ambient_bed(self, kind: str, seconds: float, seed: int = 7) -> np.ndarray:
        """Generated ambience so the feature works without shipping audio files."""
        sr = self.sample_rate
        n = int(seconds * sr)
        rng = np.random.default_rng(seed)
        t = np.arange(n, dtype=np.float32) / sr
        if kind == "office":
            bed = 0.4 * rng.normal(0, 1, n).astype(np.float32)
            bed = ap.remove_noise(bed, sr, noise_floor=0.0, cutoff_hz=1200.0)
        elif kind == "jobsite":
            bed = 0.5 * rng.normal(0, 1, n).astype(np.float32)
            bed = ap.remove_noise(bed, sr, noise_floor=0.0, cutoff_hz=3000.0)
            bed += 0.3 * np.sin(2 * np.pi * 55.0 * t) * (np.sin(2 * np.pi * 0.5 * t) > 0)
        elif kind == "hold_music":
            bed = np.zeros(n, dtype=np.float32)
            for i, f in enumerate((261.6, 329.6, 392.0, 523.3)):
                bed += 0.2 * np.sin(2 * np.pi * f * t) * (np.sin(2 * np.pi * (0.25 + i * 0.05) * t) > 0)
        else:
            raise ValueError("kind must be one of office, jobsite, hold_music")
        return ap.normalize(bed, 0.5)

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def encode_base64_wav(audio: np.ndarray, sample_rate: int) -> str:
        return base64.b64encode(ap.encode_wav(audio, sample_rate)).decode()

    def settings_dict(self) -> Dict:
        return asdict(self.voice_settings)
