#!/usr/bin/env python3
"""Media-pool + music mock tests (VM only, run with the project venv).

Modes (env MOCK_MODE):
  reel   — smart-glasses Reel, zero news stories (fetch mocked to []),
           live visual APIs mocked to []: only the pool (+ title cards)
           can supply visuals; pooled template audio must be picked up.
  long   — same, for the 16:9 long video (LONG_TOPIC=smart-glasses).
  news   — news-mode Reel from 2 fake English stories, pool emptied:
           verifies the no-pool fallback (title cards) + English titles.
  all    — runs reel + long + news as subprocesses, then the
           changed-script check (exact narration -> pooled audio;
           one-word-changed narration -> None, i.e. regeneration).

Run: ./venv/bin/python tests/test_pool_mock.py [all|reel|long|news]
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import mock_pool
import build_reel as br


def run_reel():
    os.environ["SMART_GLASSES_ONLY"] = "1"
    backup = mock_pool.setup_mock_pool()
    mock_pool.patch_no_live_apis(br)
    br.fetch_stories = lambda feeds, limit=6: []  # zero news stories
    try:
        br.main()
    finally:
        mock_pool.teardown_mock_pool(backup)
    print("REEL MOCK DONE")


def run_long():
    import build_long_video as blv
    os.environ["LONG_TOPIC"] = "smart-glasses"
    backup = mock_pool.setup_mock_pool()
    mock_pool.patch_no_live_apis(br)
    br.fetch_stories = lambda feeds, limit=6: []  # zero news stories
    try:
        blv.main()
    finally:
        mock_pool.teardown_mock_pool(backup)
    print("LONG MOCK DONE")


def run_news():
    os.environ.pop("SMART_GLASSES_ONLY", None)
    fake = [
        {"title": "Global Markets Rally as Gold Hits Record High",
         "desc": "Stock markets around the world rose today as investors welcomed strong economic data and gold reached a new record price.",
         "link": "https://example.com/markets", "ts": 9999999999},
        {"title": "New Electric Car Breaks Range Record",
         "desc": "A new electric car has broken the driving range record, travelling further on a single charge than any production car before it.",
         "link": "https://example.com/ev", "ts": 9999999998},
    ]
    br.fetch_stories = lambda feeds, limit=6: fake
    mock_pool.patch_no_live_apis(br)
    # empty pool: builders must fall back to title cards
    backup = None
    if os.path.exists(br.POOL_INDEX):
        with open(br.POOL_INDEX, encoding="utf-8") as f:
            backup = f.read()
        os.remove(br.POOL_INDEX)
    try:
        br.main()
    finally:
        if backup is not None:
            with open(br.POOL_INDEX, "w", encoding="utf-8") as f:
                f.write(backup)
    with open(os.path.join(br.OUT, "stories.json"), encoding="utf-8") as f:
        used = json.load(f)
    print("NEWS MOCK titles:", [s["title"] for s in used])
    print("NEWS MOCK DONE")


def changed_script_check():
    """Exact narration -> pooled audio file; one word changed -> None."""
    t = br.SMART_TOPICS[0]
    st = br.template_story(t)
    narr_exact = br.template_narration(st, 220)
    narr_changed = narr_exact + " Extra sentence here."
    got_exact = br.pool_audio(narr_exact, br.EN_VOICE)
    got_changed = br.pool_audio(narr_changed, br.EN_VOICE)
    print(f"changed-script check: exact -> {got_exact}")
    print(f"changed-script check: modified -> {got_changed}")
    assert got_exact, "exact narration should hit the pooled audio"
    assert got_changed is None, "changed narration must NOT reuse pooled audio"
    print("CHANGED-SCRIPT CHECK PASSED")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode == "reel":
        run_reel()
    elif mode == "long":
        run_long()
    elif mode == "news":
        run_news()
    elif mode == "all":
        for m in ("reel", "long", "news"):
            r = subprocess.run([sys.executable, __file__, m],
                               capture_output=True, text=True, timeout=1200)
            sys.stdout.write(r.stdout)
            sys.stderr.write(r.stderr)
            if r.returncode != 0:
                print(f"MODE {m} FAILED (exit {r.returncode})")
                sys.exit(r.returncode)
        changed_script_check()
        print("ALL MOCK MODES PASSED")
    else:
        print(f"unknown mode {mode!r}")
        sys.exit(2)
