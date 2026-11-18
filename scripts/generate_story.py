#!/usr/bin/env python3
"""Generate fresh animal + history stories with the Gemini API (free tier).
Runs at the start of each workflow run, BEFORE the builders, so every
batch gets stories that were written just now — never repeats from a
hardcoded list.

Reads GEMINI_API_KEY from the environment (GitHub Secret). Generates 6
animals + 6 history stories (one batch worth) and writes them to
data/generated_stories.json: {"animals": [...], "history": [...]} — each a
{title, script} dict, script ~70-90 words, English, factual documentary
narration about a real animal or a real historical topic.

The builders prefer these generated stories over the hardcoded lists
(see build_reel.pick_batch_stories). If the key is missing
or generation fails, this script exits 0 WITHOUT writing the file, and
the builders fall back to the hardcoded lists — a story-generation
failure never breaks the run.
"""
import json
import os
import sys

import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(BASE, "data", "generated_stories.json")
MODEL = "gemini-2.0-flash"
N_PER_MODE = 6
MAX_RETRIES = 3

PROMPTS = {
    "animals": (
        "Write one animal documentary segment in English about ONE real "
        "animal. Return ONLY a JSON object with two keys: \"title\" (the "
        "animal's name, e.g. \"The Red Panda\", 2-5 words) and \"script\" "
        "(the documentary narration, 70-90 words, about how this animal "
        "lives: its habitat, what it eats, how it hunts or survives, and "
        "one surprising fact). Use only true, well-known facts — never "
        "invent facts, numbers, or behaviours. Choose an animal that is "
        "visually distinctive and well known, so stock photos and videos "
        "of it exist. Write in a clear, vivid documentary style suitable "
        "for voice narration."
    ),
    "history": (
        "Write one historical documentary segment in English about ONE real "
        "historical topic, event, or place. Return ONLY a JSON object with "
        "two keys: \"title\" (the topic, e.g. \"The Colosseum\", 2-7 words) "
        "and \"script\" (the documentary narration, 70-90 words, telling "
        "what it was, when it happened, and why it matters). Use only "
        "true, well-known facts — never invent dates, numbers, or quotes. "
        "Choose a topic with famous visible landmarks or scenes, so stock "
        "photos and videos of it exist. Write in a clear, vivid "
        "documentary style suitable for voice narration."
    ),
}


def generate_one(mode, api_key):
    """Generate one story for `mode` via the Gemini API. Returns a
    {title, script} dict, or None on any failure (never raises)."""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODEL}:generateContent?key={api_key}")
    body = {
        "contents": [{"parts": [{"text": PROMPTS[mode]}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 1.0,
            "maxOutputTokens": 600,
        },
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            text = (data["candidates"][0]["content"]["parts"][0]["text"])
            story = json.loads(text)
            title = str(story.get("title", "")).strip()
            script = str(story.get("script", "")).strip()
            words = len(script.split())
            if not title or words < 40 or words > 130:
                raise ValueError(
                    f"bad story (title={title!r}, words={words})")
            print(f"  + {mode}: {title} ({words} words)")
            return {"title": title, "script": script}
        except Exception as ex:
            print(f"  {mode} attempt {attempt} failed: {ex}",
                  file=sys.stderr)
    return None


def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("WARNING: no GEMINI_API_KEY — skipping story generation "
              "(builders will use the hardcoded lists)", file=sys.stderr)
        return 0
    result = {"animals": [], "history": []}
    for mode in ("animals", "history"):
        print(f"Generating {N_PER_MODE} {mode} stories ...")
        for _ in range(N_PER_MODE):
            story = generate_one(mode, api_key)
            if story:
                # avoid a duplicate title within this batch
                if story["title"] not in [s["title"]
                                          for s in result[mode]]:
                    result[mode].append(story)
        print(f"{mode}: {len(result[mode])}/{N_PER_MODE} generated")
    if not result["animals"] and not result["history"]:
        print("WARNING: no stories generated — builders will use the "
              "hardcoded lists", file=sys.stderr)
        return 0
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"WROTE: {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
