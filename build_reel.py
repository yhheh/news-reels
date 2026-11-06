#!/usr/bin/env python3
"""Auto news Reel builder.
6 Arabic stories, 3 free stock images each, Arabic voice, vertical 9:16, ~90s.
Free tools only. No RSS news-agency photos (copyright) - Openverse stock only.
Output: out/reel.mp4
"""
import calendar
import json
import os
import re
import subprocess
import sys

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

# Arabic keyword -> English stock-photo topic (Openverse search is English)
TOPICS = [
    (["كرة", "مباراة", "دوري", "هدف", "منتخب", "لاعب", "رياض"], "football stadium"),
    (["اقتصاد", "أسعار", "دولار", "ذهب", "بورصة", "أسهم", "نفط"], "economy money"),
    (["طقس", "أمطار", "حرارة", "عاصفة", "ثلوج"], "weather rain"),
    (["صحة", "مرض", "مستشفى", "دواء", "لقاح"], "health hospital"),
    (["تكنولوجيا", "ذكاء اصطناعي", "هاتف", "إنترنت"], "technology computer"),
    (["تعليم", "مدرسة", "جامعة", "طلاب"], "education school"),
    (["حرب", "جيش", "قصف", "عسكري"], "military conflict"),
    (["انتخاب", "رئيس", "حكومة", "برلمان", "وزير"], "politics government"),
    (["فن", "موسيقى", "سينما", "ممثل"], "concert music"),
    (["سياحة", "سفر", "طيران", "فندق"], "travel airplane"),
]


def topic_for(title):
    for words, topic in TOPICS:
        if any(w in title for w in words):
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


def openverse_images(query, n=N_IMG):
    """Free stock photos from Openverse (CC-licensed). Returns list of image URLs."""
    try:
        r = requests.get("https://api.openverse.org/v1/images/",
                         params={"q": query, "page_size": 10, "license_type": "commercial"},
                         timeout=20)
        r.raise_for_status()
        out = []
        for it in r.json().get("results", []):
            u = it.get("foreign_landing_url") and it.get("url")
            if u and u.lower().endswith((".jpg", ".jpeg", ".png")):
                out.append(u)
            if len(out) >= n:
                break
        return out
    except Exception as ex:
        print(f"openverse failed ({query}): {ex}", file=sys.stderr)
        return []


def download(url, path):
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        return os.path.getsize(path) > 5000
    except Exception as ex:
        print(f"download failed {url}: {ex}", file=sys.stderr)
        return False


def get_font():
    if os.path.exists(FONT_PATH) and os.path.getsize(FONT_PATH) > 50000:
        return FONT_PATH
    if download(FONT_URL, FONT_PATH):
        return FONT_PATH
    return None


def render_title_png(text, path, font_path, size=54, max_w=W - 60):
    """Render Arabic title correctly (reshaped + bidi) onto a transparent PNG."""
    from PIL import Image, ImageDraw, ImageFont
    import arabic_reshaper
    from bidi.algorithm import get_display
    font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
    # wrap into lines that fit max_w
    words = text.split()
    lines, cur = [], ""
    tmp = Image.new("RGBA", (10, 10))
    dr = ImageDraw.Draw(tmp)
    for w in words:
        t = (cur + " " + w).strip()
        if dr.textlength(get_display(arabic_reshaper.reshape(t)), font=font) <= max_w:
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
        disp = get_display(arabic_reshaper.reshape(ln))
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


def story_audio_len(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def main():
    with open(os.path.join(BASE, "feeds.json"), encoding="utf-8") as f:
        feeds = json.load(f)
    stories = fetch_stories(feeds)
    if len(stories) < N_STORIES:
        print(f"WARNING: only {len(stories)} stories found", file=sys.stderr)
    if not stories:
        print("ERROR: no stories fetched", file=sys.stderr)
        sys.exit(1)

    font = get_font()
    print(f"Stories: {len(stories)}")
    segments = []  # (image_paths, title_png, audio_path)
    for i, st in enumerate(stories):
        topic = topic_for(st["title"])
        urls = openverse_images(topic)
        if len(urls) < N_IMG:
            urls += openverse_images("news")[: N_IMG - len(urls)]
        imgs = []
        for j, u in enumerate(urls[:N_IMG]):
            p = os.path.join(ASSETS, f"s{i}_img{j}.jpg")
            if download(u, p):
                imgs.append(p)
        # fallback: reuse any image we already have
        while len(imgs) < N_IMG and imgs:
            imgs.append(imgs[0])
        if not imgs:
            print(f"story {i}: no images, skipping", file=sys.stderr)
            continue
        title_png = os.path.join(ASSETS, f"s{i}_title.png")
        render_title_png(st["title"], title_png, font)
        # narration: title + short summary (fits ~12s)
        narr = st["title"] + ". " + st["desc"][:220]
        audio = os.path.join(ASSETS, f"s{i}.mp3")
        ok = make_voice(narr, audio)
        if not ok:
            audio = None
        print(f"story {i}: {st['title'][:60]} | imgs={len(imgs)} | audio={'ok' if audio else 'NO'}")
        segments.append((imgs, title_png, audio))

    # Build one ffmpeg command: each story = 3 images (5s each) + title overlay + voice
    inputs, filters, audio_parts = [], [], []
    idx = 0
    for si, (imgs, title_png, audio) in enumerate(segments):
        for p in imgs:
            inputs += ["-loop", "1", "-t", str(IMG_SEC), "-i", p]
            idx += 1
        inputs += ["-loop", "1", "-t", str(STORY_SEC), "-i", title_png]
        title_idx = idx
        idx += 1
        if audio:
            inputs += ["-i", audio]
            audio_idx = idx
            idx += 1
        else:
            audio_idx = None
        # scale each image to 720x1280
        base = si * N_IMG
        for k in range(N_IMG):
            v = base + k
            filters.append(
                f"[{v}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},setsar=1[v{v}]")
        # concat the 3 scaled images into one 15s segment
        filters.append(
            f"[v{base}][v{base+1}][v{base+2}]concat=n=3:v=1:a=0[seg{si}]")
        filters.append(
            f"[seg{si}][{title_idx}:v]overlay=(W-w)/2:H-h-90[sv{si}]")
        if audio_idx is not None:
            audio_parts.append(f"[{audio_idx}:a]apad=whole_dur={STORY_SEC}[a{si}]")
    nseg = len(segments)
    vcat = "".join(f"[sv{i}]" for i in range(nseg))
    filters.append(f"{vcat}concat=n={nseg}:v=1:a=0[vout]")
    if audio_parts:
        acat = "".join(f"[a{i}]" for i in range(nseg))
        filters.append(f"{acat}concat=n={nseg}:v=0:a=1[aout]")
        amap = ["-map", "[aout]"]
    else:
        amap = []
    out_mp4 = os.path.join(OUT, "reel.mp4")
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
    size = os.path.getsize(out_mp4)
    print(f"DONE: {out_mp4} ({size/1e6:.1f} MB)")
    with open(os.path.join(OUT, "stories.json"), "w", encoding="utf-8") as f:
        json.dump([{"title": s["title"], "link": s["link"]} for s in stories[:nseg]],
                  f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
