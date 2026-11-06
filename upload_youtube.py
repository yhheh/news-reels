#!/usr/bin/env python3
"""Upload the built Reel (out/reel.mp4) to YouTube as a Short (public).
Credentials come from env vars (set as GitHub Secrets):
  YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN
"""
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


def main():
    if not os.path.exists(VIDEO) or os.path.getsize(VIDEO) < 10000:
        print("ERROR: no reel.mp4 to upload", file=sys.stderr)
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
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    yt = build("youtube", "v3", credentials=creds)

    # Title: first story title (short) + #Shorts; description lists all stories.
    titles = []
    try:
        with open(STORIES, encoding="utf-8") as f:
            titles = [s.get("title", "") for s in json.load(f)]
    except Exception:
        pass
    first = (titles[0][:80] if titles else "أخبار عاجلة")
    desc_lines = ["أهم الأخبار في دقيقة واحدة.", ""]
    for t in titles:
        if t:
            desc_lines.append("• " + t)
    desc_lines += ["", "أخبار عاجلة — Breaking news24"]
    body = {
        "snippet": {
            "title": first + " #Shorts",
            "description": "\n".join(desc_lines)[:4000],
            "tags": ["أخبار", "عاجل", "Shorts", "news", "breaking"],
            "categoryId": "25",  # News & Politics
        },
        "status": {"privacyStatus": "public"},
    }
    media = MediaFileUpload(VIDEO, mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            print(f"upload {int(status.progress() * 100)}%")
    vid = resp.get("id")
    print(f"UPLOADED: https://www.youtube.com/shorts/{vid}")
    with open(os.path.join(BASE, "out", "youtube.json"), "w", encoding="utf-8") as f:
        json.dump({"video_id": vid, "url": f"https://www.youtube.com/shorts/{vid}"},
                  f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
