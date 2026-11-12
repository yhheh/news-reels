#!/usr/bin/env python3
"""Pixabay video support mock tests (VM only, run with the project venv).

No real PIXABAY_API_KEY is used anywhere: the key is never present in
the environment here, so every live Pixabay call is exercised only
through a mocked requests.get returning a Pixabay-shaped payload, or
through a monkeypatched pixabay_videos() returning [] (the no-key
simulation). The placeholder string below is an obviously fake test
key, not a real credential.

Checks:
  1. no-key skip: pixabay_videos() with no PIXABAY_API_KEY env -> [].
  2. parsing: with a fake key + mocked requests.get, a Pixabay-shaped
     response is parsed like pexels_videos() — best mp4 per hit,
     requested orientation preferred, non-mp4 / non-cdn.pixabay.com
     files skipped.
  3. ordering: gather_visuals() tries Pixabay BEFORE Pexels (call order
     recorded), and a Pixabay clip lands as a "video" visual.
  4. mock Reel build with pixabay_videos -> [] (no-key simulation):
     end-to-end fallback works, out/reel.mp4 exists, ~125s.
  5. mock long-video build with pixabay_videos -> []: out/long_video.mp4
     exists, ~230s.
  6. prefetch_visuals with no keys: adds nothing, raises nothing.

Cleanup: the out/ file set is snapshotted before the builds and any new
files are removed afterwards; the mock pool is torn down and pool.json
restored, so the real pool and out/ are untouched.

Run: ./venv/bin/python tests/test_pixabay_mock.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import mock_pool
import build_reel as br
import prefetch_media as pm

FAKE_KEY = "TEST-KEY-NOT-REAL"   # obviously fake placeholder, never a real key

# Pixabay-shaped payload: hit 111 landscape-only, hit 222 portrait,
# hit 333 with no usable file (wrong host + non-mp4).
FAKE_HITS = {"total": 3, "totalHits": 3, "hits": [
    {"id": 111, "tags": "city", "duration": 8, "videos": {
        "large": {"url": "https://cdn.pixabay.com/video/2024/01/01/111-aaa_large.mp4",
                  "width": 1920, "height": 1080, "size": 1000},
        "medium": {"url": "https://cdn.pixabay.com/video/2024/01/01/111-aaa_medium.mp4",
                   "width": 1280, "height": 720, "size": 500},
        "small": {"url": "https://cdn.pixabay.com/video/2024/01/01/111-aaa_small.mp4",
                  "width": 960, "height": 540, "size": 200},
        "tiny": {"url": "https://cdn.pixabay.com/video/2024/01/01/111-aaa_tiny.mp4",
                 "width": 640, "height": 360, "size": 100}}},
    {"id": 222, "tags": "city night", "duration": 6, "videos": {
        "large": {"url": "https://cdn.pixabay.com/video/2024/01/01/222-bbb_large.mp4",
                  "width": 1080, "height": 1920, "size": 1000},
        "medium": {"url": "https://cdn.pixabay.com/video/2024/01/01/222-bbb_medium.mp4",
                   "width": 720, "height": 1280, "size": 500}}},
    {"id": 333, "tags": "bad", "duration": 5, "videos": {
        "large": {"url": "https://evil.example.com/video/333.mp4",
                  "width": 1920, "height": 1080, "size": 100},
        "medium": {"url": "https://cdn.pixabay.com/video/2024/01/01/333-ccc_medium.webm",
                   "width": 1280, "height": 720, "size": 100}}},
]}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def check_no_key_skip():
    """1. No PIXABAY_API_KEY env -> [] (silent skip, never crashes)."""
    os.environ.pop("PIXABAY_API_KEY", None)
    got = br.pixabay_videos("city", n=6, orientation="portrait")
    assert got == [], f"no-key pixabay_videos must return [], got {got!r}"
    print("1. no-key skip: pixabay_videos() -> [] OK")


def check_parsing():
    """2. Fake Pixabay payload -> best mp4 per hit, orientation-aware."""
    os.environ["PIXABAY_API_KEY"] = FAKE_KEY
    real_get = br.requests.get
    seen = {}

    def fake_get(url, params=None, timeout=None):
        assert url == "https://pixabay.com/api/videos/", url
        assert params.get("key") == FAKE_KEY
        assert params.get("q") == "city"
        seen["orientation"] = params.get("orientation")
        return _FakeResp(FAKE_HITS)

    br.requests.get = fake_get
    try:
        # portrait: hit 111 landscape -> its 1080p file (closest to H);
        # hit 222 portrait -> its 720x1280 file (height == H); hit 333 skipped.
        got_p = br.pixabay_videos("city", n=6, orientation="portrait")
        assert seen["orientation"] == "vertical", seen
        assert got_p == [
            "https://cdn.pixabay.com/video/2024/01/01/111-aaa_large.mp4",
            "https://cdn.pixabay.com/video/2024/01/01/222-bbb_medium.mp4",
        ], got_p
        # landscape: hit 111 -> 1080p file; hit 222 -> 720x1280 file
        # (its height == H, so it still wins over the 1920-high file).
        got_l = br.pixabay_videos("city", n=6, orientation="landscape")
        assert seen["orientation"] == "horizontal", seen
        assert got_l == [
            "https://cdn.pixabay.com/video/2024/01/01/111-aaa_large.mp4",
            "https://cdn.pixabay.com/video/2024/01/01/222-bbb_medium.mp4",
        ], got_l
    finally:
        br.requests.get = real_get
        os.environ.pop("PIXABAY_API_KEY", None)
    print("2. parsing: portrait + landscape picks OK, bad hit skipped")


def check_ordering():
    """3. gather_visuals() tries Pixabay BEFORE Pexels; a Pixabay clip
    lands as a video visual."""
    calls = []
    tmp_clip = os.path.join(br.ASSETS, "_pb_test_clip.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=720x1280:rate=30:duration=5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", tmp_clip],
                   capture_output=True, timeout=120, check=True)
    orig_pb, orig_px = br.pixabay_videos, br.pexels_videos
    orig_dl = br.download
    orig_px_img, orig_un, orig_ov = (br.pexels_images, br.unsplash_images,
                                     br.openverse_images)
    try:
        br.pixabay_videos = lambda *a, **k: (calls.append("pixabay"), [
            "https://cdn.pixabay.com/video/2024/01/01/999-test_large.mp4"])[1]
        br.pexels_videos = lambda *a, **k: (calls.append("pexels"), [
            "https://videos.pexels.com/video-files/123/123-hd_1920_1080_25fps.mp4"])[1]

        def fake_download(url, path, **kw):
            import shutil
            shutil.copyfile(tmp_clip, path)
            return True

        br.download = fake_download
        br.pexels_images = lambda *a, **k: []
        br.unsplash_images = lambda *a, **k: []
        br.openverse_images = lambda *a, **k: []
        visuals = br.gather_visuals([], "smart glasses augmented reality",
                                    "t0", 3, "portrait", set(), None,
                                    "Test Title")
        assert calls[0] == "pixabay", f"Pixabay must be tried first, calls={calls}"
        assert calls[1] == "pexels", f"Pexels must follow Pixabay, calls={calls}"
        assert visuals[0][1] == "video", f"first visual must be a video: {visuals}"
        assert "_pbvid" in visuals[0][0], f"first visual must be the Pixabay clip: {visuals}"
        assert len(visuals) == 3, f"expected 3 visuals, got {len(visuals)}"
    finally:
        br.pixabay_videos, br.pexels_videos = orig_pb, orig_px
        br.download = orig_dl
        br.pexels_images, br.unsplash_images, br.openverse_images = (
            orig_px_img, orig_un, orig_ov)
        for p in (tmp_clip,
                  os.path.join(br.ASSETS, "t0_pbvid0.mp4"),
                  os.path.join(br.ASSETS, "t0_vid0.mp4")):
            try:
                os.remove(p)
            except OSError:
                pass
    print("3. ordering: Pixabay tried first, clip lands as video OK")


def check_prefetch_no_keys():
    """6. prefetch_visuals with no keys: adds nothing, raises nothing."""
    for k in ("PIXABAY_API_KEY", "PEXELS_API_KEY", "UNSPLASH_ACCESS_KEY"):
        os.environ.pop(k, None)
    entries = []
    added = pm.prefetch_visuals(entries, ["smart glasses augmented reality"])
    assert added == {"photo": 0, "video": 0}, added
    assert entries == [], entries
    print("6. prefetch no-keys: adds nothing, raises nothing OK")


def _snapshot_out():
    snap = set()
    for root, _dirs, files in os.walk(br.OUT):
        for f in files:
            snap.add(os.path.relpath(os.path.join(root, f), br.OUT))
    return snap


def _cleanup_out(snap):
    now = set()
    for root, _dirs, files in os.walk(br.OUT):
        for f in files:
            now.add(os.path.relpath(os.path.join(root, f), br.OUT))
    for rel in sorted(now - snap):
        try:
            os.remove(os.path.join(br.OUT, rel))
            print(f"  cleaned out/{rel}")
        except OSError:
            pass
    for root, dirs, files in os.walk(br.OUT, topdown=False):
        for d in dirs:
            p = os.path.join(root, d)
            try:
                if not os.listdir(p):
                    os.rmdir(p)
            except OSError:
                pass


def _ffprobe_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=60)
    return float(r.stdout.strip())


def check_mock_builds():
    """4+5. Mock Reel (~125s) + mock long video (~230s) with
    pixabay_videos -> [] (no-key simulation): the fallback chain works
    end-to-end."""
    snap = _snapshot_out()
    try:
        # --- mock Reel (smart-glasses, zero news, pool only) ---
        os.environ["SMART_GLASSES_ONLY"] = "1"
        backup = mock_pool.setup_mock_pool()
        mock_pool.patch_no_live_apis(br)
        br.pixabay_videos = lambda *a, **k: []  # no-key simulation
        br.fetch_stories = lambda feeds, limit=6: []
        try:
            br.main()
        finally:
            mock_pool.teardown_mock_pool(backup)
        reel = os.path.join(br.OUT, "reel.mp4")
        assert os.path.exists(reel) and os.path.getsize(reel) > 100_000, reel
        dur = _ffprobe_dur(reel)
        assert abs(dur - 125.0) < 2.0, f"Reel duration {dur}, expected ~125s"
        print(f"4. mock Reel: {os.path.getsize(reel)/1e6:.1f} MB, {dur:.2f}s OK")
        # --- mock long video (smart-glasses, zero news, pool only) ---
        import build_long_video as blv
        os.environ["LONG_TOPIC"] = "smart-glasses"
        backup = mock_pool.setup_mock_pool()
        mock_pool.patch_no_live_apis(br)
        br.pixabay_videos = lambda *a, **k: []  # no-key simulation
        br.fetch_stories = lambda feeds, limit=6: []
        try:
            blv.main()
        finally:
            mock_pool.teardown_mock_pool(backup)
        long_mp4 = os.path.join(br.OUT, "long_video.mp4")
        assert os.path.exists(long_mp4) and os.path.getsize(long_mp4) > 100_000
        dur = _ffprobe_dur(long_mp4)
        assert abs(dur - 230.0) < 2.0, f"long duration {dur}, expected ~230s"
        print(f"5. mock long video: {os.path.getsize(long_mp4)/1e6:.1f} MB, "
              f"{dur:.2f}s OK")
    finally:
        _cleanup_out(snap)


if __name__ == "__main__":
    check_no_key_skip()
    check_parsing()
    check_ordering()
    check_prefetch_no_keys()
    check_mock_builds()
    print("ALL PIXABAY MOCK TESTS PASSED")
