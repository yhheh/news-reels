#!/usr/bin/env python3
"""Upload a built video to YouTube (public).
Credentials come from env vars (set as GitHub Secrets):
  YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN

Usage (defaults = the Reel, unchanged behavior):
  python upload_youtube.py
  python upload_youtube.py --file out/long_video.mp4 \\
      --stories out/long_stories.json --mode long

--mode reel: uploads as a Short (title + " #Shorts", shorts URL).
--mode long: uploads as a regular video (plain title, watch URL).
Title/description/tags/category come from the stories file (first entry's
"topic": "news" or "islamic"); the title is the first story's title,
never a hardcoded topic string. If the stories file is missing, sensible
English defaults are used.

After a successful upload, a top-level comment with the website link is
posted on the video (commentThreads.insert). It is NOT pinned — the API
cannot pin comments; pinning is manual in YouTube Studio/app. Posting
the comment needs the youtube.force-ssl OAuth scope in addition to
youtube.upload (see main()); if the stored refresh token lacks that
scope, the comment post fails with a warning and the upload still stands.
"""
import argparse
import json
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE = os.path.dirname(os.path.abspath(__file__))
VIDEO = os.path.join(BASE, "out", "reel.mp4")
STORIES = os.path.join(BASE, "out", "stories.json")

# Per (mode, topic): description intro, tags, category.
# 25 = News & Politics, 27 = Education.
META = {
    ("reel", "news"): ("The most important news in one minute.",
                       ["news", "breaking", "world news", "Shorts"], "25"),
    ("reel", "islamic"): ("An introduction to Islam in one minute.",
                          ["islam", "what is islam", "islamic",
                           "Shorts"], "27"),
    ("long", "news"): ("The most important news in this video.",
                       ["news", "breaking", "world news"], "25"),
    ("long", "islamic"): ("An introduction to Islam.",
                          ["islam", "what is islam", "islamic"], "27"),
}

# Top-level comment posted on every uploaded video (same text for the
# Reel and the long video, both topics). The link in a comment IS
# clickable, so viewers get a working link to the site. The YouTube Data
# API cannot pin a comment programmatically — pinning is manual in
# YouTube Studio/app — so this is posted as a regular top-level comment
# (it is the first comment on the video). Posting a comment needs the
# youtube.force-ssl OAuth scope (youtube.upload alone is not enough);
# see the scopes in main() — the stored refresh token must have been
# granted that scope, otherwise the post fails and a warning is printed.
COMMENT_TEXT = ("For more news, visit our website: "
                "https://breaking-news-en.vercel.app")


def post_comment(yt, video_id):
    """Post COMMENT_TEXT as a top-level comment on video_id.
    Returns the comment thread id on success, None on failure (the
    failure is printed as a warning; the upload itself already
    succeeded, so this never raises)."""
    try:
        resp = yt.commentThreads().insert(
            part="snippet",
            body={"snippet": {
                "videoId": video_id,
                "topLevelComment": {"snippet": {"textOriginal": COMMENT_TEXT}},
            }}).execute()
        cid = (resp.get("id") or "")
        print(f"COMMENT POSTED on {video_id} (thread {cid})")
        return cid
    except Exception as ex:
        print("WARNING: could not post the website comment on "
              f"{video_id}: {ex} "
              "(comment posting needs the youtube.force-ssl OAuth scope — "
              "re-authorize the YouTube OAuth token with that scope)",
              file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(description="Upload a video to YouTube.")
    ap.add_argument("--file", default=VIDEO,
                    help="video file to upload (default: out/reel.mp4)")
    ap.add_argument("--stories", default=STORIES,
                    help="stories JSON for title/description "
                         "(default: out/stories.json)")
    ap.add_argument("--mode", choices=["reel", "long"], default="reel",
                    help="reel = Short, long = regular video (default: reel)")
    args = ap.parse_args()

    if not os.path.exists(args.file) or os.path.getsize(args.file) < 10000:
        print(f"ERROR: no video to upload at {args.file}", file=sys.stderr)
        sys.exit(1)
    cid = os.environ.get("YOUTUBE_CLIENT_ID")
    csec = os.environ.get("YOUTUBE_CLIENT_SECRET")
    rtok = os.environ.get("YOUTUBE_REFRESH_TOKEN")
    if not (cid and csec and rtok):
        print("ERROR: missing YouTube OAuth env vars", file=sys.stderr)
        sys.exit(1)
    creds = Credentials(
        token=None,
        refresh_token=rtok,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cid,
        client_secret=csec,
        # youtube.upload covers the video upload; youtube.force-ssl is
        # needed to post the website comment (commentThreads.insert).
        # NOTE: the stored refresh token must have been granted the
        # force-ssl scope too — if it was only granted youtube.upload,
        # the comment post will fail (see post_comment) until the token
        # is re-authorized with that scope.
        scopes=["https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.force-ssl"],
    )
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)

    # Title + topic from the stories file (first entry drives both).
    entries = []
    try:
        with open(args.stories, encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        pass
    titles = [s.get("title", "") for s in entries if s.get("title")]
    topic = (entries[0].get("topic") if entries and entries[0].get("topic")
             else "news")
    if topic not in ("news", "islamic"):
        topic = "news"
    first = (titles[0][:95] if titles else "Breaking News")
    intro, tags, category = META[(args.mode, topic)]
    if args.mode == "reel":
        title = first + " #Shorts"
    else:
        title = first
    desc_lines = [intro, ""]
    for t in titles:
        desc_lines.append("• " + t)
    desc_lines += ["", "For more news, visit our website:",
                   "https://breaking-news-en.vercel.app",
                   "https://akhbar-ajila.vercel.app",
                   "", "Breaking news24"]
    body = {
        "snippet": {
            "title": title[:100],
            "description": "\n".join(desc_lines)[:4000],
            "tags": tags,
            "categoryId": category,
        },
        "status": {"privacyStatus": "public"},
    }
    media = MediaFileUpload(args.file, mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            print(f"upload {int(status.progress() * 100)}%")
    vid = resp.get("id")
    if args.mode == "reel":
        url = f"https://www.youtube.com/shorts/{vid}"
        out_json = os.path.join(BASE, "out", "youtube.json")
    else:
        url = f"https://www.youtube.com/watch?v={vid}"
        out_json = os.path.join(BASE, "out", "youtube_long.json")
    print(f"UPLOADED: {url}")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"video_id": vid, "url": url}, f, ensure_ascii=False, indent=2)
    # Website link as a top-level comment (clickable) — same for the Reel
    # and the long video. A failed comment never fails the upload.
    post_comment(yt, vid)


if __name__ == "__main__":
    main()
