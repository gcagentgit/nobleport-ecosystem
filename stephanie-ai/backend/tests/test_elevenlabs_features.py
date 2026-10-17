import numpy as np
import pytest

from models import audio_processor as ap
from services.elevenlabs_features import ElevenLabsFeatures


@pytest.fixture
def features(engine):
    return ElevenLabsFeatures(engine)


def test_multilingual(features):
    audio = features.multilingual_synthesis("Buenos días, su permiso está listo.", "es")
    assert audio.size > 0
    assert len(features.supported_languages()) == 10
    with pytest.raises(ValueError):
        features.multilingual_synthesis("hi", "xx")


def test_voice_settings_validation(features):
    settings = features.adjust_voice_settings(stability=0.3, similarity=0.9)
    assert settings.stability == 0.3 and settings.similarity_boost == 0.9 and settings.style == 0.0
    with pytest.raises(ValueError):
        features.adjust_voice_settings(style=1.5)


def test_streaming_yields_ordered_chunks(features):
    chunks = list(features.stream_voice("First sentence here. Second one follows. Third?", chunk_samples=2048))
    assert chunks, "expected streamed chunks"
    assert chunks[0]["segment"] == 0
    assert chunks[-1]["final"] is True
    assert chunks[-1]["segment"] == chunks[-1]["segments_total"] - 1
    assert all(len(c["pcm"]) <= 2048 * 2 for c in chunks)
    assert all(c["encoding"] == "pcm_s16le" for c in chunks)


def test_library_management_and_share_links(features):
    listing = features.manage_voice_library("list")
    assert any(v["voice_id"] == "stephanie_primary" for v in listing["voices"])
    share = features.manage_voice_library("share", "stephanie_primary")
    token = share["share_link"].split("share=")[1]
    assert features.verify_share_link("stephanie_primary", token)
    assert not features.verify_share_link("stephanie_warm", token)
    with pytest.raises(ValueError):
        features.manage_voice_library("delete", "stephanie_primary")
    with pytest.raises(KeyError):
        features.manage_voice_library("get", "missing")


def test_apply_emotion_post_process(features):
    t = np.arange(16000) / 16000
    audio = (0.4 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    sad = features.apply_emotion(audio, "sad", 1.0)
    assert sad.size > audio.size            # slower
    assert np.max(np.abs(sad)) < np.max(np.abs(audio))
    with pytest.raises(ValueError):
        features.apply_emotion(audio, "grumpy")


def test_enhance_audio_normalises(features):
    t = np.arange(16000) / 16000
    audio = (0.05 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    out = features.enhance_audio(audio, 16000)
    assert abs(np.max(np.abs(out)) - 0.9) < 1e-3


def test_pronunciation_registry(features):
    features.register_pronunciations({"AHJ": "authority having jurisdiction"})
    assert features.customize_pronunciation("Send it to the AHJ.", {}) == "Send it to the authority having jurisdiction."


def test_mix_and_background(features):
    mixed = features.mix_voice_profiles("Hello.", "stephanie_primary", "stephanie_warm", 0.3)
    assert mixed.size > 0
    voice = features.engine.generate_speech("Hello.")
    bed = features.ambient_bed("office", 0.5)
    out = features.add_background_audio(voice, bed, 0.2)
    assert out.size == voice.size
    with pytest.raises(ValueError):
        features.ambient_bed("space", 1.0)
    with pytest.raises(ValueError):
        features.add_background_audio(voice, bed, 2.0)
