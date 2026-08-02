import os
import sys
import time
import concurrent.futures

# Windows consoles/pipes default Python's stdout to the legacy locale codepage
# (e.g. cp1252), which can't represent Hangul - printing a Korean song title
# would otherwise crash the whole run with UnicodeEncodeError.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from render.overlay import load_style
from render.encoding import (
    detect_hw_encoder, download_song, encode_song, encode_transition, concatenate_clips, _log,
    cached_clip,
)
from render.heatmap import pick_clip
from render.chart_state import (
    DATA_FILE, load_history, songs_from_search, pre_fetch_all,
    determine_badges, save_run_state, load_songs, clean_csv_titles,
)

_DEFAULT_START = "00:01:00"
_DEFAULT_END   = "00:01:15"

# Use YouTube Data API if key is present, otherwise fall back to yt-dlp.
_YT_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
if _YT_API_KEY:
    from shared.youtube_api import batch_fetch_metadata, search_kpop as _search_fn
    print("[backend] YouTube Data API v3")
else:
    from shared.metadata import batch_fetch_metadata
    from shared.search import search_kpop as _search_fn
    print("[backend] yt-dlp (no YOUTUBE_API_KEY set)")

CLIPS_DIR  = "assets/clips"
STYLE_FILE = "assets/templates/style.json"

_BADGE_LABELS = {
    "new_entry":        "NEW ENTRY",
    "re_entry":         "RE-ENTRY",
    "highest_increase": "HIGHEST INCREASE",
    "highest_jump":     "HIGHEST JUMP",
    "":                 "",
}


def _fmt_secs(s):
    m, s = divmod(s, 60)
    return f"{int(m)}m {s:.1f}s" if m else f"{s:.1f}s"


def _avg(times):
    return sum(times) / len(times) if times else 0.0


def run_pipeline(search=None, limit=None, no_filter=False,
                  download_workers=6, encode_workers=3, clean_titles=False,
                  chart_name=None):
    """
    K-pop song ranking video generator. Programmatic entry point - the CLI
    (run.py) parses args and calls this directly.
    """
    if clean_titles:
        clean_csv_titles(DATA_FILE)
        return

    style = load_style(STYLE_FILE)

    if chart_name:
        from charts import compute_chart
        from shared.title_cleaner import clean_titles as clean_titles_fn
        csv_path = os.path.join("data", "charts", f"{chart_name}.csv")
        os.makedirs(os.path.dirname(csv_path), exist_ok=True)
        print(f'Computing chart "{chart_name}"...\n')
        results = compute_chart(chart_name, limit=limit)
        if not results:
            print("Chart produced no results (registry empty for this genre - run `sync` first?). Exiting.")
            return
        history = load_history(csv_path)
        songs   = songs_from_search(results, history)

        # A song already on this chart's CSV keeps its previously-cleaned
        # title/artist rather than being re-sent to Gemini from its raw
        # source title every run - compute_chart always returns the full
        # chart (unlike search's small fresh-results set), so without this
        # every run would re-clean the same ~200 songs it cleaned last time.
        cleaned = {
            row["url"]: (row["title"], row["artist"])
            for row in load_songs(csv_path)
        } if os.path.exists(csv_path) else {}

        new_songs = []
        for song in songs:
            cached = cleaned.get(song["url"])
            if cached:
                song["title"], song["artist"] = cached
            else:
                new_songs.append(song)

        if new_songs:
            print(f"Cleaning up titles via AI for {len(new_songs)} new song(s)...")
            clean_titles_fn(new_songs)
        else:
            print("No new songs on this chart - reusing already-cleaned titles.\n")
    elif search:
        csv_path = DATA_FILE
        from shared.title_cleaner import clean_titles as clean_titles_fn
        search_limit = limit or 20
        print(f'Searching YouTube: "{search}" (fetching top {search_limit})...\n')
        results = _search_fn(search, limit=search_limit, filter_mv=not no_filter)
        if not results:
            print("No search results returned. Exiting.")
            return
        history = load_history(csv_path)
        songs   = songs_from_search(results, history)
        print("Cleaning up titles via AI...")
        songs = clean_titles_fn(songs)

        # Merge in any CSV songs that weren't returned by the search
        if os.path.exists(csv_path):
            csv_songs   = load_songs(csv_path)
            search_urls = {s["url"] for s in songs}
            extra       = [s for s in csv_songs if s["url"] not in search_urls]
            if extra:
                print(f"  Merging {len(extra)} existing CSV song(s) into ranking.\n")
                songs = songs + extra
    else:
        csv_path = DATA_FILE
        songs = load_songs(csv_path)
        if limit:
            songs.sort(key=lambda s: int(s.get("rank") or 9999))
            songs = songs[:limit]
            print(f"  Using top {limit} songs from CSV.\n")

    # Segments, transitions, and the final compilation for this run all land
    # in their own subfolder, named after the CSV they're tied to (a chart's
    # own data/charts/<name>.csv, or data/songs.csv for csv/search runs) -
    # so different charts' encoded output never mixes in one flat directory.
    # Raw clips are the one exception: they live in encoding.RAW_CACHE_DIR,
    # shared across every run, since the same video is the same clip
    # regardless of which chart it's being rendered for.
    run_slug = os.path.splitext(os.path.basename(csv_path))[0]
    clips_dir = os.path.join(CLIPS_DIR, run_slug)
    os.makedirs(clips_dir, exist_ok=True)

    ranked = pre_fetch_all(songs, batch_fetch_metadata)
    determine_badges(ranked)

    print("Rankings by view count:")
    for s in ranked:
        gained     = s["_views_gained"]
        change     = s["_rank_change"]
        badge      = _BADGE_LABELS.get(s["_entry_type"], "")
        gained_str = f"  (+{gained:,} gained)" if gained is not None else ""
        change_str = f" {change}" if change else " (new)"
        badge_str  = f"  [{badge}]" if badge else ""
        print(f"  Rank {s['rank']}{change_str}: {s['title']} - {s['_meta']['views']:,} views{gained_str}{badge_str}")
    print()

    countdown = list(reversed(ranked))
    n = len(countdown)
    rank_to_index = {s["rank"]: i for i, s in enumerate(countdown)}
    t_pipeline_start = time.monotonic()

    codec = detect_hw_encoder()
    if codec:
        print(f"[encoder] Hardware acceleration: {codec} (falls back to CPU per-clip on failure)\n")
    else:
        print("[encoder] No GPU encoder detected in this ffmpeg build - using CPU (libx264)\n")

    transition_cfg      = style.get("transition", {})
    video_transition     = transition_cfg.get("video_type", "fade")
    transition_duration  = transition_cfg.get("duration", 1.0)
    cw  = style.get("canvas", {}).get("width", 1920)
    ch  = style.get("canvas", {}).get("height", 1080)
    fps = style.get("canvas", {}).get("fps", 30)

    def _download(song):
        t0 = time.monotonic()
        start = song.get("start") or _DEFAULT_START
        end   = song.get("end")   or _DEFAULT_END
        # No explicit override on this song (start/end still at the
        # placeholder defaults): the clip is whatever the cache already
        # has for this video, or - on the first run for it - a fresh
        # heatmap pick. Skipping pick_clip on a cache hit isn't just an
        # optimization: pick_clip rolls a random hot section on every
        # call, so re-running it each time would silently pick a
        # different window run to run instead of reusing the one already
        # rendered before.
        if start == _DEFAULT_START and end == _DEFAULT_END:
            cached = cached_clip(song["url"])
            if cached:
                _log(f"  [rank {song['rank']}] Using cached clip")
                dl_times[song["rank"]] = time.monotonic() - t0
                return cached
            picked_start, picked_end = pick_clip(song["url"])
            start, end = f"{picked_start:.2f}", f"{picked_end:.2f}"
            _log(f"  [rank {song['rank']}] Heatmap picked clip at {picked_start:.1f}s-{picked_end:.1f}s")
        raw_clip = download_song(song["rank"], song["url"], start=start, end=end)
        dl_times[song["rank"]] = time.monotonic() - t0
        return raw_clip

    def _encode(song, raw_clip, head_trim, tail_trim):
        meta       = song["_meta"]
        entry_type = song["_entry_type"]
        peak       = song.get("peak", song["rank"])
        t0 = time.monotonic()
        result = encode_song(
            style, raw_clip,
            rank=song["rank"], title=song["title"], artist=song["artist"],
            peak=peak, entry_type=entry_type,
            views=meta["views"], release_date=meta["release_date"],
            months_on_chart=meta["months_on_chart"],
            views_gained=song["_views_gained"],
            rank_change=song["_rank_change"],
            codec=codec, head_trim=head_trim, tail_trim=tail_trim,
            clips_dir=clips_dir,
        )
        enc_times[song["rank"]] = time.monotonic() - t0
        return result

    # Two independently-sized pools connected by a pipeline: as each download
    # finishes it's immediately handed to the encode pool, so encoding for
    # earlier songs overlaps with downloading of later ones instead of the
    # two stages being coupled to one shared worker count. Every clip except
    # the very first/last is trimmed on the side(s) touching a neighbor -
    # that sliver is instead rendered by the transition stage below, which
    # blends it into the adjacent clip. Trim amounts are decided from each
    # song's fixed position in `countdown`, before any downloads finish, so
    # this pipelining isn't held up waiting to learn the final success list.
    completed_by_rank, raw_by_rank, failed = {}, {}, []
    dl_times, enc_times, trans_times = {}, {}, {}
    dl_done = enc_done = 0
    dl_stage_start = time.monotonic()
    enc_stage_start = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=download_workers) as dl_pool, \
         concurrent.futures.ThreadPoolExecutor(max_workers=encode_workers) as enc_pool:

        download_futures = {dl_pool.submit(_download, song): song for song in countdown}
        encode_futures = {}

        for future in concurrent.futures.as_completed(download_futures):
            song = download_futures[future]
            try:
                raw_clip = future.result()
            except RuntimeError as e:
                _log(f"  ERROR: Rank {song['rank']} ({song['title']}) - {e}\n")
                failed.append((song["rank"], song["title"]))
                continue
            dl_done += 1
            _log(f"  [rank {song['rank']}] Downloaded in {dl_times[song['rank']]:.1f}s ({dl_done}/{n})")
            raw_by_rank[song["rank"]] = raw_clip
            idx = rank_to_index[song["rank"]]
            head_trim = 0.0 if idx == 0 else transition_duration
            tail_trim = 0.0 if idx == n - 1 else transition_duration
            if enc_stage_start is None:
                enc_stage_start = time.monotonic()
            encode_future = enc_pool.submit(_encode, song, raw_clip, head_trim, tail_trim)
            encode_futures[encode_future] = song
        dl_stage_end = time.monotonic()

        for future in concurrent.futures.as_completed(encode_futures):
            song = encode_futures[future]
            try:
                completed_by_rank[song["rank"]] = future.result()  # seg_clip path
            except RuntimeError as e:
                _log(f"  ERROR: Rank {song['rank']} ({song['title']}) - {e}\n")
                failed.append((song["rank"], song["title"]))
                continue
            enc_done += 1
            _log(f"  [rank {song['rank']}] Encoded in {enc_times[song['rank']]:.1f}s ({enc_done}/{len(encode_futures)})")
    enc_stage_end = time.monotonic()

    # Build the small boundary segment between every pair of adjacent songs
    # that both survived download/encode - skipped where a neighbor failed,
    # which just leaves a slightly earlier hard cut at that one boundary
    # instead of a crossfade. These are cheap (~1s of output each) and run
    # in their own parallel batch rather than one costly full-length re-encode.
    transition_jobs = [
        (countdown[i]["rank"], countdown[i + 1]["rank"])
        for i in range(n - 1)
        if countdown[i]["rank"] in completed_by_rank and countdown[i + 1]["rank"] in completed_by_rank
    ]

    def _build_transition(rank_a, rank_b):
        out_path = f"{clips_dir}/trans_rank{rank_a}_rank{rank_b}.mp4"
        t0 = time.monotonic()
        result = encode_transition(
            raw_by_rank[rank_a], raw_by_rank[rank_b], cw, ch, fps, out_path,
            codec=codec, video_transition=video_transition, duration=transition_duration,
        )
        trans_times[(rank_a, rank_b)] = time.monotonic() - t0
        return result

    transition_by_pair = {}
    trans_stage_start = trans_stage_end = time.monotonic()
    if transition_jobs:
        print(f"Building {len(transition_jobs)} transition(s)...")
        trans_stage_start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=encode_workers) as trans_pool:
            trans_futures = {trans_pool.submit(_build_transition, a, b): (a, b) for a, b in transition_jobs}
            for future in concurrent.futures.as_completed(trans_futures):
                pair = trans_futures[future]
                try:
                    transition_by_pair[pair] = future.result()
                except RuntimeError as e:
                    _log(f"  ERROR: Transition rank {pair[0]}->{pair[1]} - {e}\n")
        trans_stage_end = time.monotonic()

    # Raw clips are deliberately NOT cleaned up here: they live in
    # encoding.RAW_CACHE_DIR, keyed by video ID, so a song still on the
    # chart next run reuses the same file instead of re-downloading it.

    # Reassemble in countdown order, interleaving each song's segment with
    # the transition into its successor (when one was built).
    segments = []
    for i, song in enumerate(countdown):
        rank = song["rank"]
        if rank in completed_by_rank:
            segments.append(completed_by_rank[rank])
        if i < n - 1:
            pair = (rank, countdown[i + 1]["rank"])
            if pair in transition_by_pair:
                segments.append(transition_by_pair[pair])

    concat_time = 0.0
    if segments:
        t0 = time.monotonic()
        # Full re-encode, not a stream copy: every seg_/trans_ file came from
        # its own independent ffmpeg process, so each one injects its own
        # SPS/PPS at its boundary even with identical settings. Stream-
        # copying them together leaves those parameter-set changes embedded
        # mid-stream, which most software decoders shrug off but which
        # visibly breaks playback in players that lean on hardware decode
        # (confirmed reproducing in VLC, Discord's embedded player, and
        # mobile players - video freezes/blacks out right at each seam).
        compilation_path = f"{clips_dir}/final_compilation.mp4"
        concatenate_clips(segments, output_path=compilation_path, codec=codec, cw=cw, ch=ch, fps=fps)
        concat_time = time.monotonic() - t0
        print(f"Compilation saved -> {compilation_path}\n")

    save_run_state(ranked, csv_path)
    print(f"Updated {csv_path} for next run.\n")

    total_time = time.monotonic() - t_pipeline_start
    dl_span    = dl_stage_end - dl_stage_start
    enc_span   = enc_stage_end - enc_stage_start if enc_stage_start else 0.0
    trans_span = trans_stage_end - trans_stage_start

    print("=" * 55)
    print("Run summary")
    print("=" * 55)
    print(f"Total time:       {_fmt_secs(total_time)}")
    print(f"Download stage:   {_fmt_secs(dl_span)}  ({dl_span / total_time:.0%} of total)"
          f"  [{len(dl_times)}/{n} songs, {download_workers} workers, avg {_avg(dl_times.values()):.1f}s/song]")
    print(f"Encode stage:     {_fmt_secs(enc_span)}  ({enc_span / total_time:.0%} of total)"
          f"  [{len(enc_times)}/{n} songs, {encode_workers} workers, avg {_avg(enc_times.values()):.1f}s/song]")
    if transition_jobs:
        print(f"Transition stage: {_fmt_secs(trans_span)}  ({trans_span / total_time:.0%} of total)"
              f"  [{len(trans_times)}/{len(transition_jobs)} built, avg {_avg(trans_times.values()):.1f}s each]")
    print(f"Final concat:     {_fmt_secs(concat_time)}  ({concat_time / total_time:.0%} of total)")
    print("(Download/encode stages overlap by design - percentages won't sum to 100%.)")
    print("=" * 55)

    if failed:
        print("\nFailed songs:")
        for rank, title in failed:
            print(f"  Rank {rank}: {title}")
