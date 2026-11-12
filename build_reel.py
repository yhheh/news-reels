#!/usr/bin/env python3
"""Auto news Reel builder (FULLY ENGLISH channel).
6 English stories, 3 free stock visuals each, English voice, vertical 9:16.
A brief 4s presenter interstitial (assets/presenter.mp4 — the presenter's
own video, a silent clip) appears between stories: the next story's
narration starts over the presenter, then the story visuals roll. Total
≈125s (6x15s stories + 5x4s interstitials + 15s end CTA) — still a Short
(< 3 min).
Free tools only. No RSS news-agency photos/videos (copyright) - free stock only.
Visual sources (in order): pooled clips/photos (data/media_pool/) ->
Pixabay Videos API (PIXABAY_API_KEY) -> Pexels Videos API
(PEXELS_API_KEY, dormant while Pexels pauses new keys) -> pooled photos
-> live photos (Pexels -> Unsplash -> Openverse, keyless) -> PIL title
card. Video clips are real moving footage; photos fill in when fewer
than 3 usable clips are found for a story.
Modes: default = general news (real English articles only).
ISLAMIC_ONLY=1 => Islamic Reel: real Islamic news first,
then built-in English template topics (rotating, generic/educational only)
fill the remaining slots — the mode never depends on the feeds.
Output: out/reel.mp4
Background music: "Serene View" by Arulo (Mixkit, royalty-free, no
attribution required — see assets/MUSIC.md) is mixed under the narration
at volume 0.15; if the file is missing the build still succeeds, voice only.

MEDIA POOL (data/media_pool/ + pool.json, filled by scripts/prefetch_media.py):
visuals AND narration audio are picked from the pool FIRST, then the live
APIs. Visuals: pooled clips/photos for the story's topic. Audio: only the
fixed Islamic template scripts (and the fixed end CTA) can be pooled —
their narration text is known in advance, so it is keyed by a hash of
(voice + narration text); a changed script changes the hash and the audio
is re-generated, never reused stale. NEWS narration is fresh every run
(new articles), so it is always generated at build time, never pooled.
On GitHub Actions the pool directory survives between runs via the Actions
cache (see .github/workflows/build-reel.yml); without a cache hit the
pre-fetch step rebuilds it. Pool files are never committed to the repo.
"""
import calendar
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import feedparser
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "out")
os.makedirs(OUT, exist_ok=True)
ASSETS = os.path.join(OUT, "assets")
os.makedirs(ASSETS, exist_ok=True)

W, H = 720, 1280            # vertical, light for phones
STORY_SEC = 15              # seconds per story
IMG_SEC = 5                 # seconds per image (3 images x 5s = 15s)
N_STORIES = 6
N_IMG = 3
FONT_URL = ("https://github.com/google/fonts/raw/main/ofl/notonaskharabic/"
            "NotoNaskhArabic%5Bwght%5D.ttf")
FONT_PATH = os.path.join(ASSETS, "NotoNaskhArabic.ttf")

# Media pool: pre-fetched visuals + pre-rendered template audio, filled by
# scripts/prefetch_media.py. Builders READ the pool; only the pre-fetch
# script writes it. pool.json entries: topic, kind ("photo"|"video"|"audio"),
# local path (relative to BASE), source URL, provider, provider ID; audio
# entries also carry script_hash + voice (see narration_hash).
POOL_DIR = os.path.join(BASE, "data", "media_pool")
POOL_INDEX = os.path.join(POOL_DIR, "pool.json")

# End-CTA narration (identical in the Reel and the long video, so one pooled
# audio file serves both). The on-screen CTA text is rendered separately.
CTA_NARR = "For more news, visit our website: Breaking News"
EN_VOICE = "en-US-AriaNeural"

# Background music (Mixkit, royalty-free — see assets/MUSIC.md). Mixed UNDER
# the narration at low volume; the voice stays dominant. The file is
# committed in assets/music.mp3; MUSIC_URL is the build-time fallback.
MUSIC_VOL = 0.15
MUSIC_PATH = os.path.join(BASE, "assets", "music.mp3")
MUSIC_URL = "https://assets.mixkit.co/music/443/443.mp3"

# Presenter interstitials (assets/presenter.mp4 — the presenter's own video,
# supplied by the user; fictional presenter in a studio, silent clip,
# 15.04s, 720x1280, 24fps). A BRIEF 3-5s slice appears between story
# segments — visual only. The news voice is the main audio throughout: the
# NEXT story's narration starts over the presenter, then the story visuals
# roll (the story segment skips those seconds — see a_start). There is NO
# separate presenter voice line. Slices rotate so each interstitial shows a
# different part of the clip. The clip has only ONE keyframe, so slices
# must be cut by re-encoding (-ss before -i); stream-copy slicing would
# always start at 0.
PRESENTER_PATH = os.path.join(BASE, "assets", "presenter.mp4")
PRESENTER_SEC = 4               # seconds per interstitial
PRESENTER_SLICES = [(0.0, 4.0), (3.0, 7.0), (6.0, 10.0), (9.0, 13.0),
                    (11.0, 15.0)]


def make_segment(visuals, title_png, audio, dur, a_start=0.0, a_end=None):
    """One render segment: visuals [(path, "video"|"image")], an optional
    title overlay PNG (None = no overlay), optional narration audio path
    (None = silent leg — the music bed still plays), duration in seconds,
    and the narration window [a_start, a_end) taken from the audio file
    (a_end None = to the end of the file). The audio leg is always padded
    to `dur`, so a short or missing narration never breaks the concat."""
    return {"visuals": visuals, "title": title_png, "audio": audio,
            "dur": dur, "a_start": a_start, "a_end": a_end}


def presenter_slice(k, prefix, target_w, target_h):
    """Cut rotating slice k from the presenter clip into
    out/assets/{prefix}_pres{k}.mp4 at target_w x target_h, 30fps.
    Portrait target (720x1280, the Reels): direct cut — the clip fits.
    Landscape target (1280x720, the long video): the vertical clip is
    scaled to fit the height and centred over a blurred, darkened copy of
    itself, so the presenter's face stays fully visible (a centre crop
    would cut his head and shoulders). Returns the path, or None if the
    presenter file is missing or ffmpeg fails (the caller then skips the
    interstitials and the stories play with no audio skip)."""
    if not (os.path.exists(PRESENTER_PATH)
            and os.path.getsize(PRESENTER_PATH) > 10000):
        return None
    start, end = PRESENTER_SLICES[k % len(PRESENTER_SLICES)]
    dur = end - start
    out = os.path.join(ASSETS,
                       f"{prefix}_pres{k % len(PRESENTER_SLICES)}.mp4")
    if os.path.exists(out) and probe_duration(out) >= dur - 0.1:
        return out
    common = ["-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0",
              "-crf", "28", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    if target_w > target_h:
        # landscape: blurred-pad bake (face fully visible, no crop)
        fc = (f"[0:v]fps=30,split[a][b];"
              f"[a]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
              f"crop={target_w}:{target_h},gblur=sigma=30,"
              f"eq=brightness=-0.25[bg];"
              f"[b]scale=-2:{target_h}[fg];"
              f"[bg][fg]overlay=(W-w)/2:0,setsar=1[v]")
        cmd = (["ffmpeg", "-y", "-ss", str(start), "-t", str(dur),
                "-an", "-i", PRESENTER_PATH,
                "-filter_complex", fc, "-map", "[v]"] + common + [out])
    else:
        cmd = (["ffmpeg", "-y", "-ss", str(start), "-t", str(dur),
                "-an", "-i", PRESENTER_PATH,
                "-vf", "fps=30"] + common + [out])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not os.path.exists(out):
        print(f"presenter slice {k} failed: {r.stderr[-500:]}",
              file=sys.stderr)
        return None
    print(f"  presenter slice {k}: {start:.0f}-{end:.0f}s -> "
          f"{os.path.basename(out)}")
    return out


def presenter_segment(k, prefix, target_w, target_h, audio):
    """A brief presenter interstitial segment (visual only): rotating slice
    k of the presenter clip, PRESENTER_SEC long, no title overlay. Its
    audio is the first PRESENTER_SEC of the NEXT story's narration
    (`audio` — the next story's voice starts over the presenter, then the
    story visuals roll); the following story segment must skip those
    seconds (a_start=PRESENTER_SEC). Returns None if the presenter clip
    is unavailable (the caller then uses no interstitials)."""
    path = presenter_slice(k, prefix, target_w, target_h)
    if path is None:
        return None
    return make_segment([(path, "video")], None, audio, PRESENTER_SEC,
                        a_start=0.0, a_end=PRESENTER_SEC)

# Islamic filter (shared by the Reels Islamic mode and the
# long-video builder): a story counts if its title+description contains
# any of these words (matched on word boundaries, case-insensitive).
ISLAM_WORDS = ["islam", "islamic", "muslim", "quran", "koran",
               "mosque", "ramadan", "eid", "prophet muhammad",
               "mecca", "hajj", "allah", "imam"]


def _contains_any(text, words):
    """True if text contains any word (word-boundary match, case-insensitive)."""
    low = text.lower()
    return any(re.search(r"\b" + re.escape(w.lower()) + r"\b", low)
               for w in words)


def is_islamic(story):
    """True if the story is about Islam / Muslims."""
    hay = (story.get("title", "") + " " + story.get("desc", ""))
    return _contains_any(hay, ISLAM_WORDS)


def _has_arabic(s):
    return any("\u0600" <= ch <= "\u06ff" or "\u0750" <= ch <= "\u077f"
               or "\ufb50" <= ch <= "\ufdff" or "\ufe70" <= ch <= "\ufeff"
               for ch in s)


def display_text(s):
    """Text ready for PIL drawing: Arabic is reshaped + bidi-ordered;
    Latin text (English titles, URLs) passes through unchanged."""
    if _has_arabic(s):
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(s))
    return s


def fit_text(text, max_chars):
    """Truncate text to max_chars at a word boundary (for narration)."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0]


# Built-in Islamic topics (ENGLISH). The Islamic mode does NOT depend on
# news feeds: each run picks topics from this list (rotating) and builds
# segments from them; real Islamic news stories, when found, are mixed in
# as extra segments. STRICT RULE: generic and educational only — never
# invent news events, prices, release dates, or quotes. Every sentence
# below is true in general terms. The topics introduce Islam — its history,
# its characteristics, and its role in society — to a general audience,
# one introductory topic per Islamic batch (rotating).
ISLAM_TOPICS = [
    {"title": "What Is Islam?",
     "script": "Islam is a monotheistic faith followed by nearly two billion people around the world. Muslims believe in one God, called Allah in Arabic, and that the Prophet Muhammad is the final messenger. The word Islam means submission to God, and the faith teaches peace, compassion, justice and care for others. Its holy book is the Quran, believed by Muslims to be the word of God revealed to the Prophet Muhammad."},
    {"title": "The History of Islam",
     "script": "Islam began in the seventh century, when the Prophet Muhammad received the revelation of the Quran in Mecca. From the Arabian Peninsula the faith spread across the Middle East, North Africa and Asia, carried by trade, scholarship and the example of Muslim communities. Islamic civilisation produced great advances in science, medicine, mathematics and art, and its scholars preserved and extended the knowledge of earlier civilisations. Today Islam is one of the world's largest religions, with followers in every country."},
    {"title": "The Five Pillars of Islam",
     "script": "The Five Pillars are the foundation of Muslim life. They are the declaration of faith, prayer five times a day, giving charity, fasting during the month of Ramadan, and the pilgrimage to Mecca for those who are able. These five practices shape how Muslims worship, give and live every day, and they are shared by Muslims in every country and culture."},
    {"title": "The Quran",
     "script": "The Quran is the holy book of Islam. Muslims believe it is the word of God, revealed to the Prophet Muhammad over twenty three years. It is written in Arabic and gives guidance on faith, prayer, family, charity and justice. Millions of Muslims memorise the whole book, and it is recited in prayer and read in homes and mosques around the world every day."},
    {"title": "Prophet Muhammad",
     "script": "The Prophet Muhammad is the final messenger in Islam. Muslims believe he received the revelation of the Quran and taught by example: honesty, kindness, patience and care for the poor. He was born in Mecca more than fourteen centuries ago, and his life is studied by Muslims as a model of character. Muslims mention him with respect and blessings whenever his name is said."},
    {"title": "Prayer in Islam",
     "script": "Prayer, called Salah, is one of the Five Pillars of Islam. Muslims pray five times a day: at dawn, midday, afternoon, sunset and night. The prayer includes standing, bowing and prostration, with recitation from the Quran. It is a direct connection with God, a pause from daily life, and a reminder of faith, humility and gratitude."},
    {"title": "Ramadan and Fasting",
     "script": "Ramadan is the holy month in which Muslims fast from dawn to sunset. Fasting means no food or drink during the day, and it teaches patience, self-control and empathy for people who are hungry. At sunset families gather to break the fast together. The month ends with the festival of Eid al-Fitr, a celebration of community, charity and gratitude."},
    {"title": "Charity in Islam",
     "script": "Giving to others is a central duty in Islam. Zakat is an annual giving of a small part of one's wealth to help the poor, and Sadaqah is voluntary charity given at any time. Muslims believe that wealth is a trust from God and that sharing it purifies the heart. Charity is not only money: a kind word, a helping hand and a smile are also described as charity."},
    {"title": "Islamic Values and Character",
     "script": "Islamic values guide how Muslims treat others. Honesty, kindness, patience, respect for parents and elders, care for neighbours and justice are taught in the Quran and in the example of the Prophet. Muslims are encouraged to speak truthfully, to keep promises, to forgive, and to help those in need, whatever their background. These values are shared by Muslims in every culture."},
    {"title": "Islam and Society",
     "script": "Islam gives guidance for life in society, not only for worship. It teaches justice, honesty in trade, care for neighbours, respect for parents and elders, and responsibility towards the poor and the weak. Muslims are encouraged to build strong families and communities, to resolve disputes fairly, and to contribute to the common good. The mosque is at the centre of this life, a place of prayer, learning and mutual support for the whole neighbourhood."},
    {"title": "Family in Islam",
     "script": "Family is at the heart of Islamic life. The Quran teaches kindness and respect towards parents, care for children, and responsibility within the family. Marriage is described as a source of love and tranquillity, and the rights of husbands, wives and children are set out clearly. Muslims are taught to keep family ties strong, to honour their elders, and to raise children with good character, faith and respect for others."},
    {"title": "What Muslims Believe",
     "script": "Muslims believe in one God, in angels, in the holy books, in the prophets, in the Day of Judgement and in divine decree. They believe Jesus is a respected prophet, and that the Prophet Muhammad is the final messenger. Faith in Islam is both belief and practice: prayer, charity, fasting and good character in daily life. Muslims see their faith as a complete way of life, guiding worship, family and society."},
]


def islam_rotation_seed():
    """Rotation offset for picking ISLAM_TOPICS: from env
    ISLAMIC_ROTATION_SEED (the workflow passes github.run_number) so every
    run covers different topics; falls back to the UTC day number."""
    raw = os.environ.get("ISLAMIC_ROTATION_SEED", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    import datetime
    return datetime.datetime.utcnow().toordinal()


def pick_islam_topics(n, seed=None):
    """Pick n topics from ISLAM_TOPICS, rotating by seed (wraps around)."""
    if seed is None:
        seed = islam_rotation_seed()
    start = seed % len(ISLAM_TOPICS)
    return [ISLAM_TOPICS[(start + k) % len(ISLAM_TOPICS)] for k in range(n)]


# Visual search topic for Islamic mode (stock photos/video clips of
# mosques and prayer). The robot (scripts/prefetch_media.py) collects
# visuals for this topic; the builders use it for Islamic segments.
ISLAM_VISUAL_TOPIC = "islam mosque"


def template_story(topic):
    """A built-in topic as a story-like dict (no news article behind it)."""
    return {"title": topic["title"], "desc": topic["script"], "link": "",
            "ts": 0, "template": True}


# English keyword -> English stock-photo/video topic (the channel is fully
# English now: English feeds, English titles, English voice).
TOPICS = [
    # Islam first: topic_for() returns the first match, and these stories
    # must get Islamic visuals (mosque/prayer photos and video clips),
    # not generic ones. Used by the Reels and by the long Islamic video.
    (["islam", "muslim", "quran", "mosque", "ramadan", "eid", "prophet", "mecca", "hajj"], "islam mosque"),
    (["football", "soccer", "match", "league", "goal", "player", "championship", "cup", "tournament"], "football stadium"),
    (["economy", "dollar", "gold", "stock market", "shares", "oil", "inflation", "interest rate", "economy"], "economy money"),
    (["weather", "rain", "storm", "snow", "heatwave", "temperature", "flood"], "weather rain"),
    (["health", "disease", "hospital", "medicine", "vaccine", "doctor", "flu"], "health hospital"),
    (["technology", "artificial intelligence", "phone", "internet", "robot", "chip", "software"], "technology computer"),
    (["education", "school", "university", "students", "teacher", "exam"], "education school"),
    (["war", "army", "military", "strike", "attack", "missile", "soldier"], "military conflict"),
    (["election", "president", "government", "parliament", "minister", "vote", "senate"], "politics government"),
    (["music", "concert", "cinema", "film", "actor", "artist", "song"], "concert music"),
    (["travel", "tourism", "flight", "airport", "hotel", "tourist", "visa"], "travel airplane"),
]


def topic_for(title):
    for words, topic in TOPICS:
        if _contains_any(title, words):
            return topic
    return "news city"


def fetch_stories(feeds, limit=N_STORIES):
    stories, seen = [], set()
    for url in feeds:
        try:
            d = feedparser.parse(url)
            for e in d.entries[:5]:
                title = (e.get("title") or "").strip()
                link = (e.get("link") or "").strip()
                if not title or not link or link in seen:
                    continue
                seen.add(link)
                desc = re.sub(r"<[^>]+>", " ", e.get("summary") or e.get("description") or "")
                desc = re.sub(r"\s+", " ", desc).strip()
                pub = e.get("published_parsed") or e.get("updated_parsed")
                ts = calendar.timegm(pub) if pub else 0
                stories.append({"title": title, "desc": desc, "link": link, "ts": ts})
        except Exception as ex:
            print(f"feed failed {url}: {ex}", file=sys.stderr)
    stories.sort(key=lambda s: s["ts"], reverse=True)
    return stories[:limit]


# Only these Openverse providers: their CDNs allow server downloads.
# Flickr (403) and Wikimedia (429) are blocked/rate-limited from GitHub runners.
GOOD_HOSTS = ("images.pexels.com", "images.unsplash.com", "cdn.pixabay.com")


def openverse_images(query, n=8):
    """Free stock photos from Openverse (CC-licensed), only from reliable CDNs.
    Returns list of image URLs."""
    try:
        r = requests.get("https://api.openverse.org/v1/images/",
                         params={"q": query, "page_size": 20,
                                 "license_type": "commercial"},
                         timeout=20)
        r.raise_for_status()
        out = []
        for it in r.json().get("results", []):
            u = it.get("foreign_landing_url") and it.get("url")
            if not u or not u.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            host = u.split("/")[2].lower() if "://" in u else ""
            if host not in GOOD_HOSTS:
                continue
            out.append(u)
            if len(out) >= n:
                break
        return out
    except Exception as ex:
        print(f"openverse failed ({query}): {ex}", file=sys.stderr)
        return []


def pexels_images(query, n=8, orientation=None):
    """Free stock photos from the Pexels API (direct, reliable).
    Key from env PEXELS_API_KEY; no key => [] (caller falls back).
    orientation: None (any, default — the Reels) or "landscape" (long video).
    Returns list of image URLs (images.pexels.com only)."""
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    params = {"query": query, "per_page": 15}
    if orientation:
        params["orientation"] = orientation
    try:
        r = requests.get("https://api.pexels.com/v1/search",
                         params=params,
                         headers={"Authorization": key},
                         timeout=20)
        r.raise_for_status()
        out = []
        for ph in r.json().get("photos", []):
            src = (ph.get("src") or {})
            u = src.get("large") or src.get("medium") or src.get("original")
            if not u:
                continue
            host = u.split("/")[2].lower() if "://" in u else ""
            if host != "images.pexels.com":
                continue
            out.append(u)
            if len(out) >= n:
                break
        return out
    except Exception as ex:
        print(f"pexels failed ({query}): {ex}", file=sys.stderr)
        return []


def unsplash_images(query, n=8):
    """Free stock photos from the Unsplash API (direct).
    Key from env UNSPLASH_ACCESS_KEY; no key => [] (caller falls back).
    Returns list of image URLs (images.unsplash.com only)."""
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    if not key:
        return []
    try:
        r = requests.get("https://api.unsplash.com/search/photos",
                         params={"query": query, "per_page": 15},
                         headers={"Authorization": f"Client-ID {key}"},
                         timeout=20)
        r.raise_for_status()
        out = []
        for ph in r.json().get("results", []):
            urls = (ph.get("urls") or {})
            u = urls.get("regular") or urls.get("small") or urls.get("raw")
            if not u:
                continue
            host = u.split("/")[2].lower() if "://" in u else ""
            if host != "images.unsplash.com":
                continue
            out.append(u)
            if len(out) >= n:
                break
        return out
    except Exception as ex:
        print(f"unsplash failed ({query}): {ex}", file=sys.stderr)
        return []


def pexels_videos(query, n=6, orientation="portrait"):
    """Free stock VIDEO clips from the Pexels Videos API (direct, reliable).
    Same key as photos (env PEXELS_API_KEY); no key => [] (caller falls back).
    orientation: "portrait" (default — the vertical Reels) or "landscape"
    (the 16:9 long video). Returns list of direct mp4 file URLs
    (videos.pexels.com only). Per video, picks the best file: the requested
    orientation preferred, height closest to H, must have a direct mp4 link."""
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key:
        return []
    try:
        r = requests.get("https://api.pexels.com/v1/videos/search",
                         params={"query": query, "per_page": 8,
                                 "orientation": orientation, "size": "medium"},
                         headers={"Authorization": key},
                         timeout=20)
        r.raise_for_status()
        out = []
        for vid in r.json().get("videos", []):
            best, best_score = None, None
            for f in vid.get("video_files", []) or []:
                link = f.get("link") or ""
                if not link or not link.lower().endswith(".mp4"):
                    continue
                host = link.split("/")[2].lower() if "://" in link else ""
                if host != "videos.pexels.com":
                    continue
                w = f.get("width") or 0
                h = f.get("height") or 0
                if not w or not h:
                    continue
                # score: requested orientation first, then height closest to H
                if orientation == "landscape":
                    score = (1000 if w > h else 0) - abs(h - H)
                else:
                    score = (1000 if h > w else 0) - abs(h - H)
                if best_score is None or score > best_score:
                    best, best_score = link, score
            if best:
                out.append(best)
            if len(out) >= n:
                break
        return out
    except Exception as ex:
        print(f"pexels videos failed ({query}): {ex}", file=sys.stderr)
        return []


def pixabay_videos(query, n=6, orientation="portrait"):
    """Free stock VIDEO clips from the Pixabay Videos API (direct).
    Key from env PIXABAY_API_KEY (the key shown on pixabay.com/api/docs/;
    one key covers both the image and the video search); no key => []
    (caller falls back). Pexels stopped issuing new keys (2026-11-12),
    so Pixabay is the FIRST live video source; Pexels videos stay in the
    chain behind it and work again if Pexels resumes.
    orientation: "portrait" (default — the vertical Reels) or "landscape"
    (the 16:9 long video). Returns list of direct mp4 file URLs
    (cdn.pixabay.com only). Per video, picks the best file: the requested
    orientation preferred, height closest to H, must have a direct mp4 link.
    Pixabay rules: cache responses 24h; download the files (no permanent
    hotlinking). Free limit: 100 requests / 60 seconds."""
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        return []
    params = {"key": key, "q": query, "video_type": "all",
              "orientation": ("vertical" if orientation != "landscape"
                              else "horizontal"),
              "per_page": 10, "order": "popular", "safesearch": "true"}
    try:
        r = requests.get("https://pixabay.com/api/videos/",
                         params=params, timeout=20)
        r.raise_for_status()
        out = []
        for hit in r.json().get("hits", []):
            best, best_score = None, None
            for f in (hit.get("videos") or {}).values():
                if not isinstance(f, dict):
                    continue
                link = f.get("url") or ""
                if not link or not link.lower().endswith(".mp4"):
                    continue
                host = link.split("/")[2].lower() if "://" in link else ""
                if host != "cdn.pixabay.com":
                    continue
                w = f.get("width") or 0
                h = f.get("height") or 0
                if not w or not h:
                    continue
                # score: requested orientation first, then height closest to H
                if orientation == "landscape":
                    score = (1000 if w > h else 0) - abs(h - H)
                else:
                    score = (1000 if h > w else 0) - abs(h - H)
                if best_score is None or score > best_score:
                    best, best_score = link, score
            if best:
                out.append(best)
            if len(out) >= n:
                break
        return out
    except Exception as ex:
        print(f"pixabay videos failed ({query}): {ex}", file=sys.stderr)
        return []


def download(url, path, tries=3, min_bytes=5000):
    """Download with retries + browser-like headers (some hosts 403/429 bare requests).
    min_bytes: reject files smaller than this (photos: 5 KB; video clips: 100 KB)."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://www.google.com/",
    }
    for attempt in range(tries):
        try:
            r = requests.get(url, timeout=60, headers=headers)
            r.raise_for_status()
            with open(path, "wb") as f:
                f.write(r.content)
            if os.path.getsize(path) > min_bytes:
                return True
        except Exception as ex:
            print(f"download failed {url}: {ex} (attempt {attempt+1}/{tries})",
                  file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return False


def make_card_image(title, path, font_path):
    """Fallback visual: a 720x1280 title card drawn with PIL (used when no
    stock photo downloads). Guarantees every story has images so ffmpeg
    never receives zero segments."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (W, H), (18, 32, 54))
    dr = ImageDraw.Draw(img)
    # subtle vertical bands for texture
    for x in range(0, W, 40):
        dr.rectangle([x, 0, x + 20, H], fill=(24, 42, 68))
    font = (ImageFont.truetype(font_path, 64) if font_path
            else ImageFont.load_default())
    words = title.split()
    lines, cur = [], ""
    tmp = Image.new("RGB", (10, 10))
    tdr = ImageDraw.Draw(tmp)
    for w in words:
        t = (cur + " " + w).strip()
        if tdr.textlength(display_text(t), font=font) <= W - 80:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines[:4]
    lh = 100
    y = H // 2 - lh * len(lines) // 2
    for ln in lines:
        disp = display_text(ln)
        tw = tdr.textlength(disp, font=font)
        dr.text(((W - tw) // 2, y), disp, font=font, fill=(255, 255, 255))
        y += lh
    img.save(path)
    return path


def get_font():
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50000:
        return FONT_PATH
    if download(FONT_URL, FONT_PATH):
        return FONT_PATH
    return None


def render_title_png(text, path, font_path, size=54, max_w=W - 60):
    """Render the title correctly onto a transparent PNG.
    Arabic (news mode used to be Arabic) is reshaped + bidi-ordered;
    English passes through unchanged (see display_text)."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
    # wrap into lines that fit max_w
    words = text.split()
    lines, cur = [], ""
    tmp = Image.new("RGBA", (10, 10))
    dr = ImageDraw.Draw(tmp)
    for w in words:
        t = (cur + " " + w).strip()
        if dr.textlength(display_text(t), font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    lines = lines[:3]
    lh = int(size * 1.5)
    img = Image.new("RGBA", (W, lh * len(lines) + 30), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    y = 10
    for ln in lines:
        disp = display_text(ln)
        tw = dr.textlength(disp, font=font)
        # dark rounded backdrop behind each line for readability
        dr.rounded_rectangle([(W - tw) // 2 - 14, y - 4, (W + tw) // 2 + 14, y + size + 10],
                             radius=18, fill=(0, 0, 0, 170))
        dr.text(((W - tw) // 2, y), disp, font=font, fill=(255, 255, 255, 255))
        y += lh
    img.save(path)
    return path


def make_voice(text, path, voice="ar-SA-HamedNeural"):
    """Arabic TTS via Edge (free). Returns True on success."""
    import asyncio
    import edge_tts
    async def _run():
        com = edge_tts.Communicate(text, voice, rate="+5%")
        await com.save(path)
    try:
        asyncio.run(_run())
        return os.path.exists(path) and os.path.getsize(path) > 1000
    except Exception as ex:
        print(f"tts failed: {ex}", file=sys.stderr)
        return False


def probe_duration(path):
    """Duration in seconds of an audio/video file via ffprobe; 0.0 on failure."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def story_audio_len(path):
    return probe_duration(path)


def silent_audio(path, seconds):
    """Silent mp3 of `seconds` (fallback when edge-tts is unreachable, e.g.
    on this VM behind the egress proxy; the real TTS works on GitHub
    runners). Returns True if a valid file was written."""
    try:
        r = subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                            "anullsrc=r=44100:cl=stereo", "-t", str(seconds),
                            "-c:a", "libmp3lame", "-b:a", "96k", path],
                           capture_output=True, text=True, timeout=120)
        return (r.returncode == 0 and os.path.exists(path)
                and os.path.getsize(path) > 1000)
    except Exception as ex:
        print(f"silent audio failed: {ex}", file=sys.stderr)
        return False


def get_music():
    """Path to the background-music track, or None. Committed file first;
    otherwise one download attempt from the stable Mixkit URL; if that
    also fails, the build proceeds with voice only (never crashes)."""
    if os.path.exists(MUSIC_PATH) and os.path.getsize(MUSIC_PATH) > 10000:
        return MUSIC_PATH
    try:
        if download(MUSIC_URL, MUSIC_PATH, min_bytes=10000):
            return MUSIC_PATH
    except Exception as ex:
        print(f"music download failed: {ex}", file=sys.stderr)
    return None


def music_mix_filters(music_idx, total_dur):
    """Filter lines mixing the looped music track UNDER the voice concat
    [aout] at MUSIC_VOL. aformat normalizes both legs to 44.1k stereo so
    amix always gets matching formats; normalize=0 keeps the voice at full
    volume (amix would otherwise halve it). Output label: [aout2]."""
    return [f"[{music_idx}:a]atrim=0:{total_dur},volume={MUSIC_VOL},"
            "aformat=sample_rates=44100:channel_layouts=stereo[mus]",
            "[aout]aformat=sample_rates=44100:channel_layouts=stereo[av]",
            "[av][mus]amix=inputs=2:duration=first:normalize=0[aout2]"]


def template_narration(st, max_chars):
    """Narration text for a built-in template topic: title + script,
    truncated at a word boundary. The SAME function is used by the builders
    and by scripts/prefetch_media.py, so a pooled audio file always matches
    the text the builder would speak."""
    return st["title"] + ". " + fit_text(st["desc"], max_chars)


def narration_hash(text, voice):
    """Pool key for pre-rendered audio: hash of (voice + narration text).
    If the script changes by one word, the hash changes and the pooled audio
    is NOT reused — it is re-generated."""
    return hashlib.md5(f"{voice}\n{text}".encode("utf-8")).hexdigest()[:16]


def load_pool():
    """Read pool.json; return only entries whose local file exists and is
    non-empty, each with a 'local' absolute path. Missing/empty pool => []."""
    try:
        with open(POOL_INDEX, encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        p = os.path.join(BASE, e.get("path", ""))
        if os.path.exists(p) and os.path.getsize(p) > 0:
            out.append({**e, "local": p})
    return out


def _orient_rank(entry_orientation, want):
    """0 = exact orientation match, 1 = square/any, 2 = opposite (ffmpeg
    crops it anyway, so it is still usable as a last resort)."""
    o = (entry_orientation or "any").lower()
    if o == want:
        return 0
    if o in ("square", "any"):
        return 1
    return 2


def pool_pick(pool, topic, kind, orientation, n, used):
    """Up to n pool entries for (topic, kind), best orientation first,
    preferring files not yet used in this run (reuse only when exhausted)."""
    cands = [e for e in pool
             if e.get("topic") == topic and e.get("kind") == kind]
    rank = lambda e: _orient_rank(e.get("orientation"), orientation)
    fresh = sorted([e for e in cands if e["local"] not in used], key=rank)
    stale = sorted([e for e in cands if e["local"] in used], key=rank)
    return (fresh + stale)[:n]


def pool_audio(narr, voice):
    """Pooled narration audio whose script_hash matches (voice + narr), or
    None. Only for fixed texts (Islamic template scripts, end CTA) —
    news narration is fresh every run and is never pooled."""
    want = narration_hash(narr, voice)
    for e in load_pool():
        if e.get("kind") != "audio" or e.get("script_hash") != want:
            continue
        p = e["local"]
        if os.path.getsize(p) > 1000 and probe_duration(p) > 0:
            return p
    return None


def gather_visuals(pool, topic, prefix, n, orientation, used, font, story_title):
    """Visuals for one story, pool FIRST:
    pooled video clips -> live Pixabay videos -> live Pexels videos
    (dormant while Pexels pauses new keys) -> pooled photos -> live photos
    (Pexels -> Unsplash -> Openverse) -> reuse -> title card.
    orientation: "portrait" (vertical Reels) or "landscape" (long video).
    Returns [(path, "video"|"image")], never empty."""
    visuals = []
    # 0. Pooled video clips (pre-fetched, already validated).
    for e in pool_pick(pool, topic, "video", orientation, n, used):
        visuals.append((e["local"], "video"))
        used.add(e["local"])
        print(f"  pool video: {os.path.basename(e['local'])}")
    # 1. Live Pixabay video clips (first live video source — Pexels new
    # keys are paused, so Pixabay carries the video slots for now).
    if len(visuals) < n:
        for j, u in enumerate(pixabay_videos(topic, n=6, orientation=orientation)):
            p = os.path.join(ASSETS, f"{prefix}_pbvid{j}.mp4")
            if download(u, p, min_bytes=100_000) and probe_duration(p) > 0:
                visuals.append((p, "video"))
            if len(visuals) >= n:
                break
    # 2. Live Pexels video clips (dormant without a key; works again if
    # Pexels resumes issuing keys).
    if len(visuals) < n:
        for j, u in enumerate(pexels_videos(topic, n=6, orientation=orientation)):
            p = os.path.join(ASSETS, f"{prefix}_vid{j}.mp4")
            if download(u, p, min_bytes=100_000) and probe_duration(p) > 0:
                visuals.append((p, "video"))
            if len(visuals) >= n:
                break
    # 3. Pooled photos.
    if len(visuals) < n:
        for e in pool_pick(pool, topic, "photo", orientation,
                           n - len(visuals), used):
            visuals.append((e["local"], "image"))
            used.add(e["local"])
            print(f"  pool photo: {os.path.basename(e['local'])}")
    # 4. Live photos: Pexels (direct API) -> Unsplash (direct API) ->
    # Openverse (keyless) -> title card. No RSS/agency visuals.
    if len(visuals) < n:
        urls = pexels_images(
            topic, orientation=("landscape" if orientation == "landscape" else None))
        if len(urls) < 6:
            urls += unsplash_images(topic)
        if len(urls) < 6:
            urls += openverse_images(topic)
        if len(urls) < 6:
            urls += openverse_images("news")
        for j, u in enumerate(urls[:6]):
            p = os.path.join(ASSETS, f"{prefix}_img{j}.jpg")
            if download(u, p):
                visuals.append((p, "image"))
            if len(visuals) >= n:
                break
    # fallback: reuse any visual we already have
    while len(visuals) < n and visuals:
        visuals.append(visuals[0])
    if not visuals:
        # last resort: generated title card, so the story is never skipped
        card = os.path.join(ASSETS, f"{prefix}_card.jpg")
        make_card_image(story_title, card, font)
        visuals = [(card, "image")] * n
        print(f"story {prefix}: no stock visuals, using title card",
              file=sys.stderr)
    return visuals


def main():
    # The channel is fully English now: English titles, English voice
    # (en-US-AriaNeural), English on-screen text and CTA in both modes.
    # Islamic mode (env ISLAMIC_ONLY=1): real Islamic news stories
    # first, then built-in template topics (rotating) fill the remaining
    # slots — the mode never depends on the feeds, so zero news stories
    # still yields a complete Reel. NEWS mode: real articles only.
    islam_only = os.environ.get("ISLAMIC_ONLY", "").strip() == "1"
    topic_mode = "islamic" if islam_only else "news"
    voice = EN_VOICE
    with open(os.path.join(BASE, "feeds.json"), encoding="utf-8") as f:
        feeds = json.load(f)
    if islam_only:
        try:
            pool = fetch_stories(feeds, limit=60)
        except Exception as ex:
            print(f"feed fetch failed ({ex}) — building from templates only",
                  file=sys.stderr)
            pool = []
        real = [s for s in pool if is_islamic(s)][:N_STORIES]
        need = N_STORIES - len(real)
        templates = ([template_story(t) for t in pick_islam_topics(need)]
                     if need > 0 else [])
        stories = real + templates
        print(f"Islamic mode: {len(real)} real story(ies) + "
              f"{len(templates)} template topic(s)")
    else:
        stories = fetch_stories(feeds)
        if len(stories) < N_STORIES:
            print(f"WARNING: only {len(stories)} stories found", file=sys.stderr)
        if not stories:
            print("ERROR: no stories fetched", file=sys.stderr)
            sys.exit(1)
    for s in stories:
        s["topic"] = topic_mode

    font = get_font()
    pool = load_pool()
    used = set()  # pool files already used in this run (avoid repeats)
    print(f"Media pool: {len(pool)} file(s)")
    print(f"Stories: {len(stories)}")
    segments = []  # dicts (see make_segment)
    for i, st in enumerate(stories):
        # Islamic mode always uses Islamic visuals; news mode maps the
        # title to its topic (English keywords).
        topic = (ISLAM_VISUAL_TOPIC if islam_only
                 else topic_for(st["title"]))
        visuals = gather_visuals(pool, topic, f"s{i}", N_IMG, "portrait",
                                 used, font, st["title"])
        title_png = os.path.join(ASSETS, f"s{i}_title.png")
        render_title_png(st["title"], title_png, font)
        # narration: title + short summary (fits ~12s). Template topics use
        # their script, truncated at a word boundary. Template narration is
        # a fixed text, so the pool may already hold its audio (keyed by
        # script hash); news narration is fresh every run and is always
        # generated here.
        audio_src = "gen"
        if st.get("template"):
            narr = template_narration(st, 220)
            pooled = pool_audio(narr, voice)
            audio = os.path.join(ASSETS, f"s{i}.mp3")
            if pooled:
                audio, audio_src = pooled, "pool"
            elif not make_voice(narr, audio, voice=voice):
                audio = None
        else:
            narr = st["title"] + ". " + st["desc"][:220]
            audio = os.path.join(ASSETS, f"s{i}.mp3")
            if not make_voice(narr, audio, voice=voice):
                audio = None
        n_vid = sum(1 for _, k in visuals if k == "video")
        print(f"story {i}: {st['title'][:60]} | visuals={len(visuals)} "
              f"(video={n_vid}) | audio={audio_src if audio else 'NO'}")
        # Presenter interstitial BEFORE this story (except the first one):
        # a brief slice of the presenter clip, visual only — this story's
        # narration starts over it, so the story segment below skips those
        # seconds (a_start=PRESENTER_SEC). If the presenter clip is
        # unavailable, no interstitial and no skip.
        story_skip = 0.0
        if i > 0:
            inter = presenter_segment(i - 1, "s", W, H, audio)
            if inter is not None:
                segments.append(inter)
                story_skip = PRESENTER_SEC if audio else 0.0
        segments.append(make_segment(visuals, title_png, audio, STORY_SEC,
                                      a_start=story_skip))

    # End CTA (his order: every Reel ends with a site-visit call, voice +
    # on-screen). Reuses the exact segment machinery above, so it adds one
    # 15s segment (~125s Reel total). English channel: English CTA + the
    # English site on the card.
    cta_card = os.path.join(ASSETS, "cta_card.jpg")
    make_card_image("breaking-news-en.vercel.app", cta_card, font)
    cta_title = os.path.join(ASSETS, "cta_title.png")
    render_title_png("For more news, visit our website", cta_title, font)
    cta_audio = os.path.join(ASSETS, "cta.mp3")
    pooled_cta = pool_audio(CTA_NARR, voice)
    if pooled_cta:
        cta_audio = pooled_cta
        print("CTA audio: pool")
    elif not make_voice(CTA_NARR, cta_audio, voice=voice):
        cta_audio = None
    segments.append(make_segment([(cta_card, "image")] * N_IMG, cta_title,
                                 cta_audio, STORY_SEC))

    if not segments:
        print("ERROR: no story segments to render", file=sys.stderr)
        sys.exit(1)

    out_mp4 = os.path.join(OUT, "reel.mp4")
    assemble_reel(segments, out_mp4)
    size = os.path.getsize(out_mp4)
    print(f"DONE: {out_mp4} ({size/1e6:.1f} MB)")
    with open(os.path.join(OUT, "stories.json"), "w", encoding="utf-8") as f:
        json.dump([{"title": s["title"], "link": s["link"],
                    "topic": s.get("topic", "news"),
                    "template": bool(s.get("template"))} for s in stories],
                  f, ensure_ascii=False, indent=2)


def assemble_reel(segments, out_mp4):
    """Build one mp4 from segments (dicts — see make_segment).
    Each story = 3 visuals (5s each; video clips play, photos loop) + title
    overlay + voice; presenter interstitials = 1 visual (4s, no title) with
    the next story's narration starting over it. All visuals are normalized
    to 720x1280 @ 30fps so the concat works with mixed video/photo inputs.
    Segment durations vary (15s stories, 4s interstitials, 15s CTA); each
    visual fills dur/len(visuals); each audio leg is padded to its segment
    length, so the video/audio concats always line up."""
    inputs, filters = [], []
    audio_labels = []
    idx = 0
    for si, seg in enumerate(segments):
        visuals = seg["visuals"]
        title_png = seg.get("title")
        audio = seg.get("audio")
        dur = seg["dur"]
        a_start = seg.get("a_start", 0.0)
        a_end = seg.get("a_end")
        vis_dur = dur / len(visuals)
        vis_indices = []
        for p, kind in visuals:
            if kind == "video":
                # real clip: trim to vis_dur, drop its own audio (voiceover covers it)
                inputs += ["-t", str(vis_dur), "-an", "-i", p]
            else:
                inputs += ["-loop", "1", "-t", str(vis_dur), "-i", p]
            vis_indices.append(idx)
            idx += 1
        # normalize every visual to 720x1280 @ 30fps (use the REAL input
        # indices: title and audio inputs sit between stories, so visuals
        # are NOT contiguous). fps=30 normalizes mixed video/photo rates.
        for v in vis_indices:
            filters.append(
                f"[{v}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1,fps=30[v{v}]")
        if len(vis_indices) == 1:
            seg_label = f"v{vis_indices[0]}"
        else:
            # concat the visuals into one segment of length dur
            filters.append(
                "".join(f"[v{v}]" for v in vis_indices) +
                f"concat=n={len(vis_indices)}:v=1:a=0[seg{si}]")
            seg_label = f"seg{si}"
        if title_png:
            inputs += ["-loop", "1", "-t", str(dur), "-i", title_png]
            title_idx = idx
            idx += 1
            filters.append(
                f"[{seg_label}][{title_idx}:v]overlay=(W-w)/2:H-h-90[sv{si}]")
        else:
            # no title overlay (presenter interstitial): pass through
            filters.append(f"[{seg_label}]null[sv{si}]")
        if audio:
            inputs += ["-i", audio]
            audio_idx = idx
            idx += 1
            # narration window [a_start, a_end): an interstitial plays the
            # first seconds of the next story's narration; the story itself
            # skips those seconds. apad pads the leg to the segment length.
            # asetpts is REQUIRED after atrim: atrim keeps the input
            # timestamps, so without the reset the leg starts late in the
            # concat (a 4s hole in the audio timeline per skipped story).
            if a_end is not None:
                af = (f"[{audio_idx}:a]atrim={a_start}:{a_end},"
                      f"asetpts=PTS-STARTPTS,apad=whole_dur={dur}[a{si}]")
            elif a_start > 0:
                af = (f"[{audio_idx}:a]atrim={a_start},"
                      f"asetpts=PTS-STARTPTS,apad=whole_dur={dur}[a{si}]")
            else:
                af = f"[{audio_idx}:a]apad=whole_dur={dur}[a{si}]"
            filters.append(af)
            audio_labels.append(f"[a{si}]")
        else:
            # silent audio so the final audio concat always has nseg streams
            filters.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{dur}[a{si}]")
            audio_labels.append(f"[a{si}]")
    nseg = len(segments)
    total_dur = sum(s["dur"] for s in segments)
    vcat = "".join(f"[sv{i}]" for i in range(nseg))
    filters.append(f"{vcat}concat=n={nseg}:v=1:a=0[vout]")
    if audio_labels:
        acat = "".join(audio_labels)
        filters.append(f"{acat}concat=n={nseg}:v=0:a=1[aout]")
        # Background music under the voice (looped, low volume). If no
        # music file is available the build still succeeds, voice only.
        music = get_music()
        if music:
            inputs += ["-stream_loop", "-1", "-i", music]
            music_idx = idx
            idx += 1
            filters += music_mix_filters(music_idx, total_dur)
            amap = ["-map", "[aout2]"]
            print(f"Background music: {os.path.basename(music)} "
                  f"at volume {MUSIC_VOL}")
        else:
            amap = ["-map", "[aout]"]
            print("Background music: none available, voice only",
                  file=sys.stderr)
    else:
        amap = []
    cmd = (["ffmpeg", "-y"] + inputs +
           ["-filter_complex", ";".join(filters),
            "-map", "[vout]"] + amap +
           ["-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0",
            "-crf", "28", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            "-r", "30", out_mp4])
    print("Running ffmpeg ...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
    if r.returncode != 0:
        print(r.stderr[-3000:], file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
