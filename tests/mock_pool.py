"""Shared mock-pool helpers for the media-pool tests (VM only).

Creates a few fake pool files (a portrait clip, a landscape clip, a photo)
for the topic "smart glasses augmented reality" and appends them to
data/media_pool/pool.json. teardown() restores the original pool.json and
deletes the mock files, so the real pool is untouched.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_reel as br

POOL = br.POOL_DIR
TOPIC = "smart glasses augmented reality"
MOCK_FILES = ["mock_portrait.mp4", "mock_landscape.mp4", "mock_photo.jpg"]


def setup_mock_pool():
    os.makedirs(POOL, exist_ok=True)
    backup = None
    if os.path.exists(br.POOL_INDEX):
        with open(br.POOL_INDEX, encoding="utf-8") as f:
            backup = f.read()
    # fake portrait clip (720x1280, 5s) + landscape clip (1280x720, 5s)
    for name, size in (("mock_portrait.mp4", "720x1280"),
                       ("mock_landscape.mp4", "1280x720")):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                        f"testsrc=size={size}:rate=30:duration=5",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        os.path.join(POOL, name)],
                       capture_output=True, timeout=120, check=True)
    # fake photo (1280x720 solid image)
    from PIL import Image
    Image.new("RGB", (1280, 720), (40, 90, 140)).save(
        os.path.join(POOL, "mock_photo.jpg"))
    entries = json.loads(backup) if backup else []
    entries += [
        {"topic": TOPIC, "kind": "video", "orientation": "portrait",
         "path": "data/media_pool/mock_portrait.mp4", "url": "mock://portrait",
         "provider": "mock", "provider_id": "mock-portrait"},
        {"topic": TOPIC, "kind": "video", "orientation": "landscape",
         "path": "data/media_pool/mock_landscape.mp4", "url": "mock://landscape",
         "provider": "mock", "provider_id": "mock-landscape"},
        {"topic": TOPIC, "kind": "photo", "orientation": "landscape",
         "path": "data/media_pool/mock_photo.jpg", "url": "mock://photo",
         "provider": "mock", "provider_id": "mock-photo"},
    ]
    with open(br.POOL_INDEX, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    print(f"Mock pool: {len(entries)} entries "
          f"({sum(1 for e in entries if e.get('provider') == 'mock')} mock)")
    return backup


def teardown_mock_pool(backup):
    for name in MOCK_FILES:
        try:
            os.remove(os.path.join(POOL, name))
        except OSError:
            pass
    if backup is None:
        try:
            os.remove(br.POOL_INDEX)
        except OSError:
            pass
    else:
        with open(br.POOL_INDEX, "w", encoding="utf-8") as f:
            f.write(backup)
    print("Mock pool removed, original pool.json restored")


def patch_no_live_apis(module):
    """Force the builder to use ONLY the pool (and title cards): every live
    visual API returns []. Feed fetching is patched by the caller."""
    module.pexels_videos = lambda *a, **k: []
    module.pexels_images = lambda *a, **k: []
    module.unsplash_images = lambda *a, **k: []
    module.openverse_images = lambda *a, **k: []
