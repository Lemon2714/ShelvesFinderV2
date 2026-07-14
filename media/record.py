"""
Drive the in-app guided tour with Playwright and record it to webm.
Each tour step dwells for exactly its narration clip's duration so the
voiceover lines up. Writes video_meta.json for the assembly step.

Run:  python media/record.py
"""
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
APP_URL = "http://127.0.0.1:8000/"
VIDEO_DIR = ROOT / "video"
W, H = 1280, 800
PAD = 0.8     # gap between steps (covers tour transition); audio silent here
TAIL = 1.8    # linger on final step

NEXT_BTN = ".driver-popover-next-btn"
POPOVER = ".driver-popover"


def main() -> None:
    timings = json.load(open(ROOT / "timings.json"))
    durations = timings["durations"]
    n = len(durations)

    VIDEO_DIR.mkdir(exist_ok=True)
    for old in VIDEO_DIR.glob("*.webm"):
        old.unlink()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(VIDEO_DIR),
            record_video_size={"width": W, "height": H},
            device_scale_factor=1,
        )
        page = context.new_page()
        t0 = time.time()  # recording starts ~now

        page.goto(APP_URL, wait_until="networkidle")
        time.sleep(1.5)  # show the landing page

        page.click("#howItWorksBtn")
        page.wait_for_selector(POPOVER, state="visible", timeout=10000)
        time.sleep(0.6)  # let intro popover settle

        pre_roll = time.time() - t0
        print(f"pre_roll = {pre_roll:.2f}s | {n} steps", flush=True)

        for i in range(n):
            page.wait_for_selector(POPOVER, state="visible", timeout=10000)
            time.sleep(durations[i])  # narration plays
            if i < n - 1:
                btn = page.query_selector(NEXT_BTN)
                if not btn:
                    print(f"  [warn] next button missing at step {i}", flush=True)
                    break
                btn.click()
                time.sleep(PAD)
                print(f"  step {i:02d} done", flush=True)
            else:
                time.sleep(TAIL)
                print(f"  step {i:02d} (final) done", flush=True)

        video_path = page.video.path()
        context.close()
        browser.close()

    meta = {
        "video_path": str(video_path),
        "pre_roll": round(pre_roll, 3),
        "pad": PAD,
        "tail": TAIL,
        "durations": durations,
    }
    json.dump(meta, open(ROOT / "video_meta.json", "w"), indent=2)
    print(f"\nRecorded: {video_path}", flush=True)


if __name__ == "__main__":
    main()
