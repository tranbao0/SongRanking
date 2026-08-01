"""
Guards the per-chunk candidate list sent to Gemini.

The full list is one line per song across every channel the caller merged
in, repeated in each chunk's prompt. On a shared channel cross-checking
hundreds of artists that reached ~141k input tokens per call against a
10.5k-song registry, and it grows as the registry fills - the largest
single input cost in a catalogue sync.

Correctness constraint: scoping is by artist anchor, never by text
similarity. A title and its match may share no characters at all (an
alternate romanization or native-script rendering), which is exactly what
tier 3 exists to resolve, so a text filter would defeat the tier it is
meant to make affordable.

No test here reaches the network.
"""

import unittest

from .context import make_db, add_channel

from registry import song_grouping


def _row(video_id, title, song_id=None, song_channel_id=None):
    return {"video_id": video_id, "title": title, "song_id": song_id,
            "song_channel_id": song_channel_id}


class ScopedCandidatesTest(unittest.TestCase):
    def setUp(self):
        self.index = song_grouping.build_artist_index(
            [(n, song_grouping.artist_pattern(n)) for n in ["BTS", "TWICE", "aespa"]]
        )
        self.homes = {"BTS": "UC_bts", "TWICE": "UC_twice", "aespa": "UC_aespa"}
        self.songs = {1: "Dynamite", 2: "Butter", 3: "What is Love", 4: "Supernova"}
        self.anchors = {1: "UC_bts", 2: "UC_bts", 3: "UC_twice", 4: "UC_aespa"}

    def _scope(self, titles, channel_id="UC_shared"):
        chunk = [{"video_id": f"v{i}", "title": t} for i, t in enumerate(titles)]
        return song_grouping._scoped_candidates(
            chunk, self.songs, self.anchors, self.index, self.homes, channel_id,
        )

    def test_only_artists_present_in_the_chunk_are_offered(self):
        self.assertEqual(set(self._scope(["BTS - Some New Song"])), {1, 2})

    def test_multiple_artists_in_a_chunk_union_their_catalogues(self):
        self.assertEqual(set(self._scope(["BTS - New", "TWICE - New"])), {1, 2, 3})

    def test_a_match_with_no_shared_text_is_still_offered(self):
        """
        The case a text-similarity filter would break: the candidate title
        shares nothing with the upload, and resolving that is the whole
        point of the AI tier.
        """
        self.songs[5] = "슈퍼노바"
        self.anchors[5] = "UC_aespa"
        self.assertIn(5, self._scope(["aespa - Supernova (Official MV)"]))

    def test_songs_anchored_to_the_channel_being_synced_are_kept(self):
        self.songs[9] = "Channel Own Song"
        self.anchors[9] = "UC_shared"
        self.assertIn(9, self._scope(["BTS - New"], channel_id="UC_shared"))

    def test_unscoped_when_no_artist_patterns(self):
        """A single-artist channel's list is already small - don't touch it."""
        empty = song_grouping.build_artist_index([])
        chunk = [{"video_id": "v", "title": "Anything"}]
        self.assertEqual(
            song_grouping._scoped_candidates(chunk, self.songs, self.anchors, empty, self.homes, "UC_x"),
            self.songs,
        )

    def test_unscoped_when_anchors_are_unavailable(self):
        chunk = [{"video_id": "v", "title": "BTS - New"}]
        self.assertEqual(
            song_grouping._scoped_candidates(chunk, self.songs, {}, self.index, self.homes, "UC_x"),
            self.songs,
        )

    def test_unscoped_when_chunk_names_no_confirmed_artist(self):
        """Better to over-offer than to silently rule out every candidate."""
        self.assertEqual(self._scope(["Some Unknown Uploader - Track"]), self.songs)

    def test_scoping_shrinks_the_list_substantially(self):
        """The whole point: a chunk shouldn't carry the entire registry."""
        many = {i: f"Song {i}" for i in range(1000)}
        anchors = {i: f"UC_artist{i % 200}" for i in range(1000)}
        homes = {f"A{i}": f"UC_artist{i}" for i in range(200)}
        index = song_grouping.build_artist_index(
            [(f"A{i}", song_grouping.artist_pattern(f"A{i}")) for i in range(200)]
        )
        chunk = [{"video_id": f"v{i}", "title": f"A{i} - New Track"} for i in range(3)]
        scoped = song_grouping._scoped_candidates(chunk, many, anchors, index, homes, "UC_shared")
        self.assertLess(len(scoped), len(many) / 10)


class GroupingStillWorksTest(unittest.TestCase):
    """Scoping must not change what grouping decides, only what it costs."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_channel(self.conn, "UC_bts", "kpop", "BTS", "wikidata")
        add_channel(self.conn, "UC_shared", "kpop", "Broadcast", "manual")
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) "
            "VALUES (1, 'UC_bts', 'Dynamite', '2026-01-01')"
        )
        self.conn.commit()

    def test_new_upload_still_links_to_the_scoped_candidate(self):
        index = song_grouping.build_artist_index(
            [("BTS", song_grouping.artist_pattern("BTS"))]
        )
        existing = [_row("old", "BTS - Dynamite Official MV", song_id=1, song_channel_id="UC_bts")]
        new = [{"video_id": "new", "title": "BTS - Dynamite (Dance Practice)"}]

        result = song_grouping.group_channel_videos(
            self.conn, "UC_shared", existing, new,
            artist_patterns=index, artist_home_channels={"BTS": "UC_bts"},
        )
        self.assertEqual(result["new"], 1)


if __name__ == "__main__":
    unittest.main()
