# Manual song-grouping prompt

For grouping the registry's songs with several Claude Code instances instead of Gemini.

**As of 2026-08-02:** Making/Behind/trailer/teaser/etc. content is filtered
at catalog time (`shared/mv_filter.py`'s `BLOCKLIST`) and, when a title is
caught, the video is still recorded with `song_id = NULL` rather than
dropped (`registry/catalog.py`) - see the DO-NOT-merge entry below for why
NULL rather than a singleton song row. A manual pass on a registry synced
entirely after this date should see very little of this pattern; a
registry (or a channel) with history from before it may still need the
one-time excision sweep described there.

## How to shard

Grouping is sharded by a song's **anchor channel** (`songs.channel_id`), never by an arbitrary row range.

Two uploads of one song can only ever be merged if the same instance sees both.
A song's variants all share one anchor, and no correct merge ever crosses artists, so this key can't split a song across instances.
Sharding by `song_id` or `LIMIT/OFFSET` would, and the split is permanent - grouping is additive and never revisits a video.

Run each instance in its own terminal, in the repo root, with a different `SHARD` and the same `SHARD_COUNT`.

```
SHARD_COUNT = 6      SHARD = 0, 1, 2, 3, 4, 5
```

Shard sizes are uneven (a distributor's catalogue dwarfs a single artist's), which is fine - they're independent.

## Before starting any instance

The registry is a git-synced data repo, not a hosted database (see README's "Git-synced registry database" section) - its own git history already keeps a full, real backup of every previously-pushed state, so there is no separate manual dump step. If you want an extra local safety net before a big multi-session pass, just copy the local working copy aside:

```bash
cp data/.registry_working.db "data/registry-backup-$(date +%Y%m%d-%H%M%S).db"
```

Run instances **one at a time** unless you've confirmed applying concurrently is safe.
Concurrent writers to the same local SQLite file can corrupt it (no server-side connection arbitration the way a hosted database has), and a failed apply mid-shard is still annoying to unpick if two instances' edits interleave unexpectedly.
Safest pattern: let every instance produce its plan, then apply them in sequence.

### Session file layout

All grouping-audit sidecars live under `data/.grouping-audit/` (gitignored as a
unit - never commit this directory). Do not write audit scratch next to
`data/.registry_working.db` or as loose `data/.audit_*` files.

```
data/.grouping-audit/
  progress.json              # durable "channels already reviewed" log
  state.json                 # headless grouping_audit trigger state
  log.jsonl                  # headless agent transcript (when audit runs)
  shards/
    progress-{N}.json        # per-shard progress while parallel agents run
    scratch-{N}.txt          # per-shard working notes
  candidates/                # candidate-list dumps for a session
  scripts/                   # one-off apply/precheck scripts for a session
  reports/                   # session summaries / reports
```

Rules for agents writing files here:
- Progress that should survive the session → `progress.json` (or
  `shards/progress-{N}.json` while a parallel shard is in flight; merge
  into `progress.json` and delete the shard file when that shard finishes).
- Freeform notes / title dumps while reading → `shards/scratch-{N}.txt`
  (or a dated file under `reports/` for a whole-registry sweep).
- Generated candidate lists → `candidates/`.
- One-off Python you write to apply a batch → `scripts/` (delete after
  the session's merges are verified - see Procedure step 7).
- Lasting rules and precedents → this doc, not a scratch file.

---

## The prompt

Paste this into each instance, replacing `SHARD` and `SHARD_COUNT`.

````
You are grouping songs in the local working-copy SQLite file at
data/.registry_working.db (see src/registry/db.py's module docstring).
This is NOT data/registry.db - that path is a pre-migration artifact
and is stale; writing to it silently loses your work, since nothing
reads it back into the data repo. Before you start, confirm the
working copy is current - either it was populated very recently by a
command that calls db.pull_to_local() (sync/decouple/regroup all do),
or pull it yourself:

    python -c "from registry import db; db.pull_to_local()"

When your session's approved batches are applied, push them back so
the data repo (the actual source of truth every machine reads) sees
them:

    python -c "from registry import db; db.push_from_local()"

Do this at the end of each session, not just once at the very end of
the whole multi-session pass - an interrupted session (crash, closed
terminal) between pull and push leaves real merge work sitting only in
the local file, invisible to any other instance or machine.

You are instance SHARD of SHARD_COUNT. Work ONLY on your shard.

## Session files

All audit sidecars go under `data/.grouping-audit/` (gitignored). Do not
write loose `data/.audit_*` / `data/.apply_*` files. Layout:

- `progress.json` - durable reviewed-channel log (this instance: also
  `shards/progress-SHARD.json` while in flight, merge into progress.json
  when done and delete the shard file)
- `shards/scratch-SHARD.txt` - working notes
- `candidates/` - candidate-list dumps
- `scripts/` - one-off apply/precheck scripts (delete after verified)
- `reports/` - session summaries

See the doc's "Session file layout" section for the full convention.

## Your shard

Songs whose anchor channel falls in your shard:

    SELECT s.song_id, s.canonical_title, s.channel_id
    FROM songs s
    WHERE (SELECT rowid FROM channels ch WHERE ch.channel_id = s.channel_id) % SHARD_COUNT = SHARD

For each of those songs you also need its videos:

    SELECT video_id, title, channel_id FROM videos WHERE song_id = ?

## What you are deciding

Each song row is one or more video uploads. Some songs that should be a
single entry are currently split across several song rows, because the
free matching tiers only merge titles that normalise identically. Your
job is to find those splits and merge them.

You are NOT re-grouping everything from scratch. Existing groupings are
correct; you are only merging song rows that are the same underlying song.

If `songs` is empty or nearly every video has `song_id IS NULL`, the
registry was decoupled (see decouple.py) and never regrouped. Stop and
run `python run.py regroup` first (add `--genre` to scope it) - it
re-derives the free deterministic tiers with no YouTube/Gemini cost. Only
start the manual pass once that has run; otherwise there is nothing
"already grouped" for a manual pass to build on.

## Do this by reading, not by writing matching code

Do the matching yourself, by reading titles and applying judgment -
including reading Hangul/Japanese/Chinese script directly, which is
exactly what the free tiers and a script can't do. Do NOT write a
script, regex, or fuzzy-matching/similarity heuristic to generate merge
candidates *within the channel you're reading*. A candidate-generation
script re-implements the same normalisation the free tiers already ran
(so it mostly finds nothing new) and can't apply judgment to the
genuinely hard cases (arrangement vs. presentation, cover vs. original,
same-title-different-artist) - those need a reader, not a matcher. Plain
`SELECT` to pull titles is fine and expected; anything that scores or
clusters titles for you is not.

**Narrower exception, learned the hard way:** a *whole-registry* sweep
for a pattern that's already a decided rule - not a judgment call - is a
different kind of task than per-channel matching, and a script is the
right tool for it. Two examples that turned up real misses this way:

1. **Cross-channel duplicate detection.** Reading one channel's catalog
   (`WHERE videos.channel_id = ?`) misses duplicates that were uploaded
   to a *different* channel and only later cross-channel-matched to this
   one's anchor (see "Process notes" below) - a whole per-channel pass
   can never see these side by side. Fix: pull every `(song_id,
   channel_id, canonical_title)` from the whole `songs` table, normalise
   each title (strip bracket/paren content and ~40 known presentation-
   format words - Live Clip, MV, Dance Practice, Official Video, Behind,
   Making, etc.), group by `(songs.channel_id, normalised_title)`, and
   flag any group with more than one `song_id`. This is candidate
   generation for a human/AI to read and confirm before merging, never
   an auto-apply - the same "when unsure, don't merge" rule still governs
   every candidate it surfaces. Found 16 genuine misses this way on a
   registry that had already had a full per-channel pass (Weeekly, IU,
   CNBLUE, NCT DREAM, GFRIEND, SF9, N.Flying, and others each had a Live
   Clip or Stage Clip sitting unmerged from its MV for exactly this
   reason), plus one more deterministic-tier over-merge (a `Vocal Ver.`
   wrongly fused with `Band Ver.`, caught while reading one of the
   clusters - superseded 2026-08-10: a `Vocal Ver.` now merges with
   everything else per the Rules section's reversal, so this was not
   actually a bug under the current rule).
2. **Making/Behind excision** (see the DO-NOT-merge entry above) - a
   decided, binary classification with no judgment involved once a
   candidate is surfaced, so generating the candidate list by regex and
   then eyeballing it for the rare real-song false positive (see that
   entry) is the efficient and correct approach, not a shortcut around
   the "read, don't match" rule.

The line: a script may not *decide* a merge or a classification, but it
may *surface candidates* for a decided rule or for your own subsequent
reading, at a scope (whole-registry) that per-channel reading structurally
cannot reach.

## Rules - these are decided, do not re-litigate them

**2026-08-10 operator-directed reversal - read this before anything below.** Two categories
that used to be DO-NOT-merge are now MERGE, project-wide and retroactively: arrangement variants
(remix - including a named producer/DJ remix - acoustic, instrumental, vocal, karaoke, a
cappella, sped-up/slowed) and language versions (Japanese Ver., English Ver., etc.). Every
section below this point - the DO-NOT-merge precedent list, the "sharper read on Ver tags"
section, the SEVENTEEN/KBS exclusion list, the "LEMONADE" / "LEMONADE (2Spade Remix)" example -
predates this reversal and says the opposite for these two categories specifically. Where an
older entry conflicts with this note on those two categories, this note wins; everything else in
those entries (Making/Behind excision, different-artist, numbered-sequel, feat.-credit,
cross-channel-duplicate guidance) is unaffected and still applies exactly as written. See
`src/registry/song_grouping.py`'s module docstring for the code side of this change, and
`src/registry/merge_versions.py` for the one-off migration that applied it to the
already-grouped registry.

MERGE two song rows when they are the same underlying song, whether presented as the same
recording in a different format (official MV, lyric video, dance practice, choreography video,
performance/encore/live-clip video, plain audio upload, visualizer) or as a different
arrangement/mix of the same song (remix, acoustic, instrumental, vocal, karaoke, a cappella,
sped-up/slowed) or a different-language recording of the same song (Japanese Ver., English Ver.,
etc., recognized by the explicit tag/translation, not by comparing native-script text).

DO NOT merge, in any of these cases:

- Different artists, even with identical titles.
- Numbered or qualified variants: "Supernova" and "Supernova 2" differ.
- Any difference at all in native-script (Hangul/Japanese/Chinese) text, UNLESS the two titles
  are a language-version pair per the MERGE rule above. For any other pair, treat native script
  as the most reliable identity signal and compare it character by character - if it differs
  even by one character, they are different songs.

The genuine merges you ARE looking for are mostly:
- the same song romanised differently, or native script vs romanisation
  ("Supernova" / "슈퍼노바")
- stylised punctuation or spacing differences that normalisation missed
- a title that abbreviates or expands the other

When unsure, DO NOT merge. A wrong merge silently corrupts a chart entry
by fusing two songs' view counts. A missed merge only leaves them
separate, which is the status quo and is recoverable later.

### Precedents from prior runs

Recurring cases already settled by hand across ~10 channels and several
thousand songs (1theK, SM's ToHeart, JYP's 2AM, Starship, KBS WORLD TV,
HYBE LABELS, Antenna's Jung Seung-hwan, TXT) - apply the same call rather
than re-deriving it. Read this whole section before starting a fresh
channel; it will save you from re-litigating the same handful of
patterns dozens of times.

**MERGE - presentation-format variants of the same recording:**
- Repeat broadcast/re-airing of the same clip where only a date stamp or
  show name differs (e.g. `KBS WORLD TV 250711` vs `...250704`, or
  `Stage @ SBS Inkigayo 2015.11.01` vs `...2015.10.25`) - broadcaster
  channels (KBS WORLD TV) and label channels with heavy TV-stage reposting
  (JYP, TXT) are full of these; a single song can easily have 5-15 such
  duplicates.
- Per-member "focus" stage videos, vertical dance-practice cuts, or
  fancams (`[#JAEHYUN Focus] ... Dance Practice`, `[Jackson 직캠(Fancam)]
  ... Stage`) - merge every member's cut into the one song, alongside the
  plain/group version.
- `[MV]`-prefixed or bracket/underscore-restyled re-uploads of the same
  title that the normaliser's exact-match tier didn't catch.
- Live Clip / Lyric Video / Performance Video / Performance Film /
  Conceptual Performance Film / Choreography Video / Dance Practice /
  Dance Ver. / Performance Ver. / Choreography Ver. / Visual ver. /
  Close-up ver. / Onetake Ver. / Front-cam Choreography / `ENG Lyric
  Video` (captions on the *same* recording, distinct from a re-sung
  `(English Ver.)`) against the same song's plain MV.
- Numbered or lettered official-rollout variants tied to one release:
  `Plan A` / `Plan B`, `#1 MV` / `#2 Performance Video` / `#3 Performance
  Video`, `Part.1` / `Part.2`, `Ver. A` / `Ver. B` *when paired with a
  Dance/Performance/Choreo qualifier* - these are alternate official
  video cuts of one song, not different recordings.
- `(B-side)`, `(Extended ver.)`, `(Vertical ver.)`, `(Uncut Ver.)`,
  `(Director's Cut)` - alternate edits of the same official video.
- Same feat. credit, different camera cut: `(Choreo Close-up ver.)` /
  `(Choreo Full Shot ver.)`.
- SM's dual-language sub-units (EXO-K/EXO-M, etc.): merge MV + Dance
  Practice + numbered-version *within* one language/unit, but see the
  DO-NOT-merge entry below - never merge across units.

**DO NOT merge, and DO NOT give it its own song row either - excise entirely:**
- `(Making Ver.)` / `MV Behind` / `MV Making` / `MV Commentary` /
  `Self MV 촬영 현장 BEHIND` - behind-the-scenes footage, not a recording
  of the song. WATCH FOR: in older (2010s Cube/BEAST-era) titles, `BTS`
  means "Behind The Scenes", not the group BTS - e.g. `'SHOCK' (BTS:
  Music Video pt.1)`. The deterministic tier has been caught merging
  these into the plain MV at least twice (title-normalization treats the
  `(BTS: ...)` parenthetical as noise) - check for this pattern and
  split it back out if found.

  **Decided (2026-08-02): Making/Behind content should not even be its
  own `songs` row.** Earlier guidance stopped at "don't merge it into the
  real song," which left every Making/Behind video as its own singleton
  song - each one cluttering the chart as if it were a real, if
  unpopular, song. The user reviewed this and called it: the video row
  stays (data completeness, and so a future catalog sync doesn't
  re-discover and re-walk past it every time - see
  `mv_filter.BLOCKLIST` and `catalog.py`'s `blocked`/`blocked_video_dicts`
  handling, which now do this automatically for new syncs going
  forward), but `song_id` is set to `NULL` rather than pointing at a
  dedicated song row:

      UPDATE videos SET song_id = NULL WHERE video_id IN (<the Making/
        Behind video_ids>);
      DELETE FROM songs WHERE song_id NOT IN
        (SELECT song_id FROM videos WHERE song_id IS NOT NULL);

  If you're auditing an already-grouped registry for this (rather than
  relying on the catalog-time filter, which only applies to videos
  synced after 2026-08-02), do NOT hunt for exact phrases like "MV
  Behind" one at a time - the real-world phrasing space is much larger
  than it looks (see "Do this by reading, not by writing matching code"
  above, which covers when a whole-registry regex sweep like this is the
  right tool): "MV Shooting Behind",
  "Stage Behind", "Back Stage Behind", "Live Clip Behind", "VCR Behind",
  spelled-out "Music Video Behind", 메이킹, 비하인드, 촬영 현장/비하인드,
  and more, each show up as their own title shape on different channels.
  Match broadly (bare `behind`/`making`/메이킹/비하인드 anywhere in the
  title) and then read the whole candidate list once to hand-exclude the
  real song titles that happen to contain the word "Behind" as an
  ordinary word, not a footage descriptor - these exist and are not rare:
  "Behind U", "Behind You", "Behind The Shine", "Behind the page" (all
  genuine OST/single titles) and "Hands Behind My Back" (a real song
  about literally that). A tight, anchored regex (require "behind" to sit
  next to a production noun - mv/stage/clip/practice/vcr/concert/
  shooting/bonus/performance) misses a few genuine cases (rare artist-
  specific phrasings) but has never produced a false positive across the
  whole registry when checked this way - prefer that miss rate over
  guessing on a bare `\bbehind\b` match.
- `MV EVENT 달성 영상` (goal-achieved celebration clip) / `미리보기`
  (MV preview/teaser) - promotional content tied to an MV's release or
  a view-count milestone, not a presentation of the song itself; same
  bucket as Behind/Making.
- Variety-show "Reaction" videos (`나 혼자 산다 Reaction | KEY 'BAD LOVE'
  MV`, `놀라운 토요일(Amazing Saturday) Reaction | ...`) - cast members
  watching and reacting to the MV, not the MV itself; same bucket as
  Commentary.
- **Superseded 2026-08-10 - now MERGE, see the Rules section above.** `(Acoustic Ver.)` /
  `(Instrumental)` / `(Inst.)` / `(Vocal Ver.)` / `(Karaoke Ver.)` / `(Remix)` / named remixes
  (`(Sara Landry Remix)`, `(Champions Remix)`) / `(Sped Up Ver.)` / a cappella, and a
  producer/DJ name used as a bare `"(Ver.)"` tag with no "Remix" word present (ATEEZ's `BAD
  (Steve Aoki Ver.)` / `(Ofenbach Ver.)` / `(James Carter Ver.)` / `(LEEZ Ver.)` / `(Ollounder
  Ver.)`, or a numbered official remix-EP rollout like `WORK Pt.2 - ATEEZ X Don Diablo` / `Pt.3 -
  ... X Eden-ary` / `Pt.4 - ... X G-Eazy`) - all of these now merge into the plain version and
  into each other, same as any other presentation variant. (A *member's* name in a Ver. tag,
  e.g. `Adrenaline Choreography Demo (SAN Ver.)`, was never in this bucket - that's a
  per-member cam and has always merged normally.)
- A different feat./collab credit on an otherwise identical title
  (`(feat. GURU RANDHAWA)` vs `(feat. SANTOS BRAVOS)` vs no feat. at
  all) - each regional/collab edit is its own recording unless the only
  difference is a video-cut qualifier (see Choreo Close-up/Full Shot
  above). This includes a song's headline feature being present in one
  upload's title and absent in another's when there's no other evidence
  they're the same cut (e.g. BTS's "IDOL" with vs without Nicki Minaj -
  she raps an added verse, so these are different recordings).
- A title explicitly marked "Original Song by X" (a cover, so a
  different performing artist than X); fan choreography-cover-contest
  winners, survival-show contestants, or a variety segment's self-cam
  cover of another song - always a different performer from the
  original artist, never merge with the original.
- Two different named artists crediting the identical OST title (common
  when a drama OST has several artists each release their own take on
  one title) - stays split even though the title text is identical.
- **Superseded 2026-08-10 - now MERGE, see the Rules section above.** Any language-version tag
  (`(Korean Ver.)`, `(Chinese Ver.)`, `(Japanese Ver.)`, `(English Ver.)` as a *sung* MV/
  Visualizer, or two titles that differ only by one carrying native-script text the other
  lacks, e.g. `Love Shot` vs `宣告 (Love Shot)`) now merges with the original-language version,
  even without an explicit "(Ver.)" tag and regardless of whether the surrounding video format
  matches.
- Combined/medley videos whose title names two or more different songs
  at once (`"Candle" & "I Feel You" Comeback Stage`, `Intro & 딱 좋아`
  when Intro genuinely is a separate pre-existing track) - ambiguous
  scope, leave split.

**Decided: merge bare `(Ver.)` tags too.** Earlier runs treated a bare
`(Ver.1)` / `(Ver.2)` / `(Ver. A)` / `(Ver. B)` on a plain MV or song
title (no Dance/Performance qualifier) as too ambiguous to merge -
could be an alternate video cut, could be a re-recorded single. The
user reviewed this and called it: **merge these too**, same as the
Dance/Performance-qualified version. Apply this retroactively if you
find a channel where it wasn't - e.g. TRAX's "초우(初雨) (Cold Rain)
MV Ver. A/B", Chu Ga Yeoul's "나 같은 건 없는 건가요 MV Ver. 1/2",
OH MY GIRL's "LIAR LIAR (ver.2)" vs plain, B1A4's "LONELY(없구나)
(Ver.2)" vs plain, and named-not-numbered variants like SUPER JUNIOR's
"Callin' (Winter ver.)" vs "(Winter for Spring ver.)" and DIA's
"Paradise (Hope ver.)" vs "(Dream ver.)" were all found split this way
and merged once this call was made.

Extending the same logic, `(Live Ver.)` / `(Band Ver.)` / `(Drama
Ver.)` / `(Busking Ver.)` / `(Onetake Ver.)` / `(Piano Ver.)` should
also just be merged rather than flagged - treat any `(...Ver.)` tag as
presentation. **Superseded 2026-08-10:** the "unless it names an
unambiguous arrangement change" carve-out no longer applies - a Remix,
Acoustic, Instrumental, Sped Up, Karaoke, Vocal, or a cappella tag now
merges too, same as everything else in this paragraph, per the Rules
section above.

**Ambiguous, defaulted to NOT merging - flag rather than guess:**
- A real artist name paired with an identical title credited to "Various
  Artists" (could be a compilation trailer rather than that artist's own
  upload).
- A named "concept" MV version tied to a specific sub-brand or virtual
  persona (aespa's `(æ-aespa Ver.)`) - treated as the same song's
  alternate video concept in the one case seen; flag if you hit another
  artist's equivalent and aren't sure.
- `[LYRIC ASMR] "Song" (Member Ver.)` x N, one per member, each reading/
  whispering the lyrics solo (GOT7's "Miracle") - unlike per-member dance
  fancams, these are not different camera angles on one shared performance
  instance; each is its own individually-recorded take. **Superseded
  2026-08-11:** the deterministic tier now merges these too, as a
  side effect of the 2026-08-10 reversal making any `(...Ver.)` tag merge
  unconditionally - confirmed by the operator as an acceptable
  simplification rather than worth a special-case carve-out. No longer
  left split.

**If you find the deterministic tier over-merged (opposite problem):**
Originally noted as rare (one instance: Monsta X's `WHO DO U LOVE?
(will.i.am REMIX - Audio) ft. French Montana` had been auto-merged into
the plain `WHO DO U LOVE?` song, purely because title-normalization
strips parentheticals). **Revise that framing** - a full-registry manual
pass (2026-08-06 through 2026-08-10, ~500 channels) turned up at least
six more: KNK (a SUNSET Performance video wrongly fused into RIDE's
song row), N.O.M ("I'm so sexy" M/V had a `[BEHIND]` video fused in),
Minseo (two different songs both titled "Growing Up" in English but
different Korean parentheticals, fused on the English title alone),
AleXa (an English-Ver. vocal take fused into the plain version), Maywish
(an instrumental Audio upload fused into the vocal version), and Lee
Ju-eun (a Sped Up Ver. fused into the plain version). This is not a
one-off anymore - expect to find a small number of these in any
large-enough batch, still worth fixing opportunistically rather than
proactively hunting for across the whole registry (still out of scope
as its own sweep), but don't dismiss it as unlikely when you do spot
one. Fix it: `INSERT INTO songs (channel_id, canonical_title, grouped_at)
VALUES (<same anchor channel_id as the wrongly-merged song>, <the
remix's own title>, strftime('%Y-%m-%dT%H:%M:%f','now'))`, then `UPDATE
videos SET song_id = last_insert_rowid() WHERE video_id = '<that
video>'`.

**Superseded 2026-08-10, for four of these six:** the Monsta X will.i.am
REMIX case, the AleXa English-Ver. case, the Maywish instrumental case,
and the Lee Ju-eun Sped Up Ver. case were "fixes" that split a remix/
language-version/arrangement-version pair back apart. Per the Rules
section's reversal, those pairs are now correctly merged, not a bug -
do not apply this fix to that shape of case going forward. The KNK
(wrong-song fusion) and N.O.M (Behind-video fusion) cases are unrelated
bugs (a genuinely different song, and non-song content, respectively
fused in by mistake) and this fix procedure still applies to those.

### Precedents from the 2026-08-02 new-batch review

A sync added 6,024 videos across 204 new/expanded channels in one day; 8
parallel reviewers each worked a disjoint set of channels against the
rules above. These are the new patterns and refinements that came out of
that pass - apply them going forward rather than re-deriving them.

**MERGE - additional presentation-format variants:**
- Two *different-named* live/stage occasions of the same song, with no
  shared broadcast/date-stamp tying them together (e.g. an "End of Year
  Stage" performance and a separately-named "Christmas Special Stage" of
  the same song) - extends the repeat-broadcast precedent beyond
  same-show reruns to any two legitimate live performances of one song.
- A hashtag-tagged or "Special Lyric Video (Member Ver.)" that is a
  visual-only montage over the *same audio track* - treat like a
  per-member focus cam (same recording, different visual edit). Contrast
  with GOT7's LYRIC ASMR case below, where each member re-performs solo.
- Multi-day "Day 1/2/3" (일차) dance-practice "progress" uploads of the
  same song - one practice session's documented progression, not
  different recordings.
- A sub-unit-branded Ver. tag on a Performance Video (e.g. "NEXZOO
  ver.") - a sub-unit's own performance cut, same treatment as a
  per-member cam.
- A named-concept alternate MV ("SKZOO ver.", "GROUND SOCCER Ver.",
  "ZOMBIE PERFORMANCE") - confirms the aespa `(æ-aespa Ver.)` call below
  generalizes to other artists: a themed/branded alternate video of the
  same song is a presentation variant, not a different recording, as
  long as the underlying song is unambiguously the same one.
- Alternate lighting/color-grade cuts of one shoot (e.g. "Sunlight/Fixed"
  vs "Moonlight/Fixed") - same bucket as Director's Cut/Extended ver.
- A "교차 편집" (cross-edit) compilation of one song's live broadcast
  stages - a compilation, but of a single song's performances, so same
  bucket as a Live Clip, not a medley.
- A producer credit vs. a feat. credit naming the *same* collaborator
  ("Produced by X" vs "feat. X" on an otherwise identical title) - the
  same recording, not a different collab edit.
- A "Self MV" / "Fan Music Video Project" / member-produced alternate
  edit, and a "Conceptual Performance Film Rehearsal" - an alternate
  official-style edit or a practice-run of a listed presentation format,
  not Making/Behind.

**A sharper read on Ver. tags:**
- **Superseded 2026-08-10:** this bullet used to say a named `(Ver.)` tag on a Lyric Video or
  Visualizer (usually a producer's alias) is a remix credit and shouldn't merge, unlike the same
  tag on a Dance Practice/Performance Video. Per the Rules section's reversal, a remix/producer
  credit now merges too, regardless of video type - so this distinction no longer matters; every
  named `(Ver.)` tag merges the same way now.
- A Ver. tag that names a specific broadcast/award-show occasion (e.g.
  "(Rock Ver.)" performed at a named festival, "(Christmas Ver.)" at a
  year-end awards show) is more likely a genuinely rearranged special
  stage than a plain re-airing - default to NOT merging these, unlike an
  unattributed costume/theme tag ("Winter ver.", "Halloween ver.") which
  still merges normally as a presentation variant.
- Where a Ver.-style qualifier sits can also matter: placed *outside* the
  quoted song title (`"CEREMONY" Dance Practice (Marching Band Ver.)`) it
  usually describes the cut - merge; placed *inside* the quoted title
  (`"CEREMONY (KARMA Version)"`) it more often names a distinct official
  mix - don't merge.

**Individually-recorded takes, a second worked example:** per-member
"Original Stage" solo performances (each member performing the whole
song solo, not a camera angle on one shared instance) stay split - same
call as GOT7's LYRIC ASMR case below. Contrast with a per-member "Stage
Cam (focus)" cut of one group performance, which does merge.

**Excise - non-song content beyond the literal Making/Behind/Reaction/
Commentary wording.** All of these are the same underlying "not a
presentation of the song" judgment as the DO-NOT-merge/excise entry
above, just without the literal keyword - read the content, not the
title's vocabulary:
- A numbered documentary/vlog series about an MV's production (e.g. "MV
  ep.01" through "ep.11"), even with no "Behind"/"Making" in the title -
  identified by episodic format and content, not a keyword.
- NG-cut blooper reels; administrative "viewing guide"/"how to watch"
  videos; tour or event promotional "Spot" teasers naming no song;
  tech-demo/experience clips (e.g. a "360 Reality Audio" walkthrough);
  casting-call or fan-participation promo videos.
- Farewell/thank-you tour VCRs, encore skit VCRs, and vlog-style
  event-announcement episodes with no song content.
- A retrospective/nostalgia series revisiting *other* artists' classic
  MVs with no "Cover by" credit - functionally the same as a Reaction
  video even without that word.
- "@MV Filming Set" per-member videos - behind-the-scenes-at-the-shoot,
  not a performance. Contrast with "@ [Stage Name]" or "@ Debut
  Showcase" per-member focus cams, which are real performance cuts and
  do merge.
- Album/single preview "Audio Snippet" or "Audio Guide" clips spanning
  multiple tracks - same bucket as 미리보기 (preview/teaser).
- A view-count-milestone celebration video stays excised even when its
  own title literally names a content type (e.g. "...MV 200만뷰
  안무영상") - the milestone/pledge framing controls over the literal
  type word.
- "MV 해석" / "MV Theory" (explainer/discussion videos analyzing an MV's
  plot or meaning) - same bucket as MV Commentary.
- Dorm-log, afterparty, or talk-segment vlog clips with no musical
  content, even when informally posted to the artist's own channel.

**Not Making/Behind, despite resembling it:** a comedic full-reenactment
"MV parody" series (a performer redoing another artist's MV as a bit,
not documenting its production) is a cover/parody performance like any
other - give it its own song row, don't excise it.

**Process note - a song can be identified only by a hashtag.** A title
with no readable song name in the visible text but an unambiguous
hashtag (e.g. `#babystep`) can still be matched to that song's existing
group - don't assume a title with no visible song-name text is
unmatchable without checking for one.

**Process notes:**
- A channel's own catalog is not the same as `songs.channel_id`
  (the anchor). To see everything a channel has ever uploaded - which is
  what you need to find splits within it - query through `videos`:
  `SELECT DISTINCT s.song_id, s.canonical_title FROM videos v JOIN songs
  s ON s.song_id = v.song_id WHERE v.channel_id = ? ORDER BY s.song_id`.
  A song's anchor may sit on a different (usually the artist's own)
  channel even though one of its videos was uploaded to the one you're
  reviewing.
- Cross-channel duplicates slip through even when both uploads already
  anchor to the same artist channel - e.g. IVE's "Supernova Love" was
  uploaded to both IVE's own channel and 1theK, both correctly anchored
  to IVE, but never linked to each other because the two titles'
  bracket/underscore styling differed enough to dodge the exact-match
  tier. A `WHERE canonical_title LIKE '%Song Name%'` sweep catches a
  one-off case you already suspect, but on a full pass do the whole-
  registry normalise-and-cluster-by-anchor sweep described under "Do
  this by reading, not by writing matching code" above instead of
  relying on LIKE sweeps per channel - it finds the ones you wouldn't
  have thought to search for, which turned out to be most of them. 1theK
  and HYBE LABELS (both large aggregator channels) were the source of
  nearly every miss found this way; a smaller artist's own channel
  rarely is.
- A broadcaster/label/aggregator channel's `display_name` can be wrong
  (a Wikidata data bug once produced the display name "Tacos de asada y
  cebollin" for TXT's actual channel) - this doesn't affect grouping,
  which keys on `channel_id` and video titles, not `display_name`, but
  worth flagging separately as a channel-metadata issue if you notice it.

### Precedents from the 2026-08-04 automated backstop audit

A whole-registry normalize-and-cluster cross-channel scan of 10,818 songs
surfaced 368 candidate clusters; 208 were genuine merges, reducing the song count to 10,594.
The vast majority applied rules already documented above.
One new judgment call:

**A headline feat. credit present in only one of two same-language-version uploads.**
LE SSERAFIM "HOT (English Ver.) feat. JADE" (MV) and "HOT (English Ver.) Performance Film"
(no feat. credit in title) were merged as the same English recording in different video formats.
The feat. credit's absence from the Performance Film's title is a title-formatting decision,
not evidence of a separate vocal cut without JADE.
This extends the existing "same feat. credit, different camera cut" rule to language-version pairs:
when two uploads share the same language-ver. tag and differ only in video format and whether
the feat. credit appears in the title, merge normally.
Contrast with the DO-NOT-merge "different feat. credit" rule (`feat. GURU RANDHAWA` vs.
`feat. SANTOS BRAVOS`), which applies when the collab itself differs - not when one title simply
omits a credit integral to the shared language version's release identity.

### Precedents and process notes from the 2026-08-05 session

**Bug, not a judgment call: some titles are stored as NFD (decomposed) Unicode, not NFC.**
A whole-registry normalize-and-cluster script written this session used a character-class regex
(`가-힣`, the precomposed Hangul Syllables block) to decide which characters survive
normalization. That block does not cover decomposed Hangul (individual Jamo, `ᄀ-ᇿ`),
so a title stored in NFD had its entire Korean portion silently stripped to nothing - collapsing
several unrelated songs on the same channel (Jang Na-ra's discography, worst case) into one false
cluster, since they all normalized to just the artist's romanised name. 132 `songs.canonical_title`
rows and 138 `videos.title` rows registry-wide are affected (spread across ~50 channels - Maywish,
NCT, Highlight, Onewe, Weeekly, ToHeart, and others each had at least one). Any future
normalize-and-cluster candidate script must call `unicodedata.normalize('NFC', title)` before
anything else, not just widen the character class - the fix is normalizing the input, not
special-casing the regex. This is a real data-quality issue independent of grouping: two uploads
of the same song could fail to match anywhere they're compared by string equality (search,
dedup, the free tiers' own exact-match tier) purely because one is NFC and the other NFD, with no
title-level clue anything is wrong. Worth a source-level fix (normalize at catalog time) and a
one-time backfill on existing rows; out of scope for a grouping pass to fix itself.

**Open question from this session, moot as of 2026-08-10:** is ATEEZ's "TOPPINGS" tag a
named-concept presentation variant or a remix/producer series? Both readings now merge per the
Rules section's reversal, so "Lemon Drop (TOPPINGS Drift Ver.)" should simply be merged with the
plain version - no need to resolve which category it was. The original open question is kept
below for context. The 2026-08-04 audit's precedent (see above)
treats a themed/branded alternate video as a presentation variant to merge, by analogy with
aespa's `(æ-aespa Ver.)` and SKZOO/GROUND SOCCER-style tags. Under that reading, ATEEZ's "Ice On
My Teeth" `(Double Pepperoni Ver.)` / `(OIive Alive Ver.)` / `(TELL ME BYE Ver.)` lyric videos were
merged into the main song this session, at the operator's explicit direction. But "Lemon Drop
(TOPPINGS Drift Ver.)" surfaced in the same session's whole-registry sweep uses the literal word
"TOPPINGS" in its tag - suggesting these are all installments of one named alternate-version
*series* tied to a release (closer to the producer/DJ-name-Ver.-tag non-merge precedent than to a
one-off themed concept video). This session left "Lemon Drop (TOPPINGS Drift Ver.)" split from
the plain version pending a clearer read, and did not revisit the already-applied Ice On My Teeth
merge. A future session with more context on whether "TOPPINGS" is an official named rollout
(check the release's own promotional material, not just the titles) should resolve both
consistently rather than treating them case by case.

**Process: check whether the local working copy is ahead of the data repo before any command that
pulls.** `python run.py regroup` and `decouple` used to wrap themselves in an automatic
`pull_to_local()` / `push_from_local()`, matching `sync`. That pull discards whatever local-only,
not-yet-pushed work exists (by design - `pull_to_local()` is always a full rebuild from the data
repo's current state, see `db.py`'s module docstring), which is exactly what happened this
session: a local working copy held a prior audit's 208 merges plus this session's own manual work,
none of it ever pushed to the hosted registry this project used at the time, and running `regroup`
silently reverted all of it before applying its own (otherwise correct) grouping of 188
newly-added, never-grouped channels. Recovered only because `pull_to_local()` also takes a
`registry.<ts>.pre-pull.db` backup before overwriting (see `_backup_and_prune_local()`) - that
backup is what made reconstructing the merged state possible. `regroup` and `decouple` no longer
auto pull/push (see `run.py` - they now behave like `audit`, local-working-copy-only); this doesn't
remove the underlying risk, since a plain `python run.py pull` can still discard un-pushed local
work the same way. Before pulling, check whether the local working copy is meaningfully ahead of
the data repo (e.g. lower `songs` count than expected, or a recent audit's progress-file entry with
no matching push) rather than assuming pull is always safe to run first.

**The cross-channel duplicate pattern (1theK, KBS WORLD TV, and same-song reposts across an
artist's own channel and a broadcaster/label channel) is not a rare miss - it was the large
majority of a fresh whole-registry sweep.** Of 245 genuine merge clusters found in one pass over
16,770 songs, the large majority were exactly this shape: a fully-grouped song on the artist's own
channel, with a duplicate (often multi-video itself) sitting unmerged on 1theK
(`UCweOkPb1wVVH0Q0Tlj4a5Pw`) or a broadcaster channel. This confirms the 2026-08-04 audit's note
that 1theK/HYBE LABELS are the source of nearly every cross-channel miss - it is worth treating
this specific shape (same artist name + same song title + one side anchored to a known aggregator
channel) as close to a decided-rule fast path during future reads, same spirit as the Making/
Behind excision sweep, rather than re-deriving the judgment call fresh on every one.

**Bug fixed at the source, not just noted: `normalize_title` (tier 1/2's own matching key,
`src/registry/song_grouping.py`) missed nearly every "Dance Practice/Performance Video (X Ver.)"
pairing with its own plain MV.** Found via VIXX's "향 (Scentist)" Dance Practice Video (Moving Cam
ver.) never matching its Official M/V - the bracket-stripper only fired when the bracket contained
a recognizable video-type word (mv/lyric/performance/video/etc.); a pure camera/motion qualifier
like "Moving Cam ver." or "Fix ver." contains none of those and survived untouched, silently
blocking the free tiers from ever linking the two. A follow-up whole-registry sweep using the
fixed function found 282 more genuine merge clusters of exactly this shape across channels that
had each already been through a per-channel manual pass - meaning this was not a rare miss, it was
systematically hiding almost every dance-practice-ver-tag pairing tier 1/2 should have caught for
free. Three fixes landed together in `normalize_title`:
1. A `_CAMERA_ANGLE_RE` (`moving`, `fix(ed)`, `cam`, `close-up`, `onetake`, `front-cam`, `vertical`,
   `horizontal`) strips a bracket containing only these words, regardless of video type - none of
   them double as a producer name or language name anywhere in this project's data.
2. Broader than that: **any** Ver. tag on a Dance Practice / Performance Video / Choreography Video
   now merges regardless of what the tag says - a costume theme, an occasion, a camera setup - per
   the operator's call that these video types have no separate audio mix to credit, so a Ver. tag
   there is never a producer alias the way it can be on a Lyric Video or Visualizer. This is the
   doc's existing "sharper read on Ver tags" section made literal in code rather than left to a
   human/AI reader to apply consistently. **Except** a tag naming a language (`(Japanese Ver.)`,
   `(Chinese Ver.)`, ...) - that always stays a different vocal recording regardless of video type,
   and this exception had to be added after RIIZE's "Love 119 (Japanese Ver.)" Dance Practice and
   ZHOUMI's Chinese-Ver/Korean-Ver "Uncontact LIVE Performance Video" pair both wrongly matched
   their other-language counterpart on the first pass.
3. A bare numbered/lettered Ver. tag (`(Ver.1)`, `(Ver. A)`) now merges unconditionally - the
   doc's "Decided: merge bare (Ver.) tags too" call was never actually implemented in the
   deterministic tier before this, only ever caught by a human/AI pass.

Also NFC-normalizes the title first (see the NFD bug above) - same function, same fix, applied at
the source instead of only in an ad-hoc audit script. See `tests/test_song_grouping.py`'s
`NormalizeTitleTest` for the regression tests locking all of this in (camera-angle match, any-Ver-
tag-on-dance/performance match, language-Ver-tag non-match, bare-numbered-Ver match, NFD match).

**Excised the same session, same categories as always, just new title shapes: numbered vlog/EP
series and "Talk" segments.** A whole-registry sweep for `\bep\.?\s*\d+` / `\btalk\b` / `\bvlog\b`
found the same Making/Behind/Reaction pattern wearing different words - `EP.14 M/V BEHIND`,
`Ep.31 ... 코멘터리` (commentary), `Ep.11 ... 촬영기` (filming diary), `Talk Marathon.mp4 ⏵ 'X' MV
Filming`, `JINHO-LOG EP.06/07 ... 촬영 Behind`, a dorm-log "뒤풀이 TALK" (afterparty talk) clip, a
fan-lyric-rewrite bit, a performance-tips discussion episode, and tripleS's own `MV ep.01`-`ep.11`
documentary series and two `MV 리액션` (reaction) uploads - all excised. Two long-running general
vlog shows (MONSTA X's "몬채널", ITZY's "ITZY? ITZY!") got the same call even where one episode's
title named a real content type ("Dance Practice") embedded in it: the whole video is a variety-
show episode, not a dedicated upload of that content, so it's excised entirely rather than kept
for the fragment. Contrast with KBS WORLD TV's "TALK & STAGE" broadcasts (2AM, SOLAR, KISS OF
LIFE, HYOLYN & RALRAL) - left alone, same call as the NCT "Behind Story & Live Stage" precedent
above: a combined interview+performance broadcast keeps its genuine performance half. And one
merge, not an excision: `평민에서 왕까지 EP.01/02/03` (commoner/noble/king costume theme) turned out
to be a themed 3-part Dance Practice rollout of one song, not a making-of series - merged, per the
"Day 1/2/3 progress" precedent above, once actually read rather than pattern-matched on "EP."
alone. The "false positive" side of this sweep was large too: "Talk" is the literal song title in
GOTTA TALK TO YOU/U, BODY TALK, Small Talk, Talk that Talk, Talk Saxy, Love Talk, Can We Talk
Again, We don't talk together, and Cherry Talk, and "EP" numbering on a genuine competition-show
cover performance (THE ORIGIN, KandiSilkWayStar) or the explicitly doc-protected 하와유.MOV MV-
parody series isn't Making/Behind at all - read the content the numbering wraps, not just the
numbering.

**AI-tier context, added this session: video tags.** `song_grouping.group_channel_videos` now
passes each new video's YouTube tags (persisted in `videos.tags`, fetched at zero extra quota cost
alongside the metadata catalog.py already pulls) into the Tier 3 prompt as a `[tags: ...]` hint -
see `_video_tags_label` and the prompt's own instruction that tags are supporting evidence only,
never grounds to override what the title (especially native-script text) says. Deliberately not
wired into Tier 1/2's deterministic matching: tags are uploader-supplied and inconsistent enough
that blind string comparison deciding a merge from them would be a real regression risk, whereas
the AI tier can weigh them with judgment the same way it already weighs everything else in this
doc. A video catalogued before this column existed simply has no tags - `_video_tags_label`
returns "" and the label falls back to plain title, no special-casing needed elsewhere.

### Precedents from a 2026-08-05 full-channel deep-read pass (BTS, BLACKPINK)

Prompted by the operator spotting "many Dynamite stage cam duplicates" and DDU-DU DDU-DU
split across song rows. Read both artists' entire anchor-channel song list by hand (324 songs
for BTS, 58 for BLACKPINK) rather than generating candidates with a script, per the operator's
explicit request this round. Found the "different live occasions merge" rule (decided
2026-08-02, see above) had never been retroactively swept - BTS's own channel alone had ~30
songs that should have been one ("Dynamite" was split across 13 song rows, one per award-show
Stage CAM; similar splits existed for Black Swan, FAKE LOVE, Boy With Luv, Dionysus, IDOL,
Life Goes On, DNA, ON, MIC Drop, and about a dozen smaller ones). BLACKPINK's channel had the
same shape at smaller scale (DDU-DU DDU-DU, Kill This Love, BOOMBAYAH, Pink Venom, Shut Down,
Typa Girl, JUMP, Ready For Love, plus JENNIE/LISA/ROSE solo-stage splits). 49 merge groups and
19 excisions applied across the two channels.

**New excision pattern: "MV Shooting" without the word "Behind" is still Behind footage.**
BTS's channel had 8 songs titled `'FIRE' MV Shooting - <thing>` / `'FIRE' MV Shooting-
'<Member>' Follow ver.` - shooting-day footage of the FIRE MV, same bucket as `MV Behind`, just
missing that literal word. Confirms the existing guidance to match on content, not vocabulary,
covers phrasings without "behind"/"making" anywhere in them at all, not just unfamiliar
synonyms for those words.

**New excision categories, same "not a performance of the song" judgment, no keyword overlap
with any prior list:** backstage "standby time" between/before stage performances; "monitoring
time" (members watching a stage-performance's own playback afterward); a member's on-camera
opinions about other members' solo stages (commentary); a "[Member] is directing a new music
video?!" clickbait-style vlog clip; a literal movie-trailer "Sneak Peek"; a between-song New
Year's stage greeting (announcement, not a performance); a "Choreographers BTS" feature about
the choreography staff themselves.

**Cross-script identity confirmed on two more real pairs, same underlying rule as the
Supernova/슈퍼노바 precedent:** BTS's `Stage SELF CAM '앙팡맨 by BTS'` and its own `'Anpanman'
Stage CAM` entries are the same song (앙팡맨 is a Korean phonetic rendering of "Anpanman," not
a different title) - merged despite sharing zero Latin-script characters. Separately, Agust D's
`'대취타' Live Clip` and BTS's own `SUGA's Daechwita Sword Dance Practice` are the same song
credited under the artist's group-member name instead of the solo persona used elsewhere on the
same channel - romanization (Daechwita) vs native script (대취타) plus a different-but-known
same-person artist credit, still one song.

**New presentation-format call: a game/avatar-rendered dance video is a presentation variant,
not a different recording.** `BLACKPINK X Selena Gomez - 'Ice Cream' DANCE PERFORMANCE VIDEO
(in ZEPETO)` - the same choreography and audio rendered through a virtual-avatar game
(ZEPETO) instead of filmed normally - judged the same way as a lyric video or visualizer
(same recording, different visual presentation), not a different recording. Merged with the
plain M/V.

**Scope note - not a whole-registry sweep.** This pass covered only these two artists' own
anchor channels in full. The underlying gap (the 2026-08-02 "different live occasions" rule
never being retroactively swept) almost certainly also affects other artists with a heavy
per-occasion live-stage upload habit (own-channel BANGTAN BOMB-style sub-brands, or any group
whose channel reposts award-show stages under distinctly-named titles rather than repeat
date-stamped reruns) - flagged for a future whole-registry pass, not resolved here.

**Note added 2026-08-06, mid whole-registry pass:** re-confirmed this section's two rules
(lowest song_id survives; canonical_title gets reset to the shortest title in the group) after
noticing the apply scripts used earlier in this session's channel sweep picked survivors somewhat
inconsistently and never touched canonical_title. Not worth re-deriving every prior channel's
merges to fix retroactively - the dedupe itself (one song_id, one video set) is correct either
way, canonical_title is a display nicety - but every merge from this point in the pass forward
follows both rules exactly as written below.

**Extended scope for "different live occasions merge" (2026-08-06):** the rule isn't limited to
Stage CAM videos. Any video representation of the same underlying song - Official MV, Official
Audio, Dance Practice, Performance Video, ENG/native Lyric Video, a broadcaster's re-aired
lyrics-overlay clip (e.g. "KBS WORLD TV"), a Director's Note re-upload of the same MV - collapses
into one song_id, regardless of content type or which occasion/broadcast it came from. Confirmed
against SEVENTEEN's channel, which mixes PLEDIS's own MV catalog with KBS WORLD TV re-broadcasts:
MAESTRO, LOVE MONEY FAME, 손오공, HOME;RUN, Rock with you, HOT, and THUNDER each had their KBS
lyrics-clip or ENG Lyric Video split into a separate song row from the Official MV of the exact
same song. **Superseded 2026-08-10:** this used to list named remixes/DJ Ver., a genuine
different-language re-recording ("(English Ver.)" with re-sung vocals), and Sped Up Ver. as
still excluded from this merge. Per the Rules section's reversal, all three now merge too -
only a feat.-credit version with an added guest vocalist stays excluded (that's a different-
collaborator rule, untouched by this reversal).

### The full-registry manual pass (2026-08-06 through 2026-08-10) - complete

Every channel with at least one `song_id` (506 channels at completion) has now been read in
full by hand at least once, either solo or via a parallel Task-agent shard (see below). Song
count went from ~16,000 at the start of this stretch to 12,381 at completion; video count never
moved (19,831 throughout - only `song_id` reassignment/nullification, no data loss). A future
session should treat this as **maintenance, not a fresh pass**: only channels added or expanded
since the last sync need a from-scratch read; an already-swept channel only needs re-reading if
its video count grew. `data/.grouping-audit/progress.json` has one entry per channel covered,
each with a `scope` field naming the channel(s) and a `note` summarizing what was found - check
it before re-reading a channel to confirm it isn't already done.

**Parallel Task-agent sharding, distinct from the "How to shard" section's terminal-instance
model above.** For a pass this large, dispatching multiple `Agent` tool calls (not separate
terminal instances) in parallel - each given a fixed, disjoint list of `channel_id`s and the same
rules/procedure as this doc - worked well and is the recommended approach for any future
large-scale sweep of this shape. Practical notes from doing it at ~500-channel scale:
- Give each shard agent its own scratch file (`data/.grouping-audit/shards/scratch-{N}.txt`) and
  its own progress-log file (`data/.grouping-audit/shards/progress-{N}.json`) rather than the
  shared `data/.grouping-audit/progress.json` - concurrent agents writing the same file corrupts
  it. Merge each shard's log into the main file (and delete the shard file) only after that shard
  reports fully complete. The whole `data/.grouping-audit/` tree is gitignored; still delete
  session scratch after the session so it doesn't accumulate or get re-read by mistake (see
  Procedure step 7 and "Session file layout" above).
- Concurrent `python run.py push` calls from sibling shards can hit a transient
  `git pull --ff-only` conflict. This is expected and self-resolves on a short wait-and-retry
  (~5s) - it is not data loss, just a race on which shard's push lands first.
- **The practical ceiling on shard count is the account's spend limit, not anything about the
  task itself.** Running many shards in parallel (10 was tried once, failed outright; 3 at a time
  repeatedly hit a *monthly* spend cap partway through a batch) will get agents killed mid-task
  with an API error, not a graceful stop. Recovery is straightforward: resend the interrupted
  agent a message telling it to check its own shard progress-log file for what's already
  applied+tested+pushed, then continue from the next unlogged channel in its original list - do
  not restart it fresh, since a fresh agent has no memory of which channel it was mid-way through
  and no channel list without being given one again.
- For the tail end (small channels, 1-10 songs each), pack far more channels per shard than for
  the head end (large aggregator/label channels) - a 40-channel shard of 1-6-song channels
  finishes faster than a 30-channel shard of 50-300-song channels. Batch by total *song count*
  across the shard, not channel count, when sizing shards unevenly.

**New excision categories found at this scale, same "not a presentation of the song" judgment,
no keyword overlap with prior lists:** a fan concert "Encore VCR" naming no song; a "Self MV" or
`[공약영상]` (pledge/milestone video) despite its MV-shaped title; a "watch-along"/reaction-style
vlog; a "MV 촬영 브이로그" (shooting vlog) distinct from a literal Behind/Making clip; a "SOLO
CONCERT HISTORY" retrospective/milestone-anniversary clip naming no specific song; a subscriber-
count-milestone-plus-fan-naming-contest video; a "Before shooting MV" pre-shoot clip; an old
Cube/BEAST-era literal `BTS` (Behind The Scenes) mislabel confirmed again on a different artist,
same pattern as the entry already documented above.

**Known open issue, flagged not fixed (out of scope for a grouping pass): a channel-anchor
assignment bug, not a grouping judgment call.** The "After School" channel has a Weeekly song
anchored to it purely because of a coincidental title match with the channel-artist's name - the
anchor assignment itself is wrong at the source (whatever process picked the anchor channel for
that song), not something a merge/excise decision inside this doc's scope can correct. Same class
of issue as the Wikidata `display_name` bugs already documented above (doesn't affect correctness
of grouping, which keys on `channel_id` not `display_name`, but worth fixing at the source
eventually) - flagged here so a future session investigating anchor-assignment logic has a known
repro case.

## How to apply a merge

Keep the LOWEST song_id of the group as the survivor.

    UPDATE videos SET song_id = <survivor> WHERE song_id IN (<others>);
    UPDATE songs SET canonical_title = <shortest title among all the
        group's videos> WHERE song_id = <survivor>;
    DELETE FROM songs WHERE song_id NOT IN
        (SELECT song_id FROM videos WHERE song_id IS NOT NULL);

canonical_title matters: it is what renders on the chart overlay, and the
convention is the shortest title in the group.

## How to excise Making/Behind (or any other non-song content)

Same idea, opposite direction - the video isn't wrong to have in the
registry, it's wrong to have as a song:

    UPDATE videos SET song_id = NULL WHERE video_id IN (<the video_ids>);
    DELETE FROM songs WHERE song_id NOT IN
        (SELECT song_id FROM videos WHERE song_id IS NOT NULL);

Do this per video_id, not per song_id - a Making/Behind video sitting
merged together with real content under one song_id (a deterministic-tier
over-merge) needs only its own row detached, not the whole group wiped.
The same `DELETE FROM songs WHERE ...` cleanup line handles both a group
that fully empties out and one that just loses a member.

Never DELETE from videos. Never touch a song outside your shard.

## Procedure

1. Read your shard's songs and their video titles.
2. Work through it in batches you can reason about carefully (~100 songs).
   Accuracy matters far more than speed here. A shard can easily be
   thousands of songs - that's expected, not a sign to shortcut the
   per-title reading with a script (see above). If it's large, say so
   plainly rather than quietly scoping down.
3. For each batch, list the proposed merges (song_ids, titles, one line
   of reasoning each) and get a yes before applying that batch - don't
   accumulate the whole shard into one giant dry-run before anything
   lands. Once told you have standing permission to apply without
   asking each time, keep doing the same size/quality of batch and just
   report what you applied instead of asking first.
4. Apply each approved batch in its own transaction (survivor
   selection, canonical_title recompute, empty-song cleanup, as below),
   and verify integrity after every batch, not just at the end.
5. This spans more than one session on a large registry. Keep a
   progress file at `data/.grouping-audit/progress.json` (gitignored with
   the rest of `data/.grouping-audit/` - see "Session file layout" above)
   recording which channels are fully reviewed and exactly where you
   stopped in the current one, so a resumed session doesn't re-read
   what's already done or silently skip what isn't. Any other scratch
   from this session (candidate dumps, one-off scripts, reports, shard
   notes) goes under the matching subdirectory there - never as a loose
   `data/.audit_*` / `data/.apply_*` file.
6. Verify and report, both per batch and at the end of a session:

    -- must both be 0
    SELECT COUNT(*) FROM videos v WHERE v.song_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM songs s WHERE s.song_id = v.song_id);
    SELECT COUNT(*) FROM songs s WHERE NOT EXISTS
      (SELECT 1 FROM videos v WHERE v.song_id = s.song_id);

   -- and the video count must be unchanged from when you started
    SELECT COUNT(*) FROM videos;

Report: songs before, songs after, merges applied, and any case you
deliberately left alone because you were not confident.

7. Clean up session scratch before treating the session as done. Files
   under `data/.grouping-audit/candidates/`, `scripts/`, `reports/`, and
   `shards/` are local working notes only - leave them on disk if you
   still need them, but they must never land in this repo (the whole
   `data/.grouping-audit/` tree is gitignored). Delete session scratch
   once the session's merges are verified, so a later agent doesn't
   re-run an already-applied apply script against an updated DB. Keep
   `data/.grouping-audit/progress.json` (durable "already reviewed" log)
   and `state.json` / `log.jsonl` when the headless auditor wrote them.
   Keep timestamped DB backups under `data/` only until after a
   successful push, then delete those too. Any lasting rule or precedent
   belongs in this doc's Precedents section, not in a scratch file.
````

---

## After all shards finish

```bash
python -m unittest discover -s tests -t .
python -c "import sqlite3; print(sqlite3.connect('data/.registry_working.db').execute('pragma integrity_check').fetchone()[0])"
python -c "from registry import db; db.push_from_local()"
```

Confirm every shard has pushed (or push once here after collecting all shards'
local working copies) before treating the pass as done - the data repo, not
any local file, is what the next sync/chart run and every other machine
actually reads. Then delete leftover session scratch under
`data/.grouping-audit/{candidates,scripts,reports,shards}/` and any
pre-merge DB backups you no longer need - same cleanup as Procedure step 7.
Keep `progress.json` (and `state.json` / `log.jsonl` if present).

Cross-shard merges are not attempted by design.
A song split across two anchors is rare and is a symptom of the anchor being wrong, which is worth fixing at the source rather than by hand here.
