#!/usr/bin/env python3
"""Long-form video builder (FULLY ENGLISH channel).
Horizontal 16:9, 1280x720, 30fps. Up to 6 stories, 30s each, with a brief
4s presenter interstitial between stories (the presenter's own silent
video, assets/presenter.mp4 — the next story's narration starts over the
presenter, then the story visuals roll) + a 30s end CTA. Total ≈230s.
English titles, English voice (en-US-AriaNeural), English CTA.
Modes (env LONG_TOPIC, default "smart-glasses"):
  smart-glasses: real smart-glasses news first, then built-in English
    template topics (rotating, generic/educational only) fill the remaining
    slots — the mode never depends on the feeds.
  news: the newest English news stories, real articles only.
Background music: "Serene View" by Arulo (Mixkit, royalty-free — see
assets/MUSIC.md) is mixed under the narration at volume 0.15; if the file
is missing the build still succeeds, voice only.
Visuals per story (same fallback order as the Reels): pool clips/photos
first (data/media_pool/, see build_reel.gather_visuals), then Pexels video
clips (landscape) -> Pexels photos -> Unsplash -> Openverse -> title card.
Audio: smart-glasses template narration is a fixed text, so the pool may
already hold it (keyed by script hash — see build_reel.pool_audio); news
narration is fresh every run and is always generated at build time.
Free stock only, no RSS/agency visuals.
Outputs: out/long_video.mp4 + out/long_stories.json
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_reel as br

# Landscape geometry for this builder (build_reel's W/H are the vertical
# Reel's). The shared helpers read the module globals at call time, so set
# them right after import — before any card/title/visual call.
br.W, br.H = 1280, 720

LW, LH = 1280, 720
L_STORY_SEC = 30            # seconds per story
L_VIS_SEC = 10              # seconds per visual (3 visuals x 10s = 30s)
L_N_VIS = 3
L_N_STORIES = 6
EN_VOICE = "en-US-AriaNeural"
SMART_TOPIC = "smart glasses augmented reality"


def fetch_long_stories(feeds, mode):
    """Stories for the long video (smart-glasses mode only; news mode uses
    br.fetch_stories directly). Real stories first, templates fill the rest."""
    try:
        pool = br.fetch_stories(feeds, limit=60)
    except Exception as ex:
        print(f"feed fetch failed ({ex}) — building from templates only",
              file=sys.stderr)
        pool = []
    real = [s for s in pool if br.is_smart_glasses(s)][:L_N_STORIES]
    need = L_N_STORIES - len(real)
    templates = ([br.template_story(t) for t in br.pick_smart_topics(need)]
                 if need > 0 else [])
    print(f"Smart-glasses mode: {len(real)} real story(ies) + "
          f"{len(templates)} template topic(s)")
    return real + templates


def story_visuals(st, i, font, topic, pool, used):
    """Up to 3 visuals for one story: the shared pool-first chain
    (build_reel.gather_visuals) — pooled clips/photos, then live Pexels
    videos (landscape), then live photos, then title card.
    Returns [(path, "video"|"image")], never empty."""
    return br.gather_visuals(pool, topic, f"l{i}", L_N_VIS, "landscape",
                              used, font, st["title"])


def assemble_long(segments, out_mp4):
    """Build one 16:9 mp4 from segments (dicts — see br.make_segment).
    Each story = 3 visuals (10s each; video clips loop to fill, photos loop)
    + title overlay + voice; presenter interstitials = 1 visual (4s, no
    title) with the next story's narration starting over it. The vertical
    presenter clip is adapted to 16:9 by the slice step (blurred-pad bake —
    see br.presenter_slice), so here it is just a normal 1280x720 visual.
    All visuals are normalized to 1280x720 @ 30fps so the concat works with
    mixed video/photo inputs. Segment durations vary (30s stories, 4s
    interstitials, 30s CTA); each visual fills dur/len(visuals); each
    audio leg is padded to its segment length."""
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
                # real clip: loop to fill vis_dur, drop its own audio (voiceover covers it)
                inputs += ["-stream_loop", "-1", "-t", str(vis_dur), "-an", "-i", p]
            else:
                inputs += ["-loop", "1", "-t", str(vis_dur), "-i", p]
            vis_indices.append(idx)
            idx += 1
        # normalize every visual to 1280x720 @ 30fps (use the REAL input
        # indices: title and audio inputs sit between stories, so visuals
        # are NOT contiguous). fps=30 normalizes mixed video/photo rates.
        for v in vis_indices:
            filters.append(
                f"[{v}:v]scale={LW}:{LH}:force_original_aspect_ratio=increase,"
                f"crop={LW}:{LH},setsar=1,fps=30[v{v}]")
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
                f"[{seg_label}][{title_idx}:v]overlay=(W-w)/2:H-h-60[sv{si}]")
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
        # Background music under the voice (looped, low volume) — shared
        # helper from build_reel. If no music file is available the build
        # still succeeds, voice only.
        music = br.get_music()
        if music:
            inputs += ["-stream_loop", "-1", "-i", music]
            music_idx = idx
            idx += 1
            filters += br.music_mix_filters(music_idx, total_dur)
            amap = ["-map", "[aout2]"]
            print(f"Background music: {os.path.basename(music)} "
                  f"at volume {br.MUSIC_VOL}")
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


def main():
    mode = os.environ.get("LONG_TOPIC", "smart-glasses").strip().lower()
    if mode not in ("smart-glasses", "news"):
        print(f"WARNING: unknown LONG_TOPIC {mode!r}, using smart-glasses",
              file=sys.stderr)
        mode = "smart-glasses"
    print(f"Long video mode: {mode}")
    with open(os.path.join(br.BASE, "feeds.json"), encoding="utf-8") as f:
        feeds = json.load(f)
    if mode == "news":
        stories = br.fetch_stories(feeds)
        if len(stories) < L_N_STORIES:
            print(f"WARNING: only {len(stories)} stories found", file=sys.stderr)
        if not stories:
            print("ERROR: no stories fetched", file=sys.stderr)
            sys.exit(1)
    else:
        stories = fetch_long_stories(feeds, mode)
    for s in stories:
        s["topic"] = mode

    font = br.get_font()
    pool = br.load_pool()
    used = set()  # pool files already used in this run (avoid repeats)
    print(f"Media pool: {len(pool)} file(s)")
    print(f"Stories: {len(stories)}")
    segments = []  # dicts (see br.make_segment)
    for i, st in enumerate(stories):
        # Smart-glasses mode always uses smart-glasses visuals; news mode
        # maps the title to its topic (English keywords).
        topic = SMART_TOPIC if mode == "smart-glasses" else br.topic_for(st["title"])
        visuals = story_visuals(st, i, font, topic, pool, used)
        title_png = os.path.join(br.ASSETS, f"l{i}_title.png")
        br.render_title_png(st["title"], title_png, font, size=64)
        # narration: title + longer summary (~30s). Template topics use
        # their script, truncated at a word boundary. Template narration is
        # a fixed text, so the pool may already hold its audio (keyed by
        # script hash); news narration is fresh every run and is always
        # generated here.
        audio_src = "gen"
        if st.get("template"):
            narr = br.template_narration(st, 400)
            pooled = br.pool_audio(narr, EN_VOICE)
            audio = os.path.join(br.ASSETS, f"l{i}.mp3")
            if pooled:
                audio, audio_src = pooled, "pool"
            elif not br.make_voice(narr, audio, voice=EN_VOICE):
                audio = None
        else:
            narr = st["title"] + ". " + st["desc"][:400]
            audio = os.path.join(br.ASSETS, f"l{i}.mp3")
            if not br.make_voice(narr, audio, voice=EN_VOICE):
                audio = None
        n_vid = sum(1 for _, k in visuals if k == "video")
        print(f"story {i}: {st['title'][:60]} | visuals={len(visuals)} "
              f"(video={n_vid}) | audio={audio_src if audio else 'NO'}")
        # Presenter interstitial BEFORE this story (except the first one):
        # a brief slice of the presenter clip, visual only — this story's
        # narration starts over it, so the story segment below skips those
        # seconds (a_start=br.PRESENTER_SEC). The vertical clip is adapted
        # to 16:9 by the slice step (blurred-pad bake). If the presenter
        # clip is unavailable, no interstitial and no skip.
        story_skip = 0.0
        if i > 0:
            inter = br.presenter_segment(i - 1, "l", LW, LH, audio)
            if inter is not None:
                segments.append(inter)
                story_skip = br.PRESENTER_SEC if audio else 0.0
        segments.append(br.make_segment(visuals, title_png, audio,
                                        L_STORY_SEC, a_start=story_skip))

    # End CTA: same site-visit call as the Reels (English channel, 30s here).
    cta_card = os.path.join(br.ASSETS, "lcta_card.jpg")
    br.make_card_image("breaking-news-en.vercel.app", cta_card, font)
    cta_title = os.path.join(br.ASSETS, "lcta_title.png")
    br.render_title_png("For more news, visit our website", cta_title, font)
    cta_audio = os.path.join(br.ASSETS, "lcta.mp3")
    pooled_cta = br.pool_audio(br.CTA_NARR, EN_VOICE)
    if pooled_cta:
        cta_audio = pooled_cta
        print("CTA audio: pool")
    elif not br.make_voice(br.CTA_NARR, cta_audio, voice=EN_VOICE):
        cta_audio = None
    segments.append(br.make_segment([(cta_card, "image")] * L_N_VIS,
                                    cta_title, cta_audio, L_STORY_SEC))

    if not segments:
        print("ERROR: no story segments to render", file=sys.stderr)
        sys.exit(1)

    out_mp4 = os.path.join(br.OUT, "long_video.mp4")
    assemble_long(segments, out_mp4)
    size = os.path.getsize(out_mp4)
    print(f"DONE: {out_mp4} ({size/1e6:.1f} MB)")
    with open(os.path.join(br.OUT, "long_stories.json"), "w", encoding="utf-8") as f:
        json.dump([{"title": s["title"], "link": s["link"],
                    "topic": s.get("topic", mode),
                    "template": bool(s.get("template"))} for s in stories],
                  f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
