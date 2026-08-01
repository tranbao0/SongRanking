"""
Guards channel exclusion, the mechanism for pruning genre false positives.

A popularity-seeded provider once tagged international acts as kpop,
labelled impeccably by their own labels. No
title-level filter can reject those: an Oasis "Official Lyric Video" looks
exactly like the K-pop uploads we want. The channel has to be pruned, and
the pruning has to reach content already catalogued from it.
"""

import unittest
import unittest.mock

from .context import make_db, add_channel, add_video

from registry import discovery


class PurgeExcludedTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        add_channel(self.conn, "UC_keep", "kpop", "Real Kpop Act", "manual")
        add_channel(self.conn, "UC_drop", "kpop", "Oasis", "manual")
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) "
            "VALUES (1, 'UC_keep', 'Real Song', '2026-01-01'), "
            "       (2, 'UC_drop', 'Wonderwall', '2026-01-01')"
        )
        add_video(self.conn, "keep_vid", "UC_keep", title="Real Song (Official MV)", song_id=1)
        add_video(self.conn, "drop_vid", "UC_drop", title="Oasis - Wonderwall (Official Video)", song_id=2)
        self.conn.execute(
            "INSERT INTO view_snapshots (video_id, snapshot_date, views) "
            "VALUES ('keep_vid', '2026-01-01', 10), ('drop_vid', '2026-01-01', 20)"
        )
        self.conn.commit()

    def _purge(self, excluded):
        with unittest.mock.patch.object(discovery, "_excluded_channel_ids", return_value=excluded):
            n = discovery._purge_excluded(self.conn, "kpop")
        self.conn.commit()
        return n

    def _count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_excluded_channel_and_its_content_are_removed(self):
        self.assertEqual(self._purge({"UC_drop"}), 1)
        self.assertEqual(
            [r["channel_id"] for r in self.conn.execute("SELECT channel_id FROM channels")],
            ["UC_keep"],
        )
        self.assertEqual(self._count("videos"), 1)
        self.assertEqual(self._count("songs"), 1)

    def test_videos_are_removed_not_just_the_channel_row(self):
        """
        Charts read videos joined to channels. Leaving the videos behind
        would keep an excluded channel's uploads charting under the genre
        they were wrongly tagged with.
        """
        self._purge({"UC_drop"})
        self.assertEqual(
            [r["video_id"] for r in self.conn.execute("SELECT video_id FROM videos")], ["keep_vid"]
        )

    def test_snapshots_of_removed_videos_are_cleaned_up(self):
        self._purge({"UC_drop"})
        self.assertEqual(
            [r["video_id"] for r in self.conn.execute("SELECT video_id FROM view_snapshots")],
            ["keep_vid"],
        )

    def test_kept_channel_is_untouched(self):
        self._purge({"UC_drop"})
        self.assertEqual(self._count("videos"), 1)
        self.assertEqual(
            self.conn.execute("SELECT canonical_title FROM songs").fetchone()[0], "Real Song"
        )

    def test_empty_exclude_list_changes_nothing(self):
        self.assertEqual(self._purge(set()), 0)
        self.assertEqual(self._count("channels"), 2)
        self.assertEqual(self._count("videos"), 2)

    def test_a_song_keeping_videos_elsewhere_survives(self):
        """Only songs left with no videos at all are dropped."""
        add_video(self.conn, "shared_vid", "UC_keep", title="Wonderwall Cover (Official MV)", song_id=2)
        self.conn.commit()
        self._purge({"UC_drop"})
        self.assertEqual(self._count("songs"), 2)


class RealExcludeFileTest(unittest.TestCase):
    def test_the_shipped_kpop_exclude_file_parses(self):
        ids = discovery._excluded_channel_ids("kpop")
        self.assertTrue(ids, "kpop_exclude.yaml should list the known genre false positives")
        for channel_id in ids:
            with self.subTest(channel_id=channel_id):
                self.assertTrue(channel_id.startswith("UC"))


if __name__ == "__main__":
    unittest.main()
