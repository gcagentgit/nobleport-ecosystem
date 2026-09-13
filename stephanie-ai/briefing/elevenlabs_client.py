"""Minimal ElevenLabs REST client (httpx). No SDK, no fallback voice.

Every call returns evidence (status, request id, latency, byte counts). If the
key is missing or rejected this raises — it never silently degrades to text or
to a local voice, because that is exactly how earlier "ready" claims went wrong.

Endpoints used (ElevenLabs public API v1):
  GET  /v1/user                              auth check + character quota
  GET  /v1/voices                            voice library
  GET  /v1/voices/{voice_id}                 one voice
  POST /v1/text-to-speech/{voice_id}         synthesis (query: output_format)
Header: xi-api-key
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

DEFAULT_BASE_URL = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_OUTPUT_FORMAT = "mp3_44100_128"
ENV_API_KEY = "ELEVENLABS_API_KEY"
ENV_VOICE_ID = "STEPHANIE_ELEVENLABS_VOICE_ID"
ENV_VOICE_NAME = "STEPHANIE_ELEVENLABS_VOICE_NAME"
ENV_MODEL_ID = "ELEVENLABS_MODEL_ID"


class ElevenLabsError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body[:500]


@dataclass
class VoiceSettings:
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = True
    speed: Optional[float] = None  # ElevenLabs accepts 0.7–1.2; None = provider default

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stability": self.stability,
            "similarity_boost": self.similarity_boost,
            "style": self.style,
            "use_speaker_boost": self.use_speaker_boost,
        }
        if self.speed is not None:
            if not 0.7 <= self.speed <= 1.2:
                raise ValueError("ElevenLabs speed must be between 0.7 and 1.2")
            payload["speed"] = self.speed
        return payload


@dataclass
class SynthesisResult:
    audio: bytes
    voice_id: str
    model_id: str
    output_format: str
    characters: int
    latency_ms: int
    request_id: Optional[str] = None
    history_item_id: Optional[str] = None
    content_type: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def bytes(self) -> int:
        return len(self.audio)


class ElevenLabsClient:
    def __init__(self, api_key: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, timeout: float = 90.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self.api_key = api_key or os.getenv(ENV_API_KEY) or ""
        if not self.api_key:
            raise ElevenLabsError(f"{ENV_API_KEY} is not set — no authenticated access to ElevenLabs")
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport,
                                    headers={"xi-api-key": self.api_key, "accept": "application/json"})

    # -- helpers -------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = self._client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise ElevenLabsError(f"network error calling ElevenLabs {path}: {exc}") from exc
        if resp.status_code == 401:
            raise ElevenLabsError("ElevenLabs rejected the API key (401)", 401, resp.text)
        if resp.status_code == 404 and "/voices/" in path or (resp.status_code == 404 and "/text-to-speech/" in path):
            raise ElevenLabsError("voice_id not found in this ElevenLabs account (404)", 404, resp.text)
        if resp.status_code >= 400:
            raise ElevenLabsError(f"ElevenLabs {method} {path} failed with HTTP {resp.status_code}",
                                  resp.status_code, resp.text)
        return resp

    # -- API -------------------------------------------------------------------------
    def check_auth(self) -> Dict[str, Any]:
        """GET /user — proves the key works and reports remaining characters."""
        started = time.perf_counter()
        data = self._request("GET", "/user").json()
        sub = data.get("subscription", {}) or {}
        return {
            "ok": True,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "tier": sub.get("tier"),
            "character_count": sub.get("character_count"),
            "character_limit": sub.get("character_limit"),
            "characters_remaining": (sub.get("character_limit") or 0) - (sub.get("character_count") or 0)
            if sub.get("character_limit") is not None else None,
            "next_reset_unix": sub.get("next_character_count_reset_unix"),
            "user_id": data.get("user_id") or data.get("xi_api_key_preview"),
        }

    def list_voices(self) -> List[Dict[str, Any]]:
        data = self._request("GET", "/voices").json()
        voices = []
        for v in data.get("voices", []):
            voices.append({
                "voice_id": v.get("voice_id"),
                "name": v.get("name"),
                "category": v.get("category"),
                "labels": v.get("labels") or {},
                "preview_url": v.get("preview_url"),
                "description": v.get("description"),
            })
        return voices

    def get_voice(self, voice_id: str) -> Dict[str, Any]:
        v = self._request("GET", f"/voices/{voice_id}").json()
        return {"voice_id": v.get("voice_id"), "name": v.get("name"), "category": v.get("category"),
                "labels": v.get("labels") or {}, "settings": v.get("settings")}

    def resolve_voice(self, voice_id: Optional[str] = None, voice_name: Optional[str] = None) -> Dict[str, Any]:
        """Resolve the canonical Stephanie voice.

        Preference: explicit voice_id (must exist in the account) → exact
        case-insensitive name match (must be unique). Anything else is an error
        with the account's voice list attached so a human can choose.
        """
        voice_id = (voice_id or os.getenv(ENV_VOICE_ID) or "").strip()
        voice_name = (voice_name or os.getenv(ENV_VOICE_NAME) or "").strip()
        if voice_id:
            voice = self.get_voice(voice_id)
            voice["resolved_by"] = "voice_id"
            return voice
        voices = self.list_voices()
        if voice_name:
            matches = [v for v in voices if (v["name"] or "").strip().lower() == voice_name.lower()]
            if len(matches) == 1:
                matches[0]["resolved_by"] = f"name:{voice_name}"
                return matches[0]
            if not matches:
                raise ElevenLabsError(f"no voice named '{voice_name}' in this account; available: "
                                      + ", ".join(f"{v['name']} ({v['voice_id']})" for v in voices))
            raise ElevenLabsError(f"{len(matches)} voices named '{voice_name}' — set {ENV_VOICE_ID} to one of: "
                                  + ", ".join(v["voice_id"] for v in matches))
        raise ElevenLabsError(
            f"canonical Stephanie voice not chosen: set {ENV_VOICE_ID} (preferred) or {ENV_VOICE_NAME}. "
            "Account voices: " + (", ".join(f"{v['name']} ({v['voice_id']})" for v in voices) or "none"))

    def synthesize(self, text: str, voice_id: str, model_id: Optional[str] = None,
                   output_format: str = DEFAULT_OUTPUT_FORMAT, settings: Optional[VoiceSettings] = None,
                   previous_text: Optional[str] = None, next_text: Optional[str] = None) -> SynthesisResult:
        if not text.strip():
            raise ValueError("text is empty")
        model_id = model_id or os.getenv(ENV_MODEL_ID) or DEFAULT_MODEL_ID
        payload: Dict[str, Any] = {"text": text, "model_id": model_id,
                                   "voice_settings": (settings or VoiceSettings()).to_payload()}
        if previous_text:
            payload["previous_text"] = previous_text
        if next_text:
            payload["next_text"] = next_text
        started = time.perf_counter()
        resp = self._request("POST", f"/text-to-speech/{voice_id}", params={"output_format": output_format},
                             json=payload, headers={"accept": "audio/mpeg" if output_format.startswith("mp3") else "*/*"})
        audio = resp.content
        if not audio:
            raise ElevenLabsError("ElevenLabs returned an empty audio body", resp.status_code)
        return SynthesisResult(
            audio=audio, voice_id=voice_id, model_id=model_id, output_format=output_format,
            characters=len(text), latency_ms=int((time.perf_counter() - started) * 1000),
            request_id=resp.headers.get("request-id") or resp.headers.get("x-request-id"),
            history_item_id=resp.headers.get("history-item-id"),
            content_type=resp.headers.get("content-type", ""),
        )

    def close(self) -> None:
        self._client.close()
