"""Duration/bitrate of an MP3 by walking MPEG frame headers (no decoder needed)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

_BITRATES = {  # kbps; index by (version_key, layer, bitrate_index)
    ("1", 3): [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    ("2", 3): [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_SAMPLE_RATES = {"1": [44100, 48000, 32000], "2": [22050, 24000, 16000], "2.5": [11025, 12000, 8000]}


@dataclass
class Mp3Info:
    frames: int
    duration_s: float
    sample_rate: int
    bitrate_kbps: float
    bytes: int

    def as_dict(self) -> dict:
        return {"frames": self.frames, "duration_s": round(self.duration_s, 3), "sample_rate": self.sample_rate,
                "bitrate_kbps": round(self.bitrate_kbps, 1), "bytes": self.bytes}


def _skip_id3(data: bytes) -> int:
    if data[:3] == b"ID3" and len(data) >= 10:
        size = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        return 10 + size
    return 0


def mp3_info(data: bytes) -> Optional[Mp3Info]:
    pos = _skip_id3(data)
    frames = 0
    samples = 0
    sample_rate = 0
    total_bits = 0.0
    n = len(data)
    while pos + 4 <= n:
        b1, b2, b3 = data[pos], data[pos + 1], data[pos + 2]
        if b1 != 0xFF or (b2 & 0xE0) != 0xE0:
            pos += 1
            continue
        version_bits = (b2 >> 3) & 0x03
        layer_bits = (b2 >> 1) & 0x03
        if version_bits == 1 or layer_bits == 0:
            pos += 1
            continue
        version = {0: "2.5", 2: "2", 3: "1"}[version_bits]
        layer = {1: 3, 2: 2, 3: 1}[layer_bits]
        if layer != 3:
            pos += 1
            continue
        br_index = (b3 >> 4) & 0x0F
        sr_index = (b3 >> 2) & 0x03
        padding = (b3 >> 1) & 0x01
        if br_index in (0, 15) or sr_index == 3:
            pos += 1
            continue
        key = ("1", 3) if version == "1" else ("2", 3)
        bitrate = _BITRATES[key][br_index] * 1000
        sr = _SAMPLE_RATES[version][sr_index]
        samples_per_frame = 1152 if version == "1" else 576
        frame_len = int(samples_per_frame / 8 * bitrate / sr) + padding
        if frame_len <= 0:
            pos += 1
            continue
        frames += 1
        samples += samples_per_frame
        sample_rate = sr
        total_bits += frame_len * 8
        pos += frame_len
    if not frames or not sample_rate:
        return None
    duration = samples / sample_rate
    return Mp3Info(frames=frames, duration_s=duration, sample_rate=sample_rate,
                   bitrate_kbps=(total_bits / duration / 1000) if duration else 0.0, bytes=n)
