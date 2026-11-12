#!/usr/bin/env python3
"""Media-pool pre-fetch worker (FULLY ENGLISH channel).

Runs at the start of a workflow run (before the builders) and prepares the
pool in data/media_pool/ + data/media_pool/pool.json, so the builders pick
ready visuals/audio instead of calling the live APIs for every story:

VISUALS (per stock topic in build_reel.TOPICS — news topics + "smart
glasses augmented reality"):
  Pexels photos (3/topic) + Unsplash photos (2/topic) + Pixabay video
  clips (2 portrait + 2 landscape per topic) + Pexels video clips
  (2 portrait + 2 landscape per topic, dormant while Pexels pauses new
  keys). Free stock only, no RSS/agency visuals. Files already in the
  pool are skipped (top-up only); the pool is pruned to 6 photos /
  6 videos per topic.

AUDIO (only fixed texts can be pre-rendered):
  For every built-in smart-glasses topic (build_reel.SMART_TOPICS) the
  narration is rendered once per format — Reel (220 chars) and long video
  (400 chars) — with the English voice en-US-AriaNeural, plus the fixed end
  CTA. Each entry is keyed by script_hash = md5(voice + narration text):
  if a script changes, its hash changes and the audio is re-generated;
  stale hashes are deleted. NEWS narration can NOT be pre-generated (the
  stories are fresh every run) — news audio is still generated at build
  time by the builders.

KEYS: PIXABAY_API_KEY / PEXELS_API_KEY / UNSPLASH_ACCESS_KEY from env
(same secrets as the builders). Without a key the matching provider is
skipped and the pool is left partial — the script NEVER crashes and
always exits 0 (the builders fall back to live APIs / title cards).
TTS needs no key (edge-tts); if
edge-tts is unreachable (e.g. on this VM behind the egress proxy) a silent
mp3 of the right length is written instead — the real TTS runs on GitHub
runners, and the builders still pick the pooled file.

PERSISTENCE: GitHub Actions runners are ephemeral — the pool only survives
between runs via the Actions cache (see .github/workflows/build-reel.yml:
weekly cache key, restore-keys fallback). Pool files are NEVER committed
to the repo (too heavy: ~44 video clips).

Usage: python scripts/prefetch_media.py [--topics q1,q2] [--skip-visuals]
       [--skip-audio]
"""
import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_reel as br

POOL_DIR = br.POOL_DIR
POOL_INDEX = br.POOL_INDEX
VOICE = br.EN_VOICE

PEXELS_PHOTOS = 3      # Pexels photos per topic
UNSPLASH_PHOTOS = 2    # Unsplash photos per topic
PIXABAY_VIDEOS_PORTRAIT = 2    # Pixabay video clips (portrait) per topic
PIXABAY_VIDEOS_LANDSCAPE = 2   # Pixabay video clips (landscape) per topic
VIDEOS_PORTRAIT = 2    # Pexels video clips (portrait) per topic
VIDEOS_LANDSCAPE = 2   # Pexels video clips (landscape) per topic
MAX_PHOTOS_TOPIC = 6   # prune cap per topic
MAX_VIDEOS_TOPIC = 6   # prune cap per topic

# (format, max narration chars, silent-audio seconds) per template render
AUDIO_FORMATS = (("reel", 220, 12), ("long", 400, 28))
CTA_SECONDS = 5


def provider_id(url):
    """Stable provider ID from a stock URL (numeric Pexels id / Unsplash
    photo uid / Pexels video id); md5 of the URL as a fallback."""
    m = re.search(r"/photos/(\d+)/", url)
    if m:
        return m.group(1)
    m = re.search(r"/(photo-[^?/]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/video-files/(\d+)/", url)
    if m:
        return m.group(1)
    # Pixabay video files: /video/2023/10/22/186115-874643483_large.mp4
    m = re.search(r"/video/\d{4}/\d{2}/\d{2}/(\d+)-", url)
    if m:
        return m.group(1)
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def photo_orientation(path):
    """'landscape' / 'portrait' / 'square' from the image size; 'any' on failure."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        if w > h:
            return "landscape"
        if h > w:
            return "portrait"
        return "square"
    except Exception:
        return "any"


def load_index():
    try:
        with open(POOL_INDEX, encoding="utf-8") as f:
            entries = json.load(f)
        return [e for e in entries if isinstance(e, dict)]
    except Exception:
        return []


def save_index(entries):
    os.makedirs(POOL_DIR, exist_ok=True)
    tmp = POOL_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
    os.replace(tmp, POOL_INDEX)


def rel_path(name):
    return "data/media_pool/" + name


def have(entries, provider, pid):
    """True if an entry with this provider+id exists and its file is present."""
    for e in entries:
        if e.get("provider") == provider and e.get("provider_id") == pid:
            if os.path.exists(os.path.join(br.BASE, e.get("path", ""))):
                return True
    return False


def add_photo(entries, url, provider, topic):
    pid = provider_id(url)
    if have(entries, provider, pid):
        return False
    ext = ".jpg"
    name = f"{provider}_photo_{pid}{ext}"
    path = os.path.join(POOL_DIR, name)
    if not br.download(url, path, min_bytes=5000):
        return False
    entries.append({"topic": topic, "kind": "photo",
                    "orientation": photo_orientation(path),
                    "path": rel_path(name), "url": url,
                    "provider": provider, "provider_id": pid})
    print(f"  + photo {provider} {pid} ({topic})")
    return True


def add_video(entries, url, provider, topic, orientation):
    pid = provider_id(url)
    if have(entries, provider, pid + "_" + orientation):
        return False
    name = f"{provider}_video_{pid}_{orientation}.mp4"
    path = os.path.join(POOL_DIR, name)
    if not br.download(url, path, min_bytes=100_000):
        return False
    if br.probe_duration(path) <= 0:
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    entries.append({"topic": topic, "kind": "video",
                    "orientation": orientation,
                    "path": rel_path(name), "url": url,
                    "provider": provider, "provider_id": pid + "_" + orientation})
    print(f"  + video {provider} {pid} {orientation} ({topic})")
    return True


def prefetch_visuals(entries, queries):
    """Top up visuals per topic. Never raises (each provider is guarded)."""
    added = {"photo": 0, "video": 0}
    for topic in queries:
        try:
            have_pex = sum(1 for e in entries
                           if e.get("topic") == topic and e.get("kind") == "photo"
                           and e.get("provider") == "pexels")
            for u in br.pexels_images(topic, n=8):
                if have_pex >= PEXELS_PHOTOS:
                    break
                if add_photo(entries, u, "pexels", topic):
                    have_pex += 1
                    added["photo"] += 1
        except Exception as ex:
            print(f"  pexels photos failed ({topic}): {ex}", file=sys.stderr)
        try:
            have_un = sum(1 for e in entries
                          if e.get("topic") == topic and e.get("kind") == "photo"
                          and e.get("provider") == "unsplash")
            for u in br.unsplash_images(topic, n=8):
                if have_un >= UNSPLASH_PHOTOS:
                    break
                if add_photo(entries, u, "unsplash", topic):
                    have_un += 1
                    added["photo"] += 1
        except Exception as ex:
            print(f"  unsplash photos failed ({topic}): {ex}", file=sys.stderr)
        # Pixabay video clips FIRST (Pexels new keys are paused, so
        # Pixabay carries the video slots; per-provider targets, like
        # the photos above).
        for orientation, target in (("portrait", PIXABAY_VIDEOS_PORTRAIT),
                                    ("landscape", PIXABAY_VIDEOS_LANDSCAPE)):
            try:
                have_vid = sum(1 for e in entries
                               if e.get("topic") == topic and e.get("kind") == "video"
                               and e.get("orientation") == orientation
                               and e.get("provider") == "pixabay")
                for u in br.pixabay_videos(topic, n=6, orientation=orientation):
                    if have_vid >= target:
                        break
                    if add_video(entries, u, "pixabay", topic, orientation):
                        have_vid += 1
                        added["video"] += 1
            except Exception as ex:
                print(f"  pixabay videos failed ({topic} {orientation}): {ex}",
                      file=sys.stderr)
        for orientation, target in (("portrait", VIDEOS_PORTRAIT),
                                    ("landscape", VIDEOS_LANDSCAPE)):
            try:
                have_vid = sum(1 for e in entries
                               if e.get("topic") == topic and e.get("kind") == "video"
                               and e.get("orientation") == orientation
                               and e.get("provider") == "pexels")
                for u in br.pexels_videos(topic, n=6, orientation=orientation):
                    if have_vid >= target:
                        break
                    if add_video(entries, u, "pexels", topic, orientation):
                        have_vid += 1
                        added["video"] += 1
            except Exception as ex:
                print(f"  pexels videos failed ({topic} {orientation}): {ex}",
                      file=sys.stderr)
    return added


def render_audio(entries, topic_title, narr, voice, fmt, seconds):
    """Render one narration text into the pool (skip if its hash is already
    pooled with a valid file). Returns True if the entry exists afterwards."""
    h = br.narration_hash(narr, voice)
    for e in entries:
        if (e.get("kind") == "audio" and e.get("script_hash") == h
                and os.path.exists(os.path.join(br.BASE, e.get("path", "")))
                and os.path.getsize(os.path.join(br.BASE, e["path"])) > 1000):
            return True
    name = f"audio_{h}.mp3"
    path = os.path.join(POOL_DIR, name)
    ok = br.make_voice(narr, path, voice=voice)
    if not ok:
        # edge-tts unreachable (e.g. this VM behind the egress proxy):
        # silent mp3 of the right length, so the builders still pick it.
        ok = br.silent_audio(path, seconds)
        if ok:
            print(f"  ~ audio {fmt} {topic_title}: TTS unreachable, silent fallback")
    if not ok or br.probe_duration(path) <= 0:
        return False
    entries.append({"topic": topic_title, "kind": "audio", "format": fmt,
                    "script_hash": h, "path": rel_path(name), "voice": voice})
    print(f"  + audio {fmt} {topic_title} (hash {h})")
    return True


def prefetch_audio(entries):
    """Pre-render narration for every built-in smart-glasses topic (both
    formats) + the fixed end CTA. Returns (rendered, skipped-already)."""
    rendered = skipped = 0
    for t in br.SMART_TOPICS:
        st = br.template_story(t)
        for fmt, max_chars, seconds in AUDIO_FORMATS:
            narr = br.template_narration(st, max_chars)
            h = br.narration_hash(narr, VOICE)
            if any(e.get("kind") == "audio" and e.get("script_hash") == h
                   for e in entries):
                skipped += 1
                continue
            if render_audio(entries, t["title"], narr, VOICE, fmt, seconds):
                rendered += 1
    # End CTA (same fixed text in the Reel and the long video).
    h = br.narration_hash(br.CTA_NARR, VOICE)
    if any(e.get("kind") == "audio" and e.get("script_hash") == h
           for e in entries):
        skipped += 1
    elif render_audio(entries, "cta", br.CTA_NARR, VOICE, "cta", CTA_SECONDS):
        rendered += 1
    return rendered, skipped


def prune(entries):
    """Drop stale audio (hash no longer matches any current script) and
    excess visuals (keep the newest per topic); delete their files."""
    current_hashes = {br.narration_hash(br.template_narration(br.template_story(t), mc), VOICE)
                      for t in br.SMART_TOPICS for _, mc, _ in AUDIO_FORMATS}
    current_hashes.add(br.narration_hash(br.CTA_NARR, VOICE))
    keep, dropped = [], 0
    for e in entries:
        if e.get("kind") == "audio" and e.get("script_hash") not in current_hashes:
            dropped += 1
            _delete_entry_file(e)
            continue
        keep.append(e)
    # visuals: per (topic, kind) keep the newest MAX_* (by file mtime)
    by_key = {}
    for e in keep:
        if e.get("kind") in ("photo", "video"):
            by_key.setdefault((e.get("topic"), e.get("kind")), []).append(e)
    caps = {"photo": MAX_PHOTOS_TOPIC, "video": MAX_VIDEOS_TOPIC}
    keep_ids = {id(e) for e in keep if e.get("kind") not in ("photo", "video")}
    for (topic, kind), items in by_key.items():
        items.sort(key=lambda e: os.path.getmtime(
            os.path.join(br.BASE, e.get("path", ""))), reverse=True)
        for e in items[:caps[kind]]:
            keep_ids.add(id(e))
        for e in items[caps[kind]:]:
            dropped += 1
            _delete_entry_file(e)
    if dropped:
        print(f"  pruned {dropped} stale/excess pool file(s)")
    return [e for e in keep if id(e) in keep_ids]


def _delete_entry_file(e):
    try:
        os.remove(os.path.join(br.BASE, e.get("path", "")))
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="Pre-fetch the media pool.")
    ap.add_argument("--topics", default="",
                    help="comma-separated stock topics to fetch (default: all TOPICS)")
    ap.add_argument("--skip-visuals", action="store_true")
    ap.add_argument("--skip-audio", action="store_true")
    args = ap.parse_args()

    os.makedirs(POOL_DIR, exist_ok=True)
    entries = load_index()
    # drop index rows whose file vanished (e.g. cache restored partially)
    entries = [e for e in entries
               if os.path.exists(os.path.join(br.BASE, e.get("path", "")))]
    print(f"Pool before: {len(entries)} entr(ies)")

    if args.topics:
        queries = [q.strip() for q in args.topics.split(",") if q.strip()]
    else:
        queries = []
        for _, q in br.TOPICS:
            if q not in queries:
                queries.append(q)
    if not args.skip_visuals:
        try:
            added = prefetch_visuals(entries, queries)
            print(f"Visuals added: {added}")
        except Exception as ex:
            print(f"prefetch visuals failed: {ex}", file=sys.stderr)
    if not args.skip_audio:
        try:
            rendered, skipped = prefetch_audio(entries)
            print(f"Audio rendered: {rendered}, already pooled: {skipped}")
        except Exception as ex:
            print(f"prefetch audio failed: {ex}", file=sys.stderr)
    try:
        entries = prune(entries)
    except Exception as ex:
        print(f"prune failed: {ex}", file=sys.stderr)
    save_index(entries)
    kinds = {}
    for e in entries:
        kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
    print(f"Pool after: {len(entries)} entr(ies) {kinds}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
