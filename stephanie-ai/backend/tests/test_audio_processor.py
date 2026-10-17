import numpy as np

from models import audio_processor as ap


def _tone(freq=220.0, seconds=0.5, sr=16000):
    t = np.arange(int(seconds * sr)) / sr
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_wav_roundtrip():
    audio = _tone()
    data = ap.encode_wav(audio, 16000)
    decoded, rate = ap.decode_wav(data)
    assert rate == 16000
    assert decoded.shape == audio.shape
    assert np.max(np.abs(decoded - audio)) < 1e-3


def test_decode_audio_bytes_accepts_wav_and_float32():
    audio = _tone()
    decoded, _ = ap.decode_audio_bytes(ap.encode_wav(audio, 16000))
    assert decoded.size == audio.size
    raw, rate = ap.decode_audio_bytes(audio.tobytes())
    assert rate == 44100 and np.allclose(raw, audio)


def test_mulaw_roundtrip_is_close():
    audio = _tone(seconds=0.1, sr=8000)
    back = ap.mulaw_decode(ap.mulaw_encode(audio))
    assert back.shape == audio.shape
    assert np.max(np.abs(back - audio)) < 0.05


def test_resample_changes_length():
    audio = _tone(sr=16000)
    out = ap.resample(audio, 16000, 8000)
    assert out.size == audio.size // 2


def test_change_speed():
    audio = _tone()
    assert ap.change_speed(audio, 2.0).size == audio.size // 2
    assert ap.change_speed(audio, 0.5).size == audio.size * 2


def test_normalize_and_compress():
    audio = _tone() * 4
    norm = ap.normalize(audio, 0.9)
    assert abs(np.max(np.abs(norm)) - 0.9) < 1e-6
    comp = ap.compress_dynamic_range(norm, threshold=0.5, ratio=4.0)
    assert np.max(np.abs(comp)) < 0.9


def test_pitch_estimate():
    assert abs(ap.estimate_pitch_hz(_tone(220.0, 1.0), 16000) - 220.0) < 8.0
    assert abs(ap.estimate_pitch_hz(_tone(120.0, 1.0), 16000) - 120.0) < 8.0


def test_mix_and_background():
    a, b = _tone(220.0), _tone(330.0, seconds=0.25)
    mixed = ap.mix(a, b, 0.5)
    assert mixed.size == a.size
    bed = ap.add_background(a, b, 0.2)
    assert bed.size == a.size and np.max(np.abs(bed)) <= 1.0


def test_fade_edges_are_silent():
    faded = ap.fade(np.ones(1600, dtype=np.float32), 16000)
    assert faded[0] == 0.0 and faded[-1] == 0.0 and faded[800] == 1.0
