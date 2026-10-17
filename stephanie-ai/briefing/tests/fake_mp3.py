"""Build a syntactically valid MPEG-1 Layer III CBR stream (silence frames) for tests."""


def fake_mp3(frames: int = 38, bitrate_index: int = 9, sr_index: int = 0) -> bytes:
    # 0xFFFB = sync + MPEG-1 + Layer III + no CRC; byte 3 = bitrate/samplerate/padding.
    header = bytes([0xFF, 0xFB, (bitrate_index << 4) | (sr_index << 2), 0x00])
    bitrate = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320][bitrate_index] * 1000
    sr = [44100, 48000, 32000][sr_index]
    frame_len = int(1152 / 8 * bitrate / sr)
    frame = header + bytes(frame_len - 4)
    return b"ID3" + bytes([4, 0, 0, 0, 0, 0, 10]) + bytes(10) + frame * frames
