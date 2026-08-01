"""
Guards catalog._confirmed_artists, which merged two queries over the same
Wikidata-confirmed channel rows into one.

Merging them is only safe if the surviving row order is the same, since
that is what decides which channel_id wins when two confirmed channels
share a display_name. Both original queries are kept below as oracles.
"""

import unittest

from .context import make_db, add_channel, add_video

from registry import catalog, song_grouping


def _reference_patterns_by_genre(conn):
    """
    The pre-merge _confirmed_artists_by_genre. Do not optimise.

    Compiles via song_grouping.artist_pattern rather than the r"\\b...\\b"
    the original used - the anchoring changed separately, to fix names
    whose first or last character isn't a word character (see
    test_song_grouping). What this oracle exists to pin is the grouping
    and row ordering, which the query merge could have disturbed.
    """
    rows = conn.execute("SELECT genre, display_name FROM channels WHERE source = 'wikidata'").fetchall()
    patterns = {}
    for row in rows:
        name = row["display_name"].strip()
        if not name:
            continue
        patterns.setdefault(row["genre"], []).append((name, song_grouping.artist_pattern(name)))
    return patterns


def _reference_home_channels(conn):
    """The pre-merge _official_channel_by_artist. Do not optimise."""
    rows = conn.execute("SELECT channel_id, display_name FROM channels WHERE source = 'wikidata'").fetchall()
    return {row["display_name"].strip(): row["channel_id"] for row in rows if row["display_name"].strip()}


def _names(patterns_by_genre):
    return {genre: [name for name, _ in pairs] for genre, pairs in patterns_by_genre.items()}


class ExistingVideosTest(unittest.TestCase):
    """
    A song first catalogued from an aggregator or broadcast channel is
    anchored to the artist's own channel, but its video row stays on the
    aggregator. Matching only by videos.channel_id would never surface it,
    so the artist's own upload of that song would start a second song -
    and on a bootstrap the aggregators are catalogued first, so this is
    the common order rather than an edge case.
    """

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_channel(self.conn, "UC_aggregator", display_name="1theK", source="kworb")
        add_channel(self.conn, "UC_artist", display_name="BTS")
        # Song discovered via the aggregator, anchored to the artist.
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) "
            "VALUES (1, 'UC_artist', 'Dynamite', '2026-01-01')"
        )
        add_video(self.conn, "agg_vid", "UC_aggregator", title="[MV] BTS - Dynamite", song_id=1)
        self.conn.commit()

    def _ids(self, channel_id):
        return {r["video_id"] for r in catalog._existing_videos(self.conn, channel_id)}

    def test_artist_channel_sees_songs_anchored_to_it(self):
        """The regression: without this the artist's own MV duplicates the song."""
        self.assertEqual(self._ids("UC_artist"), {"agg_vid"})

    def test_channel_still_sees_its_own_videos(self):
        add_video(self.conn, "own_vid", "UC_artist", title="BTS - Butter")
        self.conn.commit()
        self.assertEqual(self._ids("UC_artist"), {"agg_vid", "own_vid"})

    def test_aggregator_still_sees_the_video_it_hosts(self):
        self.assertEqual(self._ids("UC_aggregator"), {"agg_vid"})

    def test_a_video_matching_both_halves_is_returned_once(self):
        """UNION, not UNION ALL - a duplicate row would skew canonical-title choice."""
        add_video(self.conn, "both", "UC_artist", title="BTS - Dynamite (Dance Practice)", song_id=1)
        self.conn.commit()
        rows = catalog._existing_videos(self.conn, "UC_artist")
        self.assertEqual(len(rows), len({r["video_id"] for r in rows}))

    def test_unrelated_channels_are_not_pulled_in(self):
        add_channel(self.conn, "UC_other", display_name="Other")
        add_video(self.conn, "other_vid", "UC_other")
        self.conn.commit()
        self.assertNotIn("other_vid", self._ids("UC_artist"))

    def test_ungrouped_videos_do_not_leak_across_channels(self):
        """song_id NULL must not join every other ungrouped video."""
        add_video(self.conn, "loose_agg", "UC_aggregator", title="Loose One")
        add_video(self.conn, "loose_art", "UC_artist", title="Loose Two")
        self.conn.commit()
        self.assertEqual(self._ids("UC_artist"), {"agg_vid", "loose_art"})

    def test_lookup_uses_an_index_rather_than_scanning_videos(self):
        plan = " ".join(
            row["detail"] for row in self.conn.execute(
                "EXPLAIN QUERY PLAN " + catalog._EXISTING_VIDEOS_SQL, {"channel_id": "UC_artist"}
            )
        )
        self.assertNotIn("SCAN videos", plan)


class ConfirmedArtistsTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_matches_the_two_queries_it_replaced(self):
        add_channel(self.conn, "UC_bts", "kpop", "BTS", "wikidata")
        add_channel(self.conn, "UC_twice", "kpop", "TWICE", "wikidata")
        add_channel(self.conn, "UC_perfume", "jpop", "Perfume", "wikidata")
        add_channel(self.conn, "UC_kworb", "kpop", "Some Label", "kworb")
        add_channel(self.conn, "UC_manual", "kpop", "Manual Pick", "manual")
        add_channel(self.conn, "UC_blank", "kpop", "   ", "wikidata")
        self.conn.commit()

        patterns, home_channels = catalog._confirmed_artists(self.conn)
        self.assertEqual(_names(patterns), _names(_reference_patterns_by_genre(self.conn)))
        self.assertEqual(home_channels, _reference_home_channels(self.conn))

    def test_duplicate_display_names_resolve_the_same_way_as_before(self):
        """
        Two confirmed channels sharing a name is the case where merging the
        queries could silently change which channel a song anchors to.
        """
        add_channel(self.conn, "UC_first", "kpop", "Shared Name", "wikidata")
        add_channel(self.conn, "UC_second", "kpop", "Shared Name", "wikidata")
        self.conn.commit()

        _, home_channels = catalog._confirmed_artists(self.conn)
        self.assertEqual(home_channels, _reference_home_channels(self.conn))
        self.assertEqual(home_channels["Shared Name"], "UC_second")  # last row wins, as before

    def test_only_wikidata_channels_are_confirmed(self):
        """kworb is a popularity seed, not a genre roster - it must not confirm."""
        add_channel(self.conn, "UC_kworb", "kpop", "Label Channel", "kworb")
        add_channel(self.conn, "UC_manual", "kpop", "Manual Pick", "manual")
        self.conn.commit()

        patterns, home_channels = catalog._confirmed_artists(self.conn)
        self.assertEqual(patterns, {})
        self.assertEqual(home_channels, {})

    def test_blank_display_names_are_skipped(self):
        add_channel(self.conn, "UC_blank", "kpop", "   ", "wikidata")
        self.conn.commit()

        patterns, home_channels = catalog._confirmed_artists(self.conn)
        self.assertEqual(patterns, {})
        self.assertEqual(home_channels, {})

    def test_patterns_are_grouped_per_genre(self):
        add_channel(self.conn, "UC_bts", "kpop", "BTS", "wikidata")
        add_channel(self.conn, "UC_perfume", "jpop", "Perfume", "wikidata")
        self.conn.commit()

        patterns, _ = catalog._confirmed_artists(self.conn)
        self.assertEqual(_names(patterns), {"kpop": ["BTS"], "jpop": ["Perfume"]})

    def test_names_with_regex_metacharacters_are_escaped_and_matchable(self):
        """Real display_names include things like (G)I-DLE, f(x) and MAMAMOO+."""
        for channel_id, name, hit, miss in [
            ("UC_gidle", "(G)I-DLE", "[(G)I-DLE] Queencard (Official MV)", "GIDLE Queencard"),
            ("UC_fx", "f(x)", "f(x) 4 Walls (Official MV)", "fx 4 Walls"),
            ("UC_mmm", "MAMAMOO+", "MAMAMOO+ - HIP", "MAMAMOO - HIP"),
        ]:
            with self.subTest(name=name):
                add_channel(self.conn, channel_id, "kpop", name, "wikidata")
                self.conn.commit()
                patterns, _ = catalog._confirmed_artists(self.conn)
                pattern = dict(patterns["kpop"])[name]
                self.assertTrue(pattern.search(hit))
                self.assertFalse(pattern.search(miss))


if __name__ == "__main__":
    unittest.main()
