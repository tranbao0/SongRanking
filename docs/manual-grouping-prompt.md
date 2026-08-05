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

The registry is a hosted Turso database, not a local file (see README's "Hosted registry database (Turso)" section) - back it up with a real dump rather than copying a file:

```bash
echo ".dump" | bash scripts/turso_shell.sh > "data/registry-backup-$(date +%Y%m%d-%H%M%S).sql"
```

Run instances **one at a time** unless you've confirmed applying concurrently is safe.
Turso handles concurrent connections properly (no single-writer file lock the way a local SQLite file has), but a failed apply mid-shard is still annoying to unpick if two instances' edits interleave unexpectedly.
Safest pattern: let every instance produce its plan, then apply them in sequence.

---

## The prompt

Paste this into each instance, replacing `SHARD` and `SHARD_COUNT`.

````
You are grouping songs in the local working-copy SQLite file at
data/.registry_working.db (see src/registry/db.py's module docstring).
This is NOT data/registry.db - that path is a pre-Turso-migration
artifact and is stale; writing to it silently loses your work, since
nothing reads it back into the hosted registry. Before you start,
confirm the working copy is current - either it was populated very
recently by a command that calls db.pull_to_local() (sync/decouple/
regroup all do), or pull it yourself:

    python -c "from registry import db; db.pull_to_local()"

When your session's approved batches are applied, push them back so
the hosted registry (the actual source of truth every machine reads)
sees them:

    python -c "from registry import db; db.push_from_local()"

Do this at the end of each session, not just once at the very end of
the whole multi-session pass - an interrupted session (crash, closed
terminal) between pull and push leaves real merge work sitting only in
the local file, invisible to any other instance or machine.

You are instance SHARD of SHARD_COUNT. Work ONLY on your shard.

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
   clusters).
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

MERGE two song rows only when they are the same recording of the same
song, presented differently. Those are: official MV, lyric video, dance
practice, choreography video, performance/encore/live-clip video, plain
audio upload, visualizer.

DO NOT merge, in any of these cases:

- Different arrangements. A remix, acoustic, instrumental, sped-up or
  slowed version is a DIFFERENT song and charts separately. This is the
  single most common trap in this data - "LEMONADE" and "LEMONADE (2Spade
  Remix)" are two songs.
- Different artists, even with identical titles.
- Numbered or qualified variants: "Supernova" and "Supernova 2" differ.
- Any difference at all in native-script (Hangul/Japanese/Chinese) text.
  Treat native script as the most reliable identity signal and compare it
  character by character. If it differs even by one character, they are
  different songs.
- Language versions ("Japanese Ver.", "English Ver.") - different songs.

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
- `(Acoustic Ver.)` / `(Instrumental)` / `(Inst.)` / `(Vocal Ver.)` /
  `(Karaoke Ver.)` / `(Remix)` / named remixes (`(Sara Landry Remix)`,
  `(Champions Remix)`) / `(Sped Up Ver.)` / a cappella - a distinct mix
  or arrangement, even when everything else about the surrounding
  context (same OST, same feat. credit) otherwise matches. NOTE:
  `(Piano Ver.)` was moved OFF this list - see "Decided: merge bare
  (Ver.) tags too" below, which supersedes this for that specific tag.
  This bullet used to list it; if you're skimming and stop here, you'll
  get it backwards.
- A producer/DJ name used as a bare `"(Ver.)"` tag with no "Remix" word
  present (ATEEZ's `BAD (Steve Aoki Ver.)` / `(Ofenbach Ver.)` /
  `(James Carter Ver.)` / `(LEEZ Ver.)` / `(Ollounder Ver.)`, or a
  numbered official remix-EP rollout like `WORK Pt.2 - ATEEZ X Don
  Diablo` / `Pt.3 - ... X Eden-ary` / `Pt.4 - ... X G-Eazy`) - functions
  as a remix credit even without the word "Remix" in the title; keep
  each one split from the plain version and from each other. Contrast
  with a *member's* name in a Ver. tag (`Adrenaline Choreography Demo
  (SAN Ver.)`), which is a per-member cam and merges normally.
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
- Any language-version tag (`(Korean Ver.)`, `(Chinese Ver.)`, `(Japanese
  Ver.)`, `(English Ver.)` as a *sung* MV/Visualizer, or two titles that
  differ only by one carrying native-script text the other lacks, e.g.
  `Love Shot` vs `宣告 (Love Shot)`) - a different recording even without
  an explicit "(Ver.)" tag, and even when the surrounding video format
  (Performance Video, Dance Practice) otherwise matches within that one
  language.
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
presentation unless it names an unambiguous arrangement change
(Remix, Acoustic, Instrumental, Sped Up, Karaoke, Vocal, a cappella -
see the DO-NOT-merge list above, which is the real dividing line).

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
  instance; each is its own individually-recorded take. Left split.

**If you find the deterministic tier over-merged (opposite problem):**
Rare, but happened once (Monsta X's `WHO DO U LOVE? (will.i.am REMIX -
Audio) ft. French Montana` had been auto-merged into the plain `WHO DO U
LOVE?` song, purely because title-normalization strips parentheticals).
This violates the same remix rule as above, just in the other direction.
Fix it: `INSERT INTO songs (channel_id, canonical_title, grouped_at)
VALUES (<same anchor channel_id as the wrongly-merged song>, <the
remix's own title>, strftime('%Y-%m-%dT%H:%M:%f','now'))`, then `UPDATE
videos SET song_id = last_insert_rowid() WHERE video_id = '<that
video>'`. Don't go looking for these proactively across the whole
registry (out of scope for this pass) but fix one if you notice it
while reading a channel.

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
- The same style of named `(Ver.)` tag can mean different things
  depending on *what kind of video* it's attached to. On a Lyric Video or
  Visualizer, a named tag (often a producer's alias) usually functions as
  a remix/producer credit - don't merge, same logic as the existing ATEEZ
  bare-producer-name rule below. The identical-looking tag on a Dance
  Practice or Performance Video usually just names the cut - merge. When
  one channel uses the same tag text both ways, let the video type
  decide, not the tag text.
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
   progress file (e.g. `data/.manual-grouping-progress.json`, gitignored
   like the other sidecar caches in `data/`) recording which channels
   are fully reviewed and exactly where you stopped in the current one,
   so a resumed session doesn't re-read what's already done or silently
   skip what isn't.
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
````

---

## After all shards finish

```bash
python -m unittest discover -s tests -t .
python -c "import sqlite3; print(sqlite3.connect('data/.registry_working.db').execute('pragma integrity_check').fetchone()[0])"
python -c "from registry import db; db.push_from_local()"
```

Confirm every shard has pushed (or push once here after collecting all shards'
local working copies) before treating the pass as done - the hosted Turso
registry, not any local file, is what the next sync/chart run and every other
machine actually reads.

Cross-shard merges are not attempted by design.
A song split across two anchors is rare and is a symptom of the anchor being wrong, which is worth fixing at the source rather than by hand here.
