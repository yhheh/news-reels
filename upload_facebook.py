#!/usr/bin/env python3
"""Upload a built video to the Facebook Page "Breaking news" (public).
Credentials come from env vars (set as GitHub Secrets):
  FB_PAGE_ID, FB_ACCESS_TOKEN (permanent Page token, never expires)

Usage (defaults = the Reel, unchanged behavior):
  python upload_facebook.py
  python upload_facebook.py --file out/long_video.mp4 \\
      --stories out/long_stories.json --mode long

Title/description come from the stories file (first entry's "topic":
"horror" or "anime"); the title is the first story's title, never a
hardcoded topic string. If the stories file is missing, sensible
English defaults are used.

A failed Facebook upload NEVER fails the workflow: it prints a WARNING
and exits 0, so YouTube publishing (which runs first) always stands.
Facebook videos are uploaded with a simple multipart POST (videos here
are ~10-30 MB, well under the 1 GB simple-upload limit). A vertical
video under ~90 s is shown by Facebook as a Reel automatically.
"""
import argparse
import json
import os
import sys

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
VIDEO = os.path.join(BASE, "out", "reel.mp4")
STORIES = os.path.join(BASE, "out", "stories.json")
GRAPH_VERSION = "v21.0"

# Per (mode, topic): description intro.
META = {
    ("reel", "horror"): "A horror story in one minute.",
    ("reel", "anime"): "An anime story in one minute.",
    ("long", "horror"): "Horror stories in this video.",
    ("long", "anime"): "Anime stories in this video.",
}


def main():
    ap = argparse.ArgumentParser(description="Upload a video to Facebook.")
    ap.add_argument("--file", default=VIDEO,
                    help="video file to upload (default: out/reel.mp4)")
    ap.add_argument("--stories", default=STORIES,
                    help="stories JSON for title/description "
                         "(default: out/stories.json)")
    ap.add_argument("--mode", choices=["reel", "long"], default="reel",
                    help="reel = short vertical, long = regular video "
                         "(default: reel)")
    args = ap.parse_args()

    if not os.path.exists(args.file) or os.path.getsize(args.file) < 10000:
        print(f"WARNING: no video to upload at {args.file} "
              "(skipping Facebook)", file=sys.stderr)
        return 0
    page_id = os.environ.get("FB_PAGE_ID")
    token = os.environ.get("FB_ACCESS_TOKEN")
    if not (page_id and token):
        print("WARNING: missing Facebook env vars "
              "(FB_PAGE_ID / FB_ACCESS_TOKEN) — skipping Facebook",
              file=sys.stderr)
        return 0

    # Title + topic from the stories file (first entry drives both).
    entries = []
    try:
        with open(args.stories, encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        pass
    titles = [s.get("title", "") for s in entries if s.get("title")]
    topic = (entries[0].get("topic") if entries and entries[0].get("topic")
             else "horror")
    if topic not in ("horror", "anime"):
        topic = "horror"
    first = (titles[0][:95] if titles else "Breaking News")
    intro = META[(args.mode, topic)]
    desc_lines = [intro, ""]
    for t in titles:
        desc_lines.append("• " + t)
    desc_lines += ["", "For more stories, visit our website:",
                   "https://breaking-news-en.vercel.app",
                   "https://akhbar-ajila.vercel.app",
                   "", "Breaking news"]
    url = (f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}/videos")
    try:
        with open(args.file, "rb") as f:
            resp = requests.post(
                url,
                data={
                    "title": first[:100],
                    "description": "\n".join(desc_lines)[:4000],
                    "access_token": token,
                },
                files={"file": (os.path.basename(args.file), f,
                                "video/mp4")},
                timeout=600,
            )
        resp.raise_for_status()
        data = resp.json()
        vid = data.get("id")
        if not vid:
            raise RuntimeError(f"no video id in response: {data}")
        print(f"FACEBOOK UPLOADED: https://www.facebook.com/watch/?v={vid}")
        out_name = ("facebook.json" if args.mode == "reel"
                    else "facebook_long.json")
        with open(os.path.join(BASE, "out", out_name), "w",
                  encoding="utf-8") as f:
            json.dump({"video_id": vid,
                       "url": f"https://www.facebook.com/watch/?v={vid}"},
                      f, ensure_ascii=False, indent=2)
        return 0
    except Exception as ex:
        # Never fail the workflow over Facebook: YouTube already ran.
        print(f"WARNING: Facebook upload failed: {ex}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
