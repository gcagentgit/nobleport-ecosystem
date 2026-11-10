import numpy as np
import pytest

from models import audio_processor as ap
from models.voice_engine import EMOTION_PARAMS, StephanieVoiceEngine


def test_default_voices_registered(engine):
    ids = set(engine.voice_profiles)
    assert {"stephanie_primary", "stephanie_warm", "stephanie_professional", "stephanie_dispatch"} <= ids
    assert all(p.truth_label == "STAGED" for p in engine.voice_profiles.values())


def test_generate_speech_is_deterministic_and_bounded(engine):
    a = engine.generate_speech("Your estimate is ready for review.")
    b = engine.generate_speech("Your estimate is ready for review.")
    assert np.array_equal(a, b)
    assert a.dtype == np.float32
    assert np.max(np.abs(a)) <= 0.9
    assert 1.0 < ap.duration_seconds(a, engine.sample_rate) < 5.0


def test_speed_changes_duration(engine):
    slow = engine.generate_speech("Permit approved for twelve Main Street.", speed=0.5)
    fast = engine.generate_speech("Permit approved for twelve Main Street.", speed=2.0)
    assert slow.size > fast.size * 2.5


def test_emotion_changes_signal(engine):
    neutral = engine.generate_speech("Hello there.", emotion="neutral")
    excited = engine.generate_speech("Hello there.", emotion="excited")
    assert excited.size != neutral.size or not np.allclose(excited, neutral)


def test_pitch_tracks_profile(engine):
    a = engine.generate_speech("Hello hello hello hello.", voice_id="stephanie_professional")
    b = engine.generate_speech("Hello hello hello hello.", voice_id="stephanie_warm")
    pa, pb = ap.estimate_pitch_hz(a, engine.sample_rate), ap.estimate_pitch_hz(b, engine.sample_rate)
    assert pa > 0 and pb > 0
    assert pb > pa  # warm profile is pitched higher than professional


@pytest.mark.parametrize("emotion", sorted(EMOTION_PARAMS))
def test_all_emotions_synthesize(engine, emotion):
    audio = engine.generate_speech("Testing.", emotion=emotion)
    assert audio.size > 0 and np.all(np.isfinite(audio))


def test_invalid_inputs(engine):
    with pytest.raises(ValueError):
        engine.generate_speech("Hi", emotion="bored")
    with pytest.raises(ValueError):
        engine.generate_speech("Hi", speed=5.0)
    with pytest.raises(KeyError):
        engine.generate_speech("Hi", voice_id="nope")
    with pytest.raises(ValueError):
        engine.generate_speech("   ")


def test_clone_voice_persists_to_library(tmp_path):
    lib = tmp_path / "lib.json"
    engine = StephanieVoiceEngine(sample_rate=16000, library_path=str(lib))
    t = np.arange(16000 * 2) / 16000
    sample = (0.5 * np.sin(2 * np.pi * 120.0 * t)).astype(np.float32)
    profile = engine.clone_voice(sample, "Site Supervisor", 16000)
    assert profile.voice_id == "cloned_site_supervisor"
    assert profile.gender == "male"
    assert profile.cloned and lib.is_file()

    reloaded = StephanieVoiceEngine(sample_rate=16000, library_path=str(lib))
    assert "cloned_site_supervisor" in reloaded.voice_profiles
    audio = reloaded.generate_speech("Hello from the clone.", voice_id="cloned_site_supervisor")
    assert audio.size > 0
    assert reloaded.delete_voice("cloned_site_supervisor")
    assert not reloaded.delete_voice("stephanie_primary")


def test_clone_rejects_short_sample(engine):
    with pytest.raises(ValueError):
        engine.clone_voice(np.zeros(100, dtype=np.float32), "short", 16000)
