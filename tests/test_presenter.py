#!/usr/bin/env python3
"""Presenter-interstitial mock tests (VM only, run with the project venv).

Modes (env MOCK_MODE or argv):
  reel — news-mode Reel from 3 fake English stories, pool emptied, live
         visual APIs mocked to [] (title cards only). TTS is patched to
         silent audio (12s) so the narration split (first 4s over the
         interstitial, rest over the story) is exercised for real.
         Expected: 3x15s stories + 2x4s presenter interstitials + 15s CTA
         = 68.0s. Frame at t=17s must show the presenter.
  long — same, for the 16:9 long video (LONG_TOPIC=news, silent audio 28s).
         Expected: 3x30s + 2x4s + 30s = 128.0s. Frame at t=32s must show
         the presenter (landscape, blurred-pad backdrop).
  all  — runs reel + long.

Run: ./venv/bin/python tests/test_presenter.py [all|reel|long]
Frames are written to /tmp/pres_reel.png and /tmp/pres_long.png for a
visual check that the presenter actually appears in the interstitial.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import mock_pool
import build_reel as br

FAKE = [
    {"title": "Global Markets Rally as Gold Hits Record High",
     "desc": "Stock markets around the world rose today as investors welcomed strong economic data and gold reached a new record price.",
     "link": "https://example.com/markets", "ts": 9999999999},
    {"title": "New Electric Car Breaks Range Record",
     "desc": "A new electric car has broken the driving range record, travelling further on a single charge than any production car before it.",
     "link": "https://example.com/ev", "ts": 9999999998},
    {"title": "Scientists Discover New Deep Ocean Species",
     "desc": "Scientists exploring the deep ocean have discovered several species never seen before, including a fish that glows in the dark.",
     "link": "https://example.com/ocean", "ts": 9999999997},
]


def ffprobe_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=60)
    return float(r.stdout.strip())


def ffprobe_streams(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,width,height",
                        "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=60)
    return [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]


def grab_frame(video, seconds, out_png):
    subprocess.run(["ffmpeg", "-y", "-ss", str(seconds), "-i", video,
                    "-frames:v", "1", out_png],
                   capture_output=True, timeout=120, check=True)
    print(f"frame at {seconds}s -> {out_png}")


def empty_pool():
    backup = None
    if os.path.exists(br.POOL_INDEX):
        with open(br.POOL_INDEX, encoding="utf-8") as f:
            backup = f.read()
        os.remove(br.POOL_INDEX)
    return backup


def restore_pool(backup):
    if backup is not None:
        with open(br.POOL_INDEX, "w", encoding="utf-8") as f:
            f.write(backup)


def check_slices(prefix, n):
    for k in range(n):
        p = os.path.join(br.ASSETS, f"{prefix}_pres{k}.mp4")
        assert os.path.exists(p), f"missing presenter slice {p}"
        d = br.probe_duration(p)
        print(f"  slice {prefix}_pres{k}.mp4: {d:.2f}s")
        assert 3.5 <= d <= 4.5, f"slice {k} duration {d} != ~4s"


def run_reel():
    os.environ.pop("SMART_GLASSES_ONLY", None)
    br.fetch_stories = lambda feeds, limit=6: list(FAKE)
    mock_pool.patch_no_live_apis(br)
    # TTS fails on this VM (egress proxy); silent audio of the right length
    # exercises the atrim narration split for real.
    br.make_voice = lambda text, path, voice="en-US-AriaNeural": \
        br.silent_audio(path, 12)
    backup = empty_pool()
    try:
        br.main()
    finally:
        restore_pool(backup)
    out = os.path.join(br.OUT, "reel.mp4")
    d = ffprobe_dur(out)
    print(f"REEL: {out} duration {d:.2f}s (expected 68.00s)")
    print(f"  streams: {ffprobe_streams(out)}")
    assert abs(d - 68.0) < 1.0, f"reel duration {d} != 68s"
    check_slices("s", 2)
    grab_frame(out, 17, "/tmp/pres_reel.png")
    print("REEL MOCK DONE")


def run_long():
    import build_long_video as blv
    os.environ["LONG_TOPIC"] = "news"
    br.fetch_stories = lambda feeds, limit=6: list(FAKE)
    mock_pool.patch_no_live_apis(br)
    br.make_voice = lambda text, path, voice="en-US-AriaNeural": \
        br.silent_audio(path, 28)
    backup = empty_pool()
    try:
        blv.main()
    finally:
        restore_pool(backup)
    out = os.path.join(br.OUT, "long_video.mp4")
    d = ffprobe_dur(out)
    print(f"LONG: {out} duration {d:.2f}s (expected 128.00s)")
    print(f"  streams: {ffprobe_streams(out)}")
    assert abs(d - 128.0) < 1.0, f"long duration {d} != 128s"
    check_slices("l", 2)
    grab_frame(out, 32, "/tmp/pres_long.png")
    print("LONG MOCK DONE")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("all", "reel"):
        run_reel()
    if mode in ("all", "long"):
        run_long()
    print("PRESENTER MOCK TESTS PASSED")
