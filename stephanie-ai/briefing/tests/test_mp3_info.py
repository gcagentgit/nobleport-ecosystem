from briefing.mp3_info import mp3_info
from briefing.tests.fake_mp3 import fake_mp3


def test_duration_from_frames():
    info = mp3_info(fake_mp3(frames=38))          # 38 × 1152 / 44100 ≈ 0.99 s
    assert info and info.frames == 38
    assert abs(info.duration_s - 0.9926) < 0.01
    assert info.sample_rate == 44100
    assert abs(info.bitrate_kbps - 128) < 2


def test_not_mp3():
    assert mp3_info(b"RIFF....WAVE") is None
    assert mp3_info(b"") is None
