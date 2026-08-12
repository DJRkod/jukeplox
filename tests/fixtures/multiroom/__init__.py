"""Hardware-free multi-room validation fixtures (2026-08-11 plan U10)."""

import math
import struct
import wave


def write_test_tone(path: str, *, seconds: float = 2.0, freq: float = 440.0,
                    sample_rate: int = 48000, channels: int = 2) -> str:
    """Write a short 16-bit PCM sine-tone WAV — a real, decodable source the
    server-fed backends can feed through ffmpeg without a Plex/library round-trip.
    Returns the path."""
    n = int(seconds * sample_rate)
    amp = 12000
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n):
            v = int(amp * math.sin(2 * math.pi * freq * (i / sample_rate)))
            frames += struct.pack("<h", v) * channels
        w.writeframes(bytes(frames))
    return path
