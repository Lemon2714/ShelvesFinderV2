"""
Generate voiceover narration (one WAV per guided-tour step) via OpenAI TTS,
and record each clip's duration to timings.json for video sync.

Run:  python media/gen_audio.py
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import audio_util  # noqa: E402

load_dotenv(ROOT.parent / "backend" / ".env")

# One narration line per tour step, in the SAME order as the tour in app.js.
NARRATION = [
    # 0 - intro
    "Welcome to Shelves Finder, the fastest way to see where your Walmart product shows up across category shelves, and where it is missing. Let's take a quick tour of every feature.",
    # 1 - url input
    "Start by pasting any Walmart product URL here. This is the product we'll analyze for shelf discoverability and visibility.",
    # 2 - modes
    "Choose a mode. Basic runs a fixed five step pipeline, fast and predictable. Advance runs an agentic AI loop that keeps searching and digging until it hits your targets.",
    # 3 - v1 auto toggle
    "In Basic mode, the Auto toggle runs every step end to end. Turn it off to step through each stage manually and inspect the output.",
    # 4 - analyze
    "When you're ready, click Analyze. Results stream in live as the agent works.",
    # 5 - advance mode
    "Switch to Advance mode to unlock agent settings and deeper visibility analytics. Let's open it up.",
    # 6 - settings toggle
    "The Settings button expands the agent's configuration. Here's what each option does.",
    # 7 - provider
    "Pick the AI model that powers the agent, either Claude or OpenAI.",
    # 8 - max rounds
    "Max rounds caps how many search, evaluate, and check cycles the agent can run.",
    # 9 - target missing
    "Target missing tells the agent to keep working until it finds this many shelves where your product is absent.",
    # 10 - budget
    "Budget sets a hard spending cap. The agent stops once it is reached.",
    # 11 - include branded shelves
    "Enable Include Branded Shelves to add shelves discovered using searches that contain your brand name.",
    # 12 - agent context
    "Add optional context to steer the agent. For example, focus on pharmacy and wellness aisles for a supplement.",
    # 13 - live processing
    "While running, this panel tracks the agent's progress in real time, including the current round and running cost.",
    # 14 - reasoning log
    "Every decision, which tool the agent chose and why, streams into the reasoning log, so the whole process is transparent.",
    # 15 - live stats
    "These counters give you an at a glance view: pages found, pages checked, missing shelves, and keywords tried.",
    # 16 - product summary
    "When the analysis completes, results open with the product title, image, and price, so you know exactly what was analyzed.",
    # 17 - discoverability dashboard
    "The Discoverability Dashboard shows how many shelves your product is found on versus missing from, with an overall score and risk level.",
    # 18 - extracted search intent
    "Extracted Search Intent lists the keywords shoppers would use to find this product. Each one links to a live Walmart search.",
    # 19 - recommended category pages
    "Recommended Category Pages lists every discovered shelf. The Status column shows B, whether your brand is carried there, and P, whether your product was found.",
    # 20 - copy urls
    "Need the list elsewhere? Copy URLs puts every category page link on your clipboard in one click.",
    # 21 - visibility dashboard
    "For shelves where your product is found, the Visibility Dashboard scores placement depth: prime page one spots versus buried on page three or beyond.",
    # 22 - shelf placement detail
    "Shelf Placement Detail breaks it down per shelf, whether your product appears as Sponsored, Organic, both, or isn't found in that keyword's search results.",
    # 23 - email
    "Share the findings instantly by emailing the full report to your team.",
    # 24 - outro
    "That's the full tour. Paste a real Walmart URL, hit Analyze, and see where your product stands. You can replay this walkthrough anytime from the How it works button.",
]

VOICE = "nova"
MODEL = "tts-1"


def _wav_duration(path: Path) -> float:
    # OpenAI TTS WAV headers are unreliable; derive duration from real PCM size.
    return audio_util.duration(path)


def _is_valid_wav(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 1000 and _wav_duration(path) > 0.3
    except Exception:
        return False


def main() -> None:
    # Per-call timeout + retries so a single stalled request can't hang the run.
    client = OpenAI(timeout=45.0, max_retries=3)
    outdir = ROOT / "narration"
    outdir.mkdir(exist_ok=True)

    durations = []
    for i, text in enumerate(NARRATION):
        path = outdir / f"seg_{i:02d}.wav"

        # Resume: reuse a previously generated, valid clip.
        if _is_valid_wav(path):
            dur = _wav_duration(path)
            durations.append(round(dur, 3))
            print(f"  seg {i:02d}: {dur:5.2f}s  (cached)", flush=True)
            continue

        last_err = None
        for attempt in range(1, 4):
            try:
                resp = client.audio.speech.create(
                    model=MODEL, voice=VOICE, input=text, response_format="wav"
                )
                resp.write_to_file(str(path))
                if not _is_valid_wav(path):
                    raise RuntimeError("produced invalid/short wav")
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"  seg {i:02d}: attempt {attempt} failed: {e}", flush=True)
        else:
            raise RuntimeError(f"seg {i:02d} failed after retries: {last_err}")

        dur = _wav_duration(path)
        durations.append(round(dur, 3))
        print(f"  seg {i:02d}: {dur:5.2f}s  {text[:50]}...", flush=True)

    json.dump(
        {"durations": durations, "count": len(durations)},
        open(ROOT / "timings.json", "w"),
        indent=2,
    )
    print(f"\nGenerated {len(durations)} clips | total {sum(durations):.1f}s", flush=True)


if __name__ == "__main__":
    main()
