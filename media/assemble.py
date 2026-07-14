"""
Build the synced narration track (pre-roll + clips + gaps) and mux it with the
recorded webm into a final MP4.

Run:  python media/assemble.py
"""
import contextlib
import json
import subprocess
import sys
import wave
from pathlib import Path

import imageio_ffmpeg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audio_util  # noqa: E402

ROOT = Path(__file__).resolve().parent
OUT_MP4 = ROOT / "shelves_finder_demo.mp4"
MASTER_WAV = ROOT / "narration_master.wav"


def main() -> None:
    meta = json.load(open(ROOT / "video_meta.json"))
    durations = meta["durations"]
    pre_roll = meta["pre_roll"]
    pad = meta["pad"]
    tail = meta["tail"]
    video_path = meta["video_path"]
    n = len(durations)

    # Read all clips; assume a consistent PCM format (from OpenAI TTS).
    clips = [audio_util.read_pcm(ROOT / "narration" / f"seg_{i:02d}.wav") for i in range(n)]
    nch, sw, fr, _ = clips[0]

    def silence(seconds: float) -> bytes:
        frames = int(round(seconds * fr))
        return b"\x00" * (frames * nch * sw)

    pcm = bytearray()
    pcm += silence(pre_roll)
    for i in range(n):
        pcm += clips[i][3]
        pcm += silence(tail if i == n - 1 else pad)

    with contextlib.closing(wave.open(str(MASTER_WAV), "wb")) as w:
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(fr)
        w.writeframes(bytes(pcm))

    audio_len = len(pcm) / float(fr * nch * sw)
    print(f"master audio: {audio_len:.1f}s | video: {video_path}", flush=True)

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg, "-y",
        "-i", video_path,
        "-i", str(MASTER_WAV),
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-map", "0:v:0", "-map", "1:a:0",
        str(OUT_MP4),
    ]
    print("running ffmpeg...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stderr[-3000:], flush=True)
        raise SystemExit(f"ffmpeg failed ({proc.returncode})")
    print(f"\nDONE -> {OUT_MP4}", flush=True)


if __name__ == "__main__":
    main()
