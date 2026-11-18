#!/usr/bin/env python3
"""Auto story Reel builder (FULLY ENGLISH channel).
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
Modes (env STORY_MODE, default "horror"):
  horror: built-in original horror stories (rotating).
  anime: built-in original anime stories (rotating).
Both modes are template-only and never depend on the feeds.
Output: out/reel.mp4
Background music: "Serene View" by Arulo (Mixkit, royalty-free, no
attribution required — see assets/MUSIC.md) is mixed under the narration
at volume 0.15; if the file is missing the build still succeeds, voice only.

MEDIA POOL (data/media_pool/ + pool.json, filled by scripts/prefetch_media.py):
visuals AND narration audio are picked from the pool FIRST, then the live
APIs. Visuals: pooled clips/photos for the story's topic. Audio: only the
fixed template scripts (and the fixed end CTA) can be pooled —
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
CTA_NARR = "For more stories, visit our website: Breaking News"
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


# Built-in story topics: each run picks topics from these lists (rotating)
# and builds segments from them. Horror stories replace the old news mode;
# anime stories replace the old Islamic mode. STRICT RULE: original fiction
# only — never invent real news events, prices, release dates, or quotes.
# Every story below is an original short tale, one per batch (rotating).
HORROR_TOPICS = [
    {"title": "The House on Blackwood Lane",
     "script": "When Daniel inherited the old house on Blackwood Lane, the lawyer warned him never to go down to the basement after dark. On his first night, the lights flickered at exactly three in the morning, and a small voice called his name from below the stairs. He did not answer. The voice called again, closer this time, and it sounded exactly like his own voice when he was seven years old."},
    {"title": "The Last Train",
     "script": "Mara boarded the last train home, and the carriage was full, but none of the passengers had faces. The conductor walked down the aisle, punching tickets that were not there, and announced the next stop in a voice like dry leaves. The stop did not exist on any map. Mara stood up to leave, and every faceless head turned slowly toward her at the same time."},
    {"title": "The Mirror in Room 9",
     "script": "The hotel mirror in room nine showed a different room. In the reflection, the bed was made, the curtains were closed, and a man stood behind the guest, smiling. When Thomas turned around, the room was empty. He covered the mirror with a sheet and slept with the light on. In the morning, the sheet was folded neatly on the bed, and the mirror was waiting, clean and patient."},
    {"title": "The Dollmaker's Daughter",
     "script": "The dollmaker's dolls moved when no one was watching. He swore he never wound them, never touched them, yet every morning they stood in different places. The newest doll looked exactly like his daughter, who had vanished ten years ago. One night he heard tiny footsteps in the workshop, and a small voice whispered, Father, why did you make me out of wood when I was flesh?"},
    {"title": "The Forest Whispers",
     "script": "The fog in the forest whispered names. Hiker Jonas followed the sound of his own name, deeper and deeper between the trees, until the path disappeared behind him. The whispers grew louder, and he realised they were not calling him. They were spelling his name, letter by letter, in a voice that sounded like wind through hollow bones. He ran, but the forest had already learned the way he walked."},
    {"title": "The Phone Call",
     "script": "The phone rang at midnight, and the caller ID showed his own number. The voice on the other end was his own voice, and it knew everything he would do tomorrow. It told him not to take the lift, not to answer the door, not to drink the coffee. He laughed and hung up. The next morning, the lift cable snapped, someone knocked three times, and the coffee smelled faintly of something that was not coffee."},
    {"title": "The Basement Door",
     "script": "The basement door in the new house was locked from the inside. Every night at two in the morning, something knocked three times, polite and patient, as if it were only asking to be let in. The family stopped going downstairs. Then the knocking moved upstairs. It came from inside the walls now, three soft knocks, and last night, for the first time, it knocked from inside the bedroom closet."},
    {"title": "The Old Photograph",
     "script": "Elena found an old photograph in her grandmother's attic. It showed a little girl who looked exactly like her, standing in front of a house she had never visited, holding the hand of a man she had never met. On the back, in faded ink, was written: Elena, age seven, the summer we never left. Elena was six years old when the photograph was taken. She has never been seven."},
    {"title": "The Lighthouse Keeper",
     "script": "The lighthouse keeper logged a ship every full moon, a ship that had sunk a hundred years ago. It sailed into the harbour without a sound, its lanterns lit, its decks empty, and it waited until dawn. On the last night of his watch, the keeper saw a figure on the deck waving to him. It was himself, older, wearier, and it was pointing at the rocks below the lighthouse."},
    {"title": "The Elevator",
     "script": "The elevator in the old building stopped between floors, and the buttons lit up on their own, counting down from thirteen. There were only twelve floors. The doors opened onto a hallway that did not exist, carpeted in red, lit by a single flickering bulb. A woman stood at the far end, holding a suitcase. She smiled and said, I have been waiting for the lift for a very long time. Won't you come in?"},
    {"title": "The Snowstorm Cabin",
     "script": "Snowed in on the mountain, four friends found a cabin with the fire already lit and five plates set on the table. There were only four of them. They ate in silence, and the fifth plate stayed empty, but the food on it slowly disappeared. That night, they heard footsteps in the snow outside, circling the cabin, and a voice counted them through the door: one, two, three, four, five."},
    {"title": "The Ventriloquist",
     "script": "The ventriloquist's dummy spoke when his mouth was closed. It knew things he had never told it: the name of his first love, the place he had buried the money, the reason he never slept. The audience laughed, but the dummy did not laugh. It leaned close to the microphone and whispered, He thinks he is the one holding the strings. He has never once looked at his own hands."},
]

ANIME_TOPICS = [
    {"title": "The Cherry Blossom Spirit",
     "script": "Every spring, the cherry blossom tree behind the old shrine bloomed for exactly one night. Yuki met its spirit there, a girl made of petals and light, who offered her a single wish. But the wish could not be for herself. Yuki wished for her grandmother's garden to bloom again, and the next morning every tree in the village was covered in blossoms, and the spirit smiled, and was gone with the wind."},
    {"title": "The Samurai's Last Duel",
     "script": "The old samurai had not drawn his sword in twenty years. At dawn, he walked to the river to face his final duel, not for honour, but to protect a village that had long forgotten him. His opponent was young and fast and certain. The old man closed his eyes, remembered every lesson his master had taught him, and when the sun rose over the water, only one of them was still standing."},
    {"title": "The Girl Who Talked to the Wind",
     "script": "Aiko could hear the wind's stories. It told her of distant seas and faraway mountains, and one evening it whispered a warning: a great storm was coming, and it would change her town forever. Nobody believed a girl who talked to the wind. So Aiko gathered the fishermen's boats herself, tied down the market stalls, and when the storm arrived at midnight, the town was ready, because one girl had listened."},
    {"title": "The Robot Who Dreamed",
     "script": "In a scrapyard at the edge of the city, a small robot dreamed of the stars. Its circuits were old and its joints were rusted, but every night it looked up at the sky and imagined flying. One night a falling star answered, landing softly beside it, glowing warm and bright. The star said, I fell because I wanted to see the world. The little robot said, Then let us see it together."},
    {"title": "The Dragon of Mount Kiri",
     "script": "Young Kenji climbed Mount Kiri to meet the last dragon, a creature said to breathe fire and guard a mountain of gold. At the summit he found not a monster, but a small dragon hiding in a cave, trembling. It was afraid of the dark. Kenji lit a lantern and stayed with it until morning, and the dragon, who had guarded gold for a hundred years, gave him the greatest treasure it had: a friend."},
    {"title": "The Time-Traveling Student",
     "script": "Hana found an old clock in the school library that could rewind a single day. She used it to save her friend from a fall, and then again, and again, each time learning something new about the people around her. On the last rewind, she realised the clock was not giving her more time. It was teaching her to pay attention, because every day, lived properly, is already a gift."},
    {"title": "The Cat Café",
     "script": "At the end of a quiet street stood a café where the cats granted wishes to lonely customers. The price was one memory, freely given. An old man wished to see his wife's smile once more, and gave the memory of his first bicycle. A young woman wished for courage, and gave the memory of a rainy afternoon. The cats purred, the wishes came true, and the café smelled of coffee and second chances."},
    {"title": "The Star Festival",
     "script": "Once a year, the stars fell to earth as paper lanterns, and the children of the village ran to catch them. Ren caught one that glowed brighter than the rest, and inside it he saw his own future: a road, a mountain, a friend he had not yet met. He let the lantern go, and it rose back into the sky, because some futures are better when you walk toward them slowly, one step at a time."},
    {"title": "The Swordsmith's Apprentice",
     "script": "The apprentice had forged a hundred swords, and none of them was good enough. His master told him that a true sword is not made from steel, but from the heart of the one who makes it. So the boy worked through the winter, through the spring, pouring his patience and his kindness into the metal. When the sword was finished, it could cut through anything, even sorrow itself, and it never once drew blood."},
    {"title": "The Paper Crane",
     "script": "Mei folded a thousand paper cranes, one for each day her brother was away at sea. On the thousandth crane, her fingers trembled, and as she finished the last fold, the crane fluttered, stretched its paper wings, and came alive. It carried her message across the ocean, over the waves, to the ship where her brother stood at the rail, and when he opened his hand, the crane landed there, warm as a heartbeat."},
    {"title": "The Moon Rabbit",
     "script": "The rabbit on the moon had one job: to watch over the wishes of children. One night it saw a wish fading, a small light going out, because a child had stopped believing. So the rabbit descended to earth on a beam of moonlight, found the child asleep, and whispered the wish back into her dreams. In the morning, the child woke up smiling, and far above, the moon seemed just a little bit brighter."},
    {"title": "The Ninja Academy",
     "script": "At the ninja academy, every student was fast, silent and deadly, except for Takeshi, who was clumsy, loud and always late. On the day of the final test, the academy was attacked, and the fastest students froze. But Takeshi did the one thing no one expected: he made his friends laugh, and their laughter broke the fear, and together they drove the attackers away. His greatest skill, the master said, was the one nobody had thought to teach."},
]


def story_rotation_seed():
    """Rotation offset for picking story topics: from env
    STORY_ROTATION_SEED (the workflow passes github.run_number) so every
    run covers different stories; falls back to the UTC day number."""
    raw = os.environ.get("STORY_ROTATION_SEED", "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    import datetime
    return datetime.datetime.utcnow().toordinal()


def pick_story_topics(topics, n, seed=None):
    """Pick n topics from a story list, rotating by seed (wraps around)."""
    if seed is None:
        seed = story_rotation_seed()
    start = seed % len(topics)
    return [topics[(start + k) % len(topics)] for k in range(n)]


# Cross-batch no-repeat: the titles already used, per mode, live in
# data/used_stories.json, which is COMMITTED back to the repo by the
# workflow after every run (see build-reel.yml "Save used stories").
# The Actions cache cannot do this reliably: within a week, later runs
# restore the first run's cache and their own updates are not re-saved.
USED_STORIES_FILE = os.path.join(BASE, "data", "used_stories.json")


def load_used_stories():
    """Titles already used, per mode. Missing file = nothing used yet."""
    try:
        with open(USED_STORIES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {"horror": list(data.get("horror", [])),
                "anime": list(data.get("anime", []))}
    except Exception:
        return {"horror": [], "anime": []}


def save_used_stories(used):
    os.makedirs(os.path.dirname(USED_STORIES_FILE), exist_ok=True)
    with open(USED_STORIES_FILE, "w", encoding="utf-8") as f:
        json.dump(used, f, ensure_ascii=False, indent=2)


def pick_fresh_stories(topics, mode, n):
    """Pick n stories from `topics` that have NOT been used before in this
    mode. The picked titles are appended to data/used_stories.json (saved
    here; the workflow commits the file back to the repo). When fewer than
    n fresh stories remain, the used list for that mode resets and picking
    starts over from the full list. Returns the picked topic dicts."""
    used = load_used_stories()
    used_titles = set(used.get(mode, []))
    fresh = [t for t in topics if t["title"] not in used_titles]
    if len(fresh) < n:
        print(f"{mode}: used list exhausted "
              f"({len(used_titles)}/{len(topics)} used) — resetting",
              file=sys.stderr)
        used[mode] = []
        fresh = list(topics)
    picked = pick_story_topics(fresh, n)
    used[mode] = used.get(mode, []) + [t["title"] for t in picked]
    save_used_stories(used)
    return picked


# Visual search topics (stock photos/video clips). The robot
# (scripts/prefetch_media.py) collects visuals for these topics; the
# builders use them for the matching story segments.
HORROR_VISUAL_TOPIC = "dark horror"
ANIME_VISUAL_TOPIC = "anime"


def template_story(topic):
    """A built-in topic as a story-like dict (no news article behind it)."""
    return {"title": topic["title"], "desc": topic["script"], "link": "",
            "ts": 0, "template": True}


# English keyword -> English stock-photo/video topic (the channel is fully
# English now: English feeds, English titles, English voice).
TOPICS = [
    # Horror and anime first: these are the two story modes now. The
    # builders use a fixed visual topic per mode, but prefetch also
    # fetches visuals for every topic in this list.
    (["horror", "haunted", "ghost", "scary", "dark", "fear", "monster", "demon", "nightmare"], "dark horror"),
    (["anime", "manga", "samurai", "ninja", "dragon", "spirit", "cherry blossom", "tokyo", "japan"], "anime"),
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


# English stopwords dropped from story titles when building a specific
# visual-search query (the generic words add nothing to a stock search).
STOPWORDS = {
    "the", "a", "an", "in", "on", "of", "to", "for", "and", "with",
    "after", "before", "is", "are", "was", "were", "be", "been", "by",
    "at", "as", "it", "its", "this", "that", "from", "into", "over",
    "under", "about", "between", "through", "during", "above", "below",
    "up", "down", "out", "off", "again", "further", "then", "once",
    "here", "there", "when", "where", "why", "how", "all", "any", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor",
    "not", "only", "own", "same", "so", "than", "too", "very", "can",
    "will", "just", "should", "now", "has", "have", "had", "do", "does",
    "did", "but", "or", "if", "while", "who", "whom", "which", "what",
    "says", "said", "new",
}


def _content_words(text):
    """Lowercase content words of a text: drop stopwords, words shorter
    than 3 chars, and pure numbers; deduped, in order of appearance."""
    words = []
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) < 3 or w.isdigit() or w in STOPWORDS:
            continue
        if w not in words:
            words.append(w)
    return words


def story_keywords(title, desc=""):
    """Up to 3 specific search words for this story's visuals, taken
    from the title first (topped up from the description if the title
    gives fewer than 3). Space-joined, or "" if none remain. Used as
    the specific query for live visual searches, so photos/clips match
    the story itself instead of just its broad topic."""
    words = _content_words(title)
    if len(words) < 3:
        for w in _content_words(desc):
            if w not in words:
                words.append(w)
            if len(words) >= 3:
                break
    return " ".join(words[:3])


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


def _provider_id(url):
    """Stable provider ID from a stock URL (numeric Pexels id / Unsplash
    photo uid / Pexels video id / Pixabay video id); md5 of the URL as a
    fallback. Same logic as scripts/prefetch_media.py provider_id (kept
    duplicated here on purpose: prefetch imports build_reel, so importing
    it back would be a circular import)."""
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


def _url_key(provider, url):
    """Cross-run identity of a stock visual: "provider:provider_id"."""
    return f"{provider}:{_provider_id(url)}"


def _pool_key(e):
    """Identity of a pool entry, same form as _url_key.
    Pool video entries store provider_id with an orientation suffix
    ("12345_portrait"); it is stripped so the key matches the key of
    the same clip fetched live (which has no suffix) — otherwise a clip
    used from the pool could be picked again from a live fetch in the
    same video."""
    pid = e.get("provider_id", "") or ""
    if e.get("kind") == "video":
        for suf in ("_portrait", "_landscape"):
            if pid.endswith(suf):
                pid = pid[:-len(suf)]
                break
    return f"{e.get('provider', '')}:{pid}"


def _orient_rank(entry_orientation, want):
    """0 = exact orientation match, 1 = square/any, 2 = opposite (ffmpeg
    crops it anyway, so it is still usable as a last resort)."""
    o = (entry_orientation or "any").lower()
    if o == want:
        return 0
    if o in ("square", "any"):
        return 1
    return 2


def pool_pick(pool, topic, kind, orientation, n, used, strict=False):
    """Up to n pool entries for (topic, kind), best orientation first.
    Fresh = file not yet used in this run, by local path AND by visual
    identity (_pool_key) — a clip can exist both as a pool file and as a
    live-fetch result, so the key check keeps the same clip from being
    picked twice in one video. strict=True: return ONLY
    fresh entries, never fall back to already-used ones (now used for
    BOTH horror and anime videos). strict=False: fresh first, then
    stale as fallback (old behaviour, no longer used by the builders)."""
    cands = [e for e in pool
             if e.get("topic") == topic and e.get("kind") == kind]
    rank = lambda e: _orient_rank(e.get("orientation"), orientation)

    def _fresh(e):
        return e["local"] not in used and _pool_key(e) not in used

    fresh = sorted([e for e in cands if _fresh(e)], key=rank)
    if strict:
        return fresh[:n]
    stale = sorted([e for e in cands if not _fresh(e)], key=rank)
    return (fresh + stale)[:n]


def pool_audio(narr, voice):
    """Pooled narration audio whose script_hash matches (voice + narr), or
    None. Only for fixed texts (template scripts, end CTA)."""
    want = narration_hash(narr, voice)
    for e in load_pool():
        if e.get("kind") != "audio" or e.get("script_hash") != want:
            continue
        p = e["local"]
        if os.path.getsize(p) > 1000 and probe_duration(p) > 0:
            return p
    return None


def _fetch_specific_first(fetch, query, topic, need, **kw):
    """Live-search URLs for one provider: the specific story `query`
    first; if it returns fewer than `need` URLs, also try the broad
    `topic` and combine both lists (specific first), deduped by URL.
    `fetch` is called as fetch(q, **kw). `query` may be "" (then only
    the broad topic is tried)."""
    urls, seen = [], set()
    for q in ([query] if query else []) + [topic]:
        for u in fetch(q, **kw):
            if u not in seen:
                seen.add(u)
                urls.append(u)
        if len(urls) >= need:
            break
    return urls


def gather_visuals(pool, topic, prefix, n, orientation, used, font, story_title, strict=False, query=""):
    """Visuals for one story, pool FIRST:
    pooled video clips -> live Pixabay videos -> live Pexels videos
    (dormant while Pexels pauses new keys) -> pooled photos -> live photos
    (Pexels -> Unsplash -> Openverse) -> title card.
    orientation: "portrait" (vertical Reels) or "landscape" (long video).
    `used` is the within-run record of visuals already picked in this
    video: local paths AND "provider:provider_id" keys (they cannot
    collide in the set). Every visual picked here is recorded both ways,
    so the same clip/photo is never picked twice in one video. Live
    fetches are skipped when their key is already in `used`.
    strict=True: never repeat a visual in this video — if a
    story still has fewer than n visuals, the remaining slots are filled
    with a generated title card, never by repeating visuals[0] (now
    used for BOTH horror and anime videos).
    strict=False: the old reuse fallback is kept (no longer used).
    query: specific story keywords (see story_keywords), may be "".
    Live searches try the specific query FIRST, then the broad `topic`
    to fill up; pool steps always use the broad topic.
    Returns [(path, "video"|"image")], never empty."""
    visuals = []
    # 0. Pooled video clips (pre-fetched, already validated).
    for e in pool_pick(pool, topic, "video", orientation, n, used, strict):
        visuals.append((e["local"], "video"))
        used.add(e["local"])
        used.add(_pool_key(e))
        print(f"  pool video: {os.path.basename(e['local'])}")
    # 1. Live Pixabay video clips (first live video source — Pexels new
    # keys are paused, so Pixabay carries the video slots for now).
    # Specific story query first, broad topic to fill up.
    if len(visuals) < n:
        need = n - len(visuals)
        for j, u in enumerate(_fetch_specific_first(
                pixabay_videos, query, topic, need, n=6,
                orientation=orientation)):
            if _url_key("pixabay", u) in used:
                continue
            p = os.path.join(ASSETS, f"{prefix}_pbvid{j}.mp4")
            if download(u, p, min_bytes=100_000) and probe_duration(p) > 0:
                visuals.append((p, "video"))
                used.add(_url_key("pixabay", u))
            if len(visuals) >= n:
                break
    # 2. Live Pexels video clips (dormant without a key; works again if
    # Pexels resumes issuing keys).
    if len(visuals) < n:
        need = n - len(visuals)
        for j, u in enumerate(_fetch_specific_first(
                pexels_videos, query, topic, need, n=6,
                orientation=orientation)):
            if _url_key("pexels", u) in used:
                continue
            p = os.path.join(ASSETS, f"{prefix}_vid{j}.mp4")
            if download(u, p, min_bytes=100_000) and probe_duration(p) > 0:
                visuals.append((p, "video"))
                used.add(_url_key("pexels", u))
            if len(visuals) >= n:
                break
    # 3. Pooled photos.
    if len(visuals) < n:
        for e in pool_pick(pool, topic, "photo", orientation,
                           n - len(visuals), used, strict):
            visuals.append((e["local"], "image"))
            used.add(e["local"])
            used.add(_pool_key(e))
            print(f"  pool photo: {os.path.basename(e['local'])}")
    # 4. Live photos: Pexels (direct API) -> Unsplash (direct API) ->
    # Openverse (keyless) -> title card. No RSS/agency visuals.
    # (url, provider) pairs — the provider is needed for the used key.
    # Each source is tried with the specific story query first, then the
    # broad topic to fill up.
    if len(visuals) < n:
        need = n - len(visuals)
        url_prov = []
        seen = set()
        photo_sources = [
            (pexels_images, "pexels",
             {"n": 8, "orientation": ("landscape" if orientation == "landscape" else None)}),
            (unsplash_images, "unsplash", {"n": 8}),
            (openverse_images, "openverse", {"n": 8}),
        ]
        for fetch, prov, kw in photo_sources:
            for u in _fetch_specific_first(fetch, query, topic, need, **kw):
                if u not in seen:
                    seen.add(u)
                    url_prov.append((u, prov))
            if len(url_prov) >= 6:
                break
        if len(url_prov) < 6:
            for u in openverse_images("news", n=8):
                if u not in seen:
                    seen.add(u)
                    url_prov.append((u, "openverse"))
        for j, (u, prov) in enumerate(url_prov):
            if _url_key(prov, u) in used:
                continue
            p = os.path.join(ASSETS, f"{prefix}_img{j}.jpg")
            if download(u, p):
                visuals.append((p, "image"))
                used.add(_url_key(prov, u))
            if len(visuals) >= n:
                break
    # fallback: fill the remaining slots.
    if len(visuals) < n:
        if strict:
            # News mode: never repeat a visual in this video — fill with
            # a generated title card (not a stock clip/photo, so it is
            # not a repeat).
            missing = n - len(visuals)
            card = os.path.join(ASSETS, f"{prefix}_card.jpg")
            make_card_image(story_title, card, font)
            visuals += [(card, "image")] * missing
            print(f"story {prefix}: {missing} slot(s) filled with title "
                  f"card (strict, no repeat)", file=sys.stderr)
        else:
            # reuse any visual we already have
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
    # The channel is fully English: English titles, English voice
    # (en-US-AriaNeural), English on-screen text and CTA in both modes.
    # Modes (env STORY_MODE, default "horror"):
    #   horror: built-in original horror stories (rotating).
    #   anime: built-in original anime stories (rotating).
    # Both modes are template-only — they never depend on the feeds, so
    # they always yield a complete Reel.
    mode = os.environ.get("STORY_MODE", "horror").strip().lower()
    if mode not in ("horror", "anime"):
        print(f"WARNING: unknown STORY_MODE {mode!r}, using horror",
              file=sys.stderr)
        mode = "horror"
    topic_mode = mode
    voice = EN_VOICE
    topics = HORROR_TOPICS if mode == "horror" else ANIME_TOPICS
    stories = [template_story(t)
               for t in pick_fresh_stories(topics, mode, N_STORIES)]
    print(f"{mode} mode: {len(stories)} template story(ies)")
    for s in stories:
        s["topic"] = topic_mode

    font = get_font()
    pool = load_pool()
    used = set()  # visuals already used in THIS video, by local path and
    # by "provider:provider_id" key (see gather_visuals) — no clip/photo
    # is ever repeated within one video (horror and anime alike).
    # strict = no-repeat applies to BOTH horror and anime videos: no clip
    # or photo is repeated within one video (his order 2026-11-17).
    strict = True
    print(f"Media pool: {len(pool)} file(s)")
    print(f"Stories: {len(stories)}")
    segments = []  # dicts (see make_segment)
    for i, st in enumerate(stories):
        # Each mode uses its own fixed visual topic.
        topic = (HORROR_VISUAL_TOPIC if mode == "horror"
                 else ANIME_VISUAL_TOPIC)
        # Specific visual query from the story itself, so the clips/photos
        # match the story being narrated, not just its broad topic.
        sq = story_keywords(st["title"], st.get("desc", ""))
        visuals = gather_visuals(pool, topic, f"s{i}", N_IMG, "portrait",
                                 used, font, st["title"], strict, query=sq)
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
        # Audio-visual sync: the segment must last at least as long as
        # its narration, so the photos and on-screen text stay up until
        # the voice finishes. Falls back to STORY_SEC if the duration
        # cannot be measured (audio missing/unreadable).
        audio_dur = story_audio_len(audio) if audio else 0.0
        seg_dur = max(STORY_SEC, audio_dur) if audio_dur > 0 else STORY_SEC
        print(f"story {i}: {st['title'][:60]} | visuals={len(visuals)} "
              f"(video={n_vid}) | audio={audio_src if audio else 'NO'} "
              f"| seg={seg_dur:.1f}s")
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
        segments.append(make_segment(visuals, title_png, audio, seg_dur,
                                      a_start=story_skip))

    # End CTA (his order: every Reel ends with a site-visit call, voice +
    # on-screen). Reuses the exact segment machinery above, so it adds one
    # 15s segment (~125s Reel total). English channel: English CTA + the
    # English site on the card.
    cta_card = os.path.join(ASSETS, "cta_card.jpg")
    make_card_image("breaking-news-en.vercel.app", cta_card, font)
    cta_title = os.path.join(ASSETS, "cta_title.png")
    render_title_png("For more stories, visit our website", cta_title, font)
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
                    "topic": s.get("topic", "horror"),
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
                # real clip: loop to fill vis_dur, drop its own audio
                # (voiceover covers it). The loop matters when a story
                # segment is extended to match a long narration: a short
                # clip must not end early and leave the segment short.
                inputs += ["-stream_loop", "-1", "-t", str(vis_dur),
                           "-an", "-i", p]
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
