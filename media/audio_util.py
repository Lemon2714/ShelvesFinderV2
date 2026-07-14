"""
Robust WAV helpers. OpenAI TTS returns WAV with an unreliable header frame
count, so we derive frames from the real file size instead of trusting it.
"""
import contextlib
import os
import wave
from pathlib import Path
from typing import Tuple

_WAV_HEADER = 44  # canonical PCM WAV header size


def read_pcm(path: str | Path) -> Tuple[int, int, int, bytes]:
    """Return (n_channels, sample_width, framerate, pcm_bytes)."""
    path = str(path)
    with contextlib.closing(wave.open(path, "rb")) as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        data_bytes = max(0, os.path.getsize(path) - _WAV_HEADER)
        frames = data_bytes // (nch * sw)
        pcm = w.readframes(frames)
    return nch, sw, fr, pcm


def duration(path: str | Path) -> float:
    nch, sw, fr, pcm = read_pcm(path)
    return len(pcm) / float(fr * nch * sw)
