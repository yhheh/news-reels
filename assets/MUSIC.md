# Background music

## Track

- **File:** `music.mp3` (3.5 MB, MP3 256 kbps, 44.1 kHz stereo, 1:54)
- **Title:** "Serene View"
- **Artist:** Arulo
- **Source:** Mixkit free stock music — https://mixkit.co/free-stock-music/
- **Direct download URL (also used as the build-time fallback):**
  https://assets.mixkit.co/music/443/443.mp3
- **Mood:** Chillout / Atmospheric / Relaxed — calm, no vocals, no sudden
  beat drops; sits quietly under a news voiceover.

## License

**Mixkit Stock Music Free License** (https://mixkit.co/license/):

- Free for commercial and personal use, including YouTube videos.
- **No attribution required** (credit is still given here and in the
  video description as good practice).
- No Content ID claims on YouTube (verified by independent testing).
- Must NOT be resold as-is, claimed as your own, or registered on any
  rights-management / Content ID service.

The file is bundled in this repo only as a build asset for these videos;
it is not distributed as a standalone music file or pack.

## How it is used

- Mixed UNDER the narration in both builders (`build_reel.py`,
  `build_long_video.py`) at volume **0.15** via ffmpeg
  `volume` + `amix` (`normalize=0`, so the voice stays at full volume).
- The track is looped to the full video length, then trimmed to it.
- If `assets/music.mp3` is missing at build time, the builder tries once
  to download it from the direct URL above; if that also fails, the build
  still succeeds with voice only (never crashes).
