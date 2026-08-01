"""
Guards catalog._confirmed_artists, which merged two queries over the same
Wikidata-confirmed channel rows into one.

Merging them is only safe if the surviving row order is the same, since
that is what decides which channel_id wins when two confirmed channels
share a display_name. Both original queries are kept below as oracles.
"""

import unittest

from .context import make_db, add_channel

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
