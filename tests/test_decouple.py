"""
Guards the decouple command, which clears song assignments so grouping can
be redone.

It is the one operation that can destroy paid-for work. Grouping is
additive, so a video that loses its song_id is not re-grouped by syncing
again - a sync only ever looks at videos it has never seen. Anything the
AI tier decided therefore costs API calls to decide a second time.

What is pinned here: it refuses to act without confirmation, it takes a
backup first, it never touches the videos themselves, and its warning
counts the thing that is actually expensive (multi-video songs) rather
than the total, which is dominated by costless singletons.
"""

import unittest
from unittest import mock

from .context import make_db, add_channel, add_video

from registry import db, decouple


class DecoupleTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        add_channel(self.conn, "UC_k", "kpop", "Kpop Act", "wikidata")
        add_channel(self.conn, "UC_j", "jpop", "Jpop Act", "wikidata")
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) VALUES "
            "(1,'UC_k','Grouped Song','2026-01-01'),"   # 2 videos - a real decision
            "(2,'UC_k','Lonely Song','2026-01-01'),"    # 1 video  - costless
            "(3,'UC_j','Jpop Song','2026-01-01')"       # other genre
        )
        add_video(self.conn, "k1", "UC_k", title="Grouped Song (Official MV)", song_id=1)
        add_video(self.conn, "k2", "UC_k", title="Grouped Song (Dance Practice)", song_id=1)
        add_video(self.conn, "k3", "UC_k", title="Lonely Song (Official MV)", song_id=2)
        add_video(self.conn, "j1", "UC_j", title="Jpop Song (Official MV)", song_id=3)
        self.conn.commit()

        for target in ("get_connection",):
            p = mock.patch.object(db, target, return_value=self.conn)
            p.start()
            self.addCleanup(p.stop)
        # Never write a real backup during tests.
        p = mock.patch.object(decouple.shutil, "copy2")
        self.copy2 = p.start()
        self.addCleanup(p.stop)

    def _counts(self):
        return (
            self.conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
            self.conn.execute("SELECT COUNT(*) FROM videos WHERE song_id IS NOT NULL").fetchone()[0],
            self.conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0],
        )

    def test_summary_counts_the_expensive_part_separately(self):
        s = decouple.summarise(self.conn)
        self.assertEqual(s["grouped_videos"], 4)
        self.assertEqual(s["songs"], 3)
        self.assertEqual(s["multi_video_songs"], 1)   # only song 1 is a real decision
        self.assertEqual(s["videos_in_multi"], 2)

    def test_warning_names_the_cost_of_redoing_ai_work(self):
        text = decouple.describe(decouple.summarise(self.conn), None)
        self.assertIn("API calls", text)
        self.assertIn("1 song(s) currently group 2 video(s)", text)

    def test_cancelling_changes_nothing(self):
        with mock.patch("builtins.input", return_value="no"):
            self.assertEqual(decouple.decouple(), -1)
        self.assertEqual(self._counts(), (4, 4, 3))
        self.copy2.assert_not_called()

    def test_a_bare_yes_does_not_confirm(self):
        """The prompt demands a typed word, not a reflexive y."""
        for answer in ("y", "yes", "Y", ""):
            with self.subTest(answer=answer):
                with mock.patch("builtins.input", return_value=answer):
                    self.assertEqual(decouple.decouple(), -1)
        self.assertEqual(self._counts(), (4, 4, 3))

    def test_confirming_clears_groupings_but_keeps_every_video(self):
        with mock.patch("builtins.input", return_value="decouple"):
            self.assertEqual(decouple.decouple(), 4)
        videos, grouped, songs = self._counts()
        self.assertEqual(videos, 4, "videos must never be deleted")
        self.assertEqual(grouped, 0)
        self.assertEqual(songs, 0)

    def test_a_backup_is_taken_before_writing(self):
        with mock.patch("builtins.input", return_value="decouple"):
            decouple.decouple()
        self.copy2.assert_called_once()
        self.assertIn("pre-decouple", str(self.copy2.call_args[0][1]))

    def test_genre_scope_leaves_other_genres_alone(self):
        decouple.decouple(genre="kpop", assume_yes=True)
        rows = {r["video_id"]: r["song_id"] for r in
                self.conn.execute("SELECT video_id, song_id FROM videos")}
        self.assertIsNone(rows["k1"])
        self.assertIsNone(rows["k3"])
        self.assertEqual(rows["j1"], 3, "jpop grouping must survive a kpop decouple")
        self.assertEqual(
            [r["song_id"] for r in self.conn.execute("SELECT song_id FROM songs")], [3]
        )

    def test_assume_yes_skips_the_prompt(self):
        with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertEqual(decouple.decouple(assume_yes=True), 4)

    def test_nothing_grouped_is_a_no_op_without_prompting(self):
        decouple.decouple(assume_yes=True)
        with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertEqual(decouple.decouple(), 0)


if __name__ == "__main__":
    unittest.main()
