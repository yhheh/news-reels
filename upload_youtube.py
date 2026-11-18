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
"topic": "horror" or "anime"); the title is the first story's title,
never a hardcoded topic string. If the stories file is missing, sensible
English defaults are used.

After a successful upload, a custom thumbnail is set from a frame of
the video (thumbnails.set, ~50 units) and a top-level comment with the
website link is posted on the video (commentThreads.insert). The
thumbnail is NOT pinned — the API cannot pin comments; pinning is manual
in YouTube Studio/app. The comment uses its OWN credentials with the
youtube.force-ssl scope (see post_comment); if the stored refresh token
lacks that scope, the comment post fails with a warning and the upload
still stands. A failed thumbnail likewise never fails the upload
(see set_thumbnail).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE = os.path.dirname(os.path.abspath(__file__))
VIDEO = os.path.join(BASE, "out", "reel.mp4")
STORIES = os.path.join(BASE, "out", "stories.json")

# Per (mode, topic): description intro, tags, category.
# 24 = Entertainment, 1 = Film & Animation.
META = {
    ("reel", "horror"): ("A horror story in one minute.",
                         ["horror", "scary story", "horror stories",
                          "Shorts"], "24"),
    ("reel", "anime"): ("An anime story in one minute.",
                        ["anime", "anime story", "anime stories",
                         "Shorts"], "1"),
    ("long", "horror"): ("Horror stories in this video.",
                         ["horror", "scary story", "horror stories"], "24"),
    ("long", "anime"): ("Anime stories in this video.",
                        ["anime", "anime story", "anime stories"], "1"),
}

# Top-level comment posted on every uploaded video (same text for the
# Reel and the long video, both topics). The links in a comment ARE
# clickable, so viewers get working links to both sites. The YouTube Data
# API cannot pin a comment programmatically — pinning is manual in
# YouTube Studio/app — so this is posted as a regular top-level comment
# (it is the first comment on the video). Posting a comment needs the
# youtube.force-ssl OAuth scope; post_comment() builds its OWN
# credentials with that scope, so a stored token that lacks it fails
# there (warning only) and never breaks the upload.
COMMENT_TEXT = ("For more stories, visit our websites:\n"
                "https://breaking-news-en.vercel.app\n"
                "https://akhbar-ajila.vercel.app")


def set_thumbnail(yt, vid, video_file):
    """Set a custom thumbnail on video_id from a frame of the video.
    Extracts one frame at ~2s with ffmpeg and uploads it via
    thumbnails.set (~50 units from the 10,000-unit pool). Uses the same
    `yt` client as the upload (youtube.upload scope). Never raises: any
    failure prints a warning and returns None, so a thumbnail problem
    can never break the upload. Returns vid on success."""
    thumb = None
    try:
        fd, thumb = tempfile.mkstemp(suffix=".jpg", prefix="thumb_")
        os.close(fd)
        r = subprocess.run(["ffmpeg", "-y", "-ss", "2", "-i", video_file,
                            "-vframes", "1", "-q:v", "2", thumb],
                           capture_output=True, text=True, timeout=60)
        if (r.returncode != 0 or not os.path.exists(thumb)
                or os.path.getsize(thumb) < 1000):
            raise RuntimeError(
                f"ffmpeg frame extraction failed: {r.stderr[-300:]}")
        yt.thumbnails().set(
            videoId=vid,
            media_body=MediaFileUpload(thumb, mimetype="image/jpeg")).execute()
        print(f"THUMBNAIL SET on {vid}")
        return vid
    except Exception as ex:
        print(f"WARNING: could not set thumbnail on {vid}: {ex}",
              file=sys.stderr)
        return None
    finally:
        if thumb:
            try:
                os.remove(thumb)
            except OSError:
                pass


def post_comment(cid, csec, rtok, video_id):
    """Post COMMENT_TEXT as a top-level comment on video_id.
    Builds its OWN credentials with the youtube.force-ssl scope only,
    so a stored token that lacks that scope fails HERE (warning) and
    never breaks the video upload. Returns the comment thread id on
    success, None on failure (never raises)."""
    try:
        creds = Credentials(
            token=None,
            refresh_token=rtok,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cid,
            client_secret=csec,
            scopes=["https://www.googleapis.com/auth/youtube.force-ssl"],
        )
        creds.refresh(Request())
        yt = build("youtube", "v3", credentials=creds)
        resp = yt.commentThreads().insert(
            part="snippet",
            body={"snippet": {
                "videoId": video_id,
                "topLevelComment": {"snippet": {"textOriginal": COMMENT_TEXT}},
            }}).execute()
        thread = (resp.get("id") or "")
        print(f"COMMENT POSTED on {video_id} (thread {thread})")
        return thread
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
        # Upload only: the stored refresh token was granted
        # youtube.upload. The comment uses its OWN credentials with the
        # force-ssl scope (see post_comment) so a missing comment scope
        # can never break the upload.
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
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
             else "horror")
    if topic not in ("horror", "anime"):
        topic = "horror"
    first = (titles[0][:95] if titles else "Breaking News")
    intro, tags, category = META[(args.mode, topic)]
    if args.mode == "reel":
        title = first + " #Shorts"
    else:
        title = first
    desc_lines = [intro, ""]
    for t in titles:
        desc_lines.append("• " + t)
    desc_lines += ["", "For more stories, visit our website:",
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
    # Custom thumbnail from a frame of the video (same client/scope as
    # the upload). A failed thumbnail never fails the upload.
    set_thumbnail(yt, vid, args.file)
    # Website link as a top-level comment (clickable) — same for the Reel
    # and the long video. A failed comment never fails the upload.
    post_comment(cid, csec, rtok, vid)


if __name__ == "__main__":
    main()
