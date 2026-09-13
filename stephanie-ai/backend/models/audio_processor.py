"""Pure-numpy audio utilities: encoding, dynamics, EQ, mixing, resampling.

No librosa / torch. Everything here runs on a bare Linux box with numpy only,
which is what keeps the backend deployable on the NoblePort dev containers.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import wave
from typing import Optional, Tuple

import numpy as np

FloatAudio = np.ndarray  # mono float32 in [-1, 1]


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def to_int16(audio: FloatAudio) -> np.ndarray:
    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def from_int16(pcm: np.ndarray) -> FloatAudio:
    return (pcm.astype(np.float32) / 32768.0).astype(np.float32)


def encode_wav(audio: FloatAudio, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(to_int16(audio).tobytes())
    return buf.getvalue()


def decode_wav(data: bytes) -> Tuple[FloatAudio, int]:
    """Decode 8/16/32-bit PCM WAV (mono or multi-channel → mono)."""
    with wave.open(io.BytesIO(data), "rb") as wf:
        channels, width, rate, frames = (
            wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
        )
        raw = wf.readframes(frames)
    if width == 2:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        samples = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported WAV sample width: {width}")
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples.astype(np.float32), rate


def decode_audio_bytes(data: bytes) -> Tuple[FloatAudio, int]:
    """Decode WAV natively; fall back to raw float32 PCM for legacy clients."""
    if data[:4] == b"RIFF":
        return decode_wav(data)
    if len(data) % 4 == 0 and len(data) > 0:
        arr = np.frombuffer(data, dtype=np.float32)
        if np.all(np.isfinite(arr)) and np.max(np.abs(arr)) <= 1.0:
            return arr.astype(np.float32), 44100
    raise ValueError("unrecognised audio payload (expected WAV or float32 PCM)")


def encode_mp3(wav_bytes: bytes) -> Optional[bytes]:
    """Best-effort MP3 via ffmpeg when it is on PATH; None otherwise."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    proc = subprocess.run(
        [ffmpeg, "-loglevel", "error", "-i", "pipe:0", "-f", "mp3", "-b:a", "128k", "pipe:1"],
        input=wav_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


# ---------------------------------------------------------------------------
# Telephony codecs (Twilio media streams carry 8 kHz mu-law)
# ---------------------------------------------------------------------------

def mulaw_decode(data: bytes) -> FloatAudio:
    u = np.frombuffer(data, dtype=np.uint8).astype(np.int16)
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = ((mantissa << 3) + 0x84) << exponent
    magnitude = magnitude - 0x84
    pcm = np.where(sign != 0, -magnitude, magnitude).astype(np.int16)
    return from_int16(pcm)


def mulaw_encode(audio: FloatAudio) -> bytes:
    pcm = to_int16(audio).astype(np.int32)
    bias, clip = 0x84, 32635
    sign = np.where(pcm < 0, 0x80, 0).astype(np.int32)
    mag = np.minimum(np.abs(pcm), clip) + bias
    exponent = np.floor(np.log2(np.maximum(mag, 1))).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    out = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return out.astype(np.uint8).tobytes()


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

def resample(audio: FloatAudio, src_rate: int, dst_rate: int) -> FloatAudio:
    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float32)
    n_out = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def change_speed(audio: FloatAudio, rate: float) -> FloatAudio:
    """Change playback speed by resampling (pitch follows; the engine applies
    speed at synthesis time so this only touches externally supplied audio)."""
    if rate <= 0:
        raise ValueError("rate must be positive")
    n_out = max(1, int(audio.size / rate))
    x_old = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def normalize(audio: FloatAudio, peak: float = 0.9) -> FloatAudio:
    m = float(np.max(np.abs(audio))) if audio.size else 0.0
    if m < 1e-9:
        return audio.astype(np.float32)
    return (audio * (peak / m)).astype(np.float32)


def noise_gate(audio: FloatAudio, threshold: float = 0.01) -> FloatAudio:
    return np.where(np.abs(audio) < threshold, 0.0, audio).astype(np.float32)


def compress_dynamic_range(audio: FloatAudio, threshold: float = 0.5, ratio: float = 3.0) -> FloatAudio:
    mag = np.abs(audio)
    over = mag > threshold
    out = np.where(over, threshold + (mag - threshold) / ratio, mag)
    return (out * np.sign(audio)).astype(np.float32)


def _one_pole_lowpass(audio: FloatAudio, cutoff_hz: float, sample_rate: int) -> FloatAudio:
    if audio.size == 0:
        return audio
    dt = 1.0 / sample_rate
    rc = 1.0 / (2 * np.pi * cutoff_hz)
    alpha = dt / (rc + dt)
    out = np.empty_like(audio, dtype=np.float32)
    acc = 0.0
    for i, s in enumerate(audio):
        acc += alpha * (s - acc)
        out[i] = acc
    return out


def _fft_shelf(audio: FloatAudio, sample_rate: int, low_gain: float, mid_gain: float, high_gain: float) -> FloatAudio:
    """Three-band EQ in the frequency domain (fast, vectorised)."""
    if audio.size == 0:
        return audio
    spectrum = np.fft.rfft(audio)
    freqs = np.fft.rfftfreq(audio.size, d=1.0 / sample_rate)
    gains = np.ones_like(freqs)
    gains[freqs < 250] = low_gain
    gains[(freqs >= 250) & (freqs < 4000)] = mid_gain
    gains[freqs >= 4000] = high_gain
    return np.fft.irfft(spectrum * gains, n=audio.size).astype(np.float32)


def apply_eq(audio: FloatAudio, sample_rate: int, low: float = 1.0, mid: float = 1.15, high: float = 0.9) -> FloatAudio:
    return _fft_shelf(audio, sample_rate, low, mid, high)


def remove_noise(audio: FloatAudio, sample_rate: int, noise_floor: float = 0.01, cutoff_hz: float = 8000.0) -> FloatAudio:
    """Spectral-gate style clean-up: gate the floor, roll off above ``cutoff_hz``."""
    gated = noise_gate(audio, noise_floor)
    spectrum = np.fft.rfft(gated) if gated.size else gated
    if gated.size:
        freqs = np.fft.rfftfreq(gated.size, d=1.0 / sample_rate)
        spectrum = np.where(freqs > cutoff_hz, spectrum * 0.1, spectrum)
        gated = np.fft.irfft(spectrum, n=gated.size).astype(np.float32)
    return gated


def fade(audio: FloatAudio, sample_rate: int, fade_in_s: float = 0.01, fade_out_s: float = 0.03) -> FloatAudio:
    out = audio.astype(np.float32).copy()
    n_in = min(out.size, int(fade_in_s * sample_rate))
    n_out = min(out.size, int(fade_out_s * sample_rate))
    if n_in > 0:
        out[:n_in] *= np.linspace(0.0, 1.0, n_in, dtype=np.float32)
    if n_out > 0:
        out[-n_out:] *= np.linspace(1.0, 0.0, n_out, dtype=np.float32)
    return out


def mix(a: FloatAudio, b: FloatAudio, ratio: float = 0.5) -> FloatAudio:
    """Blend two signals; the shorter one is zero-padded."""
    n = max(a.size, b.size)
    pa = np.pad(a, (0, n - a.size))
    pb = np.pad(b, (0, n - b.size))
    return (pa * ratio + pb * (1.0 - ratio)).astype(np.float32)


def add_background(voice: FloatAudio, background: FloatAudio, volume: float = 0.1) -> FloatAudio:
    """Loop/trim ``background`` under ``voice`` at ``volume``."""
    if background.size == 0 or voice.size == 0:
        return voice.astype(np.float32)
    reps = int(np.ceil(voice.size / background.size))
    bed = np.tile(background, reps)[: voice.size]
    return np.clip(voice + bed * volume, -1.0, 1.0).astype(np.float32)


def silence(seconds: float, sample_rate: int) -> FloatAudio:
    return np.zeros(max(0, int(seconds * sample_rate)), dtype=np.float32)


def duration_seconds(audio: FloatAudio, sample_rate: int) -> float:
    return float(audio.size) / float(sample_rate) if sample_rate else 0.0


def rms(audio: FloatAudio) -> float:
    return float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0


def estimate_pitch_hz(audio: FloatAudio, sample_rate: int, fmin: float = 60.0, fmax: float = 500.0) -> float:
    """Autocorrelation pitch estimate over the loudest 100 ms window."""
    if audio.size < sample_rate // 10:
        return 0.0
    win = int(sample_rate * 0.1)
    energies = [rms(audio[i : i + win]) for i in range(0, audio.size - win, win)]
    if not energies:
        return 0.0
    start = int(np.argmax(energies)) * win
    frame = audio[start : start + win] - np.mean(audio[start : start + win])
    if rms(frame) < 1e-4:
        return 0.0
    corr = np.correlate(frame, frame, mode="full")[win - 1 :]
    lo, hi = int(sample_rate / fmax), int(sample_rate / fmin)
    if hi >= corr.size:
        hi = corr.size - 1
    if lo >= hi:
        return 0.0
    lag = lo + int(np.argmax(corr[lo:hi]))
    return float(sample_rate) / lag if lag else 0.0
