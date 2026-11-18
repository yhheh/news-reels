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
Modes (env STORY_MODE, default "animals"):
  animals: built-in animal documentaries, one animal per story (rotating).
  history: built-in historical stories, one event per story (rotating).
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
CTA_NARR = "For more animal and history stories, visit our website: Breaking News"
EN_VOICE = "en-US-GuyNeural"

# Background music (Mixkit, royalty-free — see assets/MUSIC.md). Mixed UNDER
# the narration at low volume; the voice stays dominant. The file is
# committed in assets/music.mp3; MUSIC_URL is the build-time fallback.
MUSIC_VOL = 0.15
MUSIC_PATH = os.path.join(BASE, "assets", "music.mp3")
MUSIC_URL = "https://assets.mixkit.co/music/443/443.mp3"
# Rotating background tracks (his order 2026-11-18: a DIFFERENT background
# sound in every video). All four URLs verified HTTP 200 on 2026-11-18.
# The track rotates by (run_number + mode offset), so the Reel and the
# long video in one batch get different tracks, and every batch rotates.
MUSIC_TRACKS = [
    "https://assets.mixkit.co/music/443/443.mp3",    # Serene View (Arulo)
    "https://assets.mixkit.co/music/1210/1210.mp3",
    "https://assets.mixkit.co/music/127/127.mp3",
    "https://assets.mixkit.co/music/130/130.mp3",
]

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
# and builds segments from them. Animal documentaries replace the old
# animal documentaries; historical stories are the second mode. STRICT RULE:
# factual documentary narration only — real animals, real history, never
# invented events, prices, or quotes. Every entry below is one animal or
# one historical topic, one per batch (rotating).
ANIMAL_TOPICS = [
    {"title": "The Lion",
     "script": "The lion is the king of the African savanna, and it is the only big cat that lives in a family group called a pride. A pride can have thirty lions, mostly females, who hunt together at night while the males guard the territory. Lions sleep for up to twenty hours a day, saving energy for the hunt. The male's great mane protects his neck in fights. A lion's roar is so powerful it can be heard eight kilometres away across the plains."},
    {"title": "The Elephant",
     "script": "The African elephant is the largest land animal on Earth, weighing as much as six tonnes. Its trunk is a miracle of nature, with forty thousand muscles, and it can pick up a single leaf or tear down a tree. Elephants never forget: they remember water holes and friends for decades. They live in close family herds led by the oldest female, and they mourn their dead, touching the bones gently with their trunks."},
    {"title": "The Wolf",
     "script": "Wolves are among the most loyal animals in the wild, living in family packs led by a breeding pair. A pack hunts together, chasing deer and elk for many kilometres until the prey is tired. Each wolf has a rank, and the whole pack works as one team. At night, their howls carry for miles, keeping the family together and warning other packs to stay away. A wolf can travel fifty kilometres in a single day."},
    {"title": "The Blue Whale",
     "script": "The blue whale is the largest animal that has ever lived on Earth, longer than three buses and heavier than two hundred cows. Its heart is the size of a small car, and its tongue alone weighs as much as an elephant. It feeds on tiny shrimp called krill, eating four tonnes of them every day in summer. Its song is the loudest sound made by any animal, travelling for hundreds of kilometres through the deep ocean."},
    {"title": "The Dolphin",
     "script": "Dolphins are among the most intelligent animals in the sea, with brains nearly as large as ours. They live in social groups called pods and communicate with clicks and whistles, each dolphin having its own signature whistle, like a name. They hunt fish together, herding them into tight balls, and they play for fun, surfing waves and tossing seaweed. A dolphin sleeps with one half of its brain awake, so it never stops swimming."},
    {"title": "The Tiger",
     "script": "The tiger is the largest of all cats, a solitary hunter that rules the forests of Asia. Its orange coat with black stripes is unique to each tiger, like a human fingerprint. A tiger can leap ten metres in a single bound and swim across wide rivers. It hunts mostly at night, using its excellent night vision, and it can eat forty kilograms of meat in one meal. Fewer than four thousand wild tigers remain today."},
    {"title": "The Giraffe",
     "script": "The giraffe is the tallest animal on land, standing nearly six metres high, as tall as a two-storey house. Its long neck lets it eat leaves from the tops of acacia trees that no other animal can reach. A giraffe's heart is enormous, pumping blood all the way up its long neck to its brain. It sleeps for only thirty minutes a day, mostly standing up, and a newborn calf can stand within an hour of being born."},
    {"title": "The Panda",
     "script": "The giant panda lives in the misty bamboo forests of the mountains of China, and it spends fourteen hours a day eating bamboo. A panda eats up to forty kilograms of bamboo daily, sitting upright and holding the stalks with a special thumb bone. Pandas are shy and solitary, and a mother raises her tiny cub, born blind and weighing only a hundred grams, in a den hidden deep in the forest. Fewer than two thousand remain in the wild."},
    {"title": "The Great White Shark",
     "script": "The great white shark is the ocean's most powerful hunter, growing up to six metres long. It can sense a single drop of blood in a hundred litres of water, and its three hundred teeth are arranged in rows that replace themselves throughout its life. It swims with bursts of speed up to fifty kilometres per hour, leaping clean out of the water to catch seals. Yet it is not a mindless killer: it is curious, intelligent, and vital to the ocean."},
    {"title": "The Octopus",
     "script": "The octopus is one of the cleverest creatures in the sea, with three hearts and blue blood. It has no bones at all, so it can squeeze through a gap the size of a coin. Its eight arms are covered in suckers that can taste what they touch. When danger comes, it changes colour in a fraction of a second, hiding in plain sight, and it can spray a cloud of black ink to escape. An octopus can solve puzzles and open jars."},
    {"title": "The Kangaroo",
     "script": "The kangaroo is the symbol of Australia, a marsupial that moves by hopping on powerful hind legs. It can leap nine metres in a single bound and reach speeds of seventy kilometres per hour. A mother carries her baby, called a joey, in a pouch on her belly for eight months, where it drinks milk and grows safe. Kangaroos live in groups called mobs, grazing on grass at dawn and dusk, and they can go for months without drinking water."},
    {"title": "The Sea Turtle",
     "script": "The sea turtle has swum the oceans for more than a hundred million years, since the time of the dinosaurs. A female returns to the very beach where she was born to lay her eggs, guided by the Earth's magnetic field across thousands of kilometres. She digs a nest in the sand and lays a hundred eggs, then swims away. The tiny hatchlings must cross the beach alone, and only one in a thousand survives to adulthood."},
]

HISTORY_TOPICS = [
    {"title": "The Pyramids of Egypt",
     "script": "More than four thousand years ago, the Egyptians built the Great Pyramid of Giza, the tallest structure on Earth for nearly four thousand years. Two million stone blocks, some weighing fifteen tonnes, were hauled into place by tens of thousands of workers. It was built as a tomb for the Pharaoh Khufu, aligned perfectly with the stars. Inside, narrow passages lead to a burial chamber hidden deep in the stone. It remains the last surviving wonder of the ancient world."},
    {"title": "The Roman Empire",
     "script": "Two thousand years ago, Rome ruled the largest empire the ancient world had ever seen, from Britain to Egypt. Roman engineers built roads that still exist today, aqueducts that carried water for a hundred kilometres, and the Colosseum, where fifty thousand people watched gladiators fight. Roman law, language and engineering shaped Europe for centuries. At its height, one in every five people on Earth lived under Roman rule."},
    {"title": "The Vikings",
     "script": "A thousand years ago, the Vikings sailed from Scandinavia in long wooden ships, reaching Iceland, Greenland and even North America five hundred years before Columbus. They were fearsome warriors, but also traders, farmers and explorers. Their ships were so advanced they could cross the open ocean and sail up shallow rivers. Viking sagas tell of gods, giants and heroes, and their runes are still carved on stones across northern Europe."},
    {"title": "The Great Wall of China",
     "script": "The Great Wall of China is the longest structure ever built by humans, stretching more than twenty thousand kilometres across mountains and deserts. It was built over two thousand years by different dynasties to protect China from northern invaders. Millions of workers laboured on it, and the wall is wide enough for five horses to ride side by side. Today it is a symbol of China, and millions of visitors walk its ancient stones every year."},
    {"title": "The Titanic",
     "script": "In 1912, the Titanic was the largest ship ever built, a floating palace with grand staircases, a swimming pool and a ballroom. On its first voyage from England to America, it struck an iceberg in the icy Atlantic at night. The ship was called unsinkable, but it sank in less than three hours, and more than fifteen hundred people lost their lives. The wreck lay undiscovered on the ocean floor for seventy-three years."},
    {"title": "The First Moon Landing",
     "script": "On the twentieth of July, 1969, the whole world watched as Apollo 11 landed on the Moon. Neil Armstrong stepped onto the lunar surface and spoke the most famous words in history: one small step for man, one giant leap for mankind. He and Buzz Aldrin walked on the Moon for two and a half hours, collecting rocks and planting a flag, while Michael Collins waited alone in orbit above them."},
    {"title": "Ancient Greece",
     "script": "Ancient Greece gave the world democracy, the Olympic Games, theatre and philosophy. In Athens, citizens gathered to vote on their own laws, a radical idea that shapes our world today. Greek thinkers like Socrates, Plato and Aristotle asked questions we still debate. The Parthenon, their great temple, still stands on a hill above Athens, and the first Olympic Games were held in Greece nearly three thousand years ago."},
    {"title": "The Samurai",
     "script": "The samurai were the warrior class of Japan for seven hundred years, bound by a strict code of honour called bushido, the way of the warrior. They carried two swords, trained from childhood, and believed that death in battle was more honourable than a life of shame. Their swords were folded a thousand times to make steel of unmatched sharpness. Though the samurai disappeared in the nineteenth century, their code still shapes Japanese culture today."},
    {"title": "The Aztec Empire",
     "script": "Five hundred years ago, the Aztec Empire ruled central Mexico from its great capital, Tenochtitlan, a city built on an island in a lake, larger than London or Paris at the time. The Aztecs built floating gardens, vast temples and a calendar more accurate than Europe's. They traded gold, feathers and chocolate across their empire. In 1519, Spanish conquistadors arrived, and within two years the great empire had fallen."},
    {"title": "The First Flight",
     "script": "In 1903, two brothers from Ohio, Wilbur and Orville Wright, built a fragile flying machine of wood and cloth in their bicycle shop. On a cold December morning at Kitty Hawk, their aeroplane rose into the air and flew for twelve seconds, covering thirty-six metres. It was the first powered flight in history. Within a few decades, aeroplanes would cross oceans, and within sixty-six years, humans would fly to the Moon."},
    {"title": "The Library of Alexandria",
     "script": "Two thousand years ago, the Library of Alexandria in Egypt held the knowledge of the ancient world: hundreds of thousands of scrolls on astronomy, medicine, mathematics and poetry. Scholars from every land came to study there. The library was the greatest centre of learning the world had ever known. It was destroyed by fire, and its loss is still mourned today as one of the greatest tragedies in the history of knowledge."},
    {"title": "The Printing Press",
     "script": "In the fourteen-forties, Johannes Gutenberg invented the printing press in Germany, and it changed the world forever. Before him, every book was copied by hand, slowly and at great cost. With movable type, a single press could print thousands of copies. Within fifty years, millions of books were in circulation, and knowledge was no longer the privilege of the rich. The printing press lit the spark of the modern age."},
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
        return {"animals": list(data.get("animals", [])),
                "history": list(data.get("history", []))}
    except Exception:
        return {"animals": [], "history": []}


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


# Fresh stories generated by scripts/generate_story.py (Gemini) at the
# start of the run. The builders prefer these over the hardcoded lists.
GENERATED_STORIES_FILE = os.path.join(BASE, "data", "generated_stories.json")


def load_generated_stories(mode):
    """Fresh stories generated for this run, or [] if none were made."""
    try:
        with open(GENERATED_STORIES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [s for s in data.get(mode, [])
                if s.get("title") and s.get("script")]
    except Exception:
        return []


def pick_batch_stories(topics, mode, n):
    """Stories for one batch: prefer freshly generated stories (from
    scripts/generate_story.py); fill any remaining slots from the
    hardcoded lists. Cross-batch no-repeat tracking applies to BOTH
    sources (recorded in data/used_stories.json), so no story is ever
    reused until its mode's list is exhausted (then it resets)."""
    used = load_used_stories()
    used_titles = set(used.get(mode, []))
    # 1) fresh generated stories (not used before)
    gen = load_generated_stories(mode)
    fresh_gen = [s for s in gen if s["title"] not in used_titles]
    picked = fresh_gen[:n]
    n_gen = len(picked)
    # 2) fill remaining slots from the hardcoded list (no-repeat)
    if len(picked) < n:
        rest = [t for t in topics
                if t["title"] not in used_titles
                and t["title"] not in [p["title"] for p in picked]]
        if len(rest) < n - len(picked):
            # used list exhausted — reset and start over
            print(f"{mode}: used list exhausted "
                  f"({len(used_titles)}/{len(topics)} used) — resetting",
                  file=sys.stderr)
            used[mode] = []
            used_titles = set()
            rest = [t for t in topics
                    if t["title"] not in [p["title"] for p in picked]]
        picked = picked + pick_story_topics(rest, n - len(picked))
    # 3) record all picked titles as used
    used[mode] = used.get(mode, []) + [t["title"] for t in picked]
    save_used_stories(used)
    print(f"{mode}: {n_gen} generated + {len(picked) - n_gen} hardcoded")
    return picked


# Visual search topics (stock photos/video clips). The robot
# (scripts/prefetch_media.py) collects visuals for these topics; the
# builders use them for the matching story segments.
ANIMALS_VISUAL_TOPIC = "animals"
HISTORY_VISUAL_TOPIC = "history"


def template_story(topic):
    """A built-in topic as a story-like dict (no news article behind it)."""
    return {"title": topic["title"], "desc": topic["script"], "link": "",
            "ts": 0, "template": True}


# English keyword -> English stock-photo/video topic (the channel is fully
# English now: English feeds, English titles, English voice).
TOPICS = [
    # Animals and history first: these are the two story modes now. The
    # builders use a fixed visual topic per mode, but prefetch also
    # fetches visuals for every topic in this list.
    (["lion", "elephant", "wolf", "whale", "dolphin", "tiger", "giraffe", "panda", "shark", "octopus", "kangaroo", "turtle", "animals", "wildlife", "savanna", "ocean"], "animals"),
    (["pyramids", "egypt", "roman", "empire", "vikings", "great wall", "china", "titanic", "moon landing", "greece", "samurai", "aztec", "flight", "library", "printing", "history", "ancient", "castle", "temple"], "history"),
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


def music_track_url(mode):
    """This run's background-music URL, rotating by (run_number + mode
    offset). Offset 0 for "animals", 1 for "history": the Reel and the
    long video in one batch always have different modes, so they always
    get different tracks, and the run number rotates the choice."""
    seed = story_rotation_seed()
    off = 0 if mode == "animals" else 1
    return MUSIC_TRACKS[(seed + off) % len(MUSIC_TRACKS)]


def get_music(mode=None):
    """Path to this run's background-music track, or None. Downloads the
    rotating track to out/assets/music_<n>.mp3 (cached between runs);
    falls back to the committed music.mp3; if that also fails, the build
    proceeds with voice only (never crashes). `mode` defaults to the
    STORY_MODE env var, so the long-video builder (which calls this
    without an argument) gets its own mode's track."""
    if mode is None:
        mode = os.environ.get("STORY_MODE", "animals").strip().lower()
    url = music_track_url(mode)
    idx = MUSIC_TRACKS.index(url)
    path = os.path.join(ASSETS, f"music_{idx}.mp3")
    if os.path.exists(path) and os.path.getsize(path) > 10000:
        return path
    try:
        if download(url, path, min_bytes=10000):
            return path
    except Exception as ex:
        print(f"music download failed: {ex}", file=sys.stderr)
    if os.path.exists(MUSIC_PATH) and os.path.getsize(MUSIC_PATH) > 10000:
        return MUSIC_PATH
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
    BOTH animal and history videos). strict=False: fresh first, then
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
    used for BOTH animal and history videos).
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
    # (en-US-GuyNeural, male), English on-screen text and CTA in both modes.
    # Modes (env STORY_MODE, default "animals"):
    #   animals: built-in animal documentaries (rotating).
    #   history: built-in historical stories (rotating).
    # Both modes are template-only — they never depend on the feeds, so
    # they always yield a complete Reel.
    mode = os.environ.get("STORY_MODE", "animals").strip().lower()
    if mode not in ("animals", "history"):
        print(f"WARNING: unknown STORY_MODE {mode!r}, using animals",
              file=sys.stderr)
        mode = "animals"
    topic_mode = mode
    voice = EN_VOICE
    topics = ANIMAL_TOPICS if mode == "animals" else HISTORY_TOPICS
    stories = [template_story(t)
               for t in pick_batch_stories(topics, mode, N_STORIES)]
    print(f"{mode} mode: {len(stories)} template story(ies)")
    for s in stories:
        s["topic"] = topic_mode

    font = get_font()
    pool = load_pool()
    used = set()  # visuals already used in THIS video, by local path and
    # by "provider:provider_id" key (see gather_visuals) — no clip/photo
    # is ever repeated within one video (animals and history alike).
    # strict = no-repeat applies to BOTH animal and history videos: no clip
    # or photo is repeated within one video (his order 2026-11-17).
    strict = True
    print(f"Media pool: {len(pool)} file(s)")
    print(f"Stories: {len(stories)}")
    segments = []  # dicts (see make_segment)
    for i, st in enumerate(stories):
        # Each mode uses its own fixed visual topic.
        topic = (ANIMALS_VISUAL_TOPIC if mode == "animals"
                 else HISTORY_VISUAL_TOPIC)
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
                    "topic": s.get("topic", "animals"),
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
        music = get_music(mode)
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
