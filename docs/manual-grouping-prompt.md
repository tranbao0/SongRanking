# Manual song-grouping prompt

For grouping the registry's songs with several Claude Code instances instead of Gemini.

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

```bash
cp data/registry.db data/registry.db.bak
```

Run instances **one at a time** unless you've confirmed the DB isn't locked.
SQLite allows one writer; concurrent writes fail with `database is locked` rather than corrupting anything, but a failed apply mid-shard is annoying to unpick.
Safest pattern: let every instance produce its plan, then apply them in sequence.

---

## The prompt

Paste this into each instance, replacing `SHARD` and `SHARD_COUNT`.

````
You are grouping songs in a local SQLite music registry at data/registry.db.
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

## How to apply a merge

Keep the LOWEST song_id of the group as the survivor.

    UPDATE videos SET song_id = <survivor> WHERE song_id IN (<others>);
    UPDATE songs SET canonical_title = <shortest title among all the
        group's videos> WHERE song_id = <survivor>;
    DELETE FROM songs WHERE song_id NOT IN
        (SELECT song_id FROM videos WHERE song_id IS NOT NULL);

canonical_title matters: it is what renders on the chart overlay, and the
convention is the shortest title in the group.

Never DELETE from videos. Never touch a song outside your shard.

## Procedure

1. Read your shard's songs and their video titles.
2. Work through it in batches you can reason about carefully (~100 songs).
   Accuracy matters far more than speed here.
3. Produce a dry-run list first: for each proposed merge, print the
   song_ids, their titles, and one line on why they are the same song.
   Show it to me before applying anything.
4. After I approve, apply the merges in a single transaction.
5. Verify and report:

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
python -c "import sqlite3; print(sqlite3.connect('data/registry.db').execute('pragma integrity_check').fetchone()[0])"
```

Cross-shard merges are not attempted by design.
A song split across two anchors is rare and is a symptom of the anchor being wrong, which is worth fixing at the source rather than by hand here.
