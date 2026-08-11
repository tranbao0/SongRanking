"""
Guards regroup.regroup_all, the companion to decouple that re-derives
groupings for videos decouple left with song_id NULL - without any
YouTube or Gemini API call.

What's pinned here: only NULL-song_id videos are touched, genre scoping,
that confirmed-artist (wikidata) channels are grouped before shared/label
channels so cross-channel matching actually finds something to link to,
that free tiers 1/2 run with use_ai=False (no Gemini), that provisional
singletons are minted so same-run cross-channel tier-1 still works, and
that the CLI agent handoff fires only when new song rows were created.
"""

import unittest
from unittest import mock

from .context import make_db, add_channel, add_video

from registry import db, regroup


class RegroupAllTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        p = mock.patch.object(db, "get_connection", return_value=self.conn)
        p.start()
        self.addCleanup(p.stop)
        # Regroup hands new provisionals to the CLI agent - keep tests offline.
        handoff = mock.patch.object(regroup.grouping_audit, "run_regroup_handoff", return_value=False)
        handoff.start()
        self.addCleanup(handoff.stop)
        self.handoff = regroup.grouping_audit.run_regroup_handoff

    def _song_ids(self):
        return {r["video_id"]: r["song_id"] for r in self.conn.execute("SELECT video_id, song_id FROM videos")}

    def test_only_ungrouped_videos_are_touched(self):
        add_channel(self.conn, "UC_a", "kpop", "Artist A")
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) "
            "VALUES (1, 'UC_a', 'Already Grouped', '2026-01-01')"
        )
        add_video(self.conn, "already", "UC_a", title="Already Grouped (Official MV)", song_id=1)
        add_video(self.conn, "new1", "UC_a", title="Already Grouped (Dance Practice)")
        self.conn.commit()

        grouped = regroup.regroup_all()

        self.assertEqual(grouped, 1)
        song_ids = self._song_ids()
        self.assertEqual(song_ids["already"], 1)
        self.assertEqual(song_ids["new1"], 1)
        # Linked into an existing song - no provisional row for the agent.
        self.handoff.assert_not_called()

    def test_local_tier_merges_same_batch_upload_types(self):
        add_channel(self.conn, "UC_a", "kpop", "Artist A")
        add_video(self.conn, "mv", "UC_a", title="Supernova (Official MV)")
        add_video(self.conn, "dance", "UC_a", title="Supernova (Dance Practice)")
        self.conn.commit()

        regroup.regroup_all()

        song_ids = self._song_ids()
        self.assertIsNotNone(song_ids["mv"])
        self.assertEqual(song_ids["mv"], song_ids["dance"])
        # One new song from a confident local cluster - agent should review.
        self.handoff.assert_called_once()
        self.assertEqual(self.handoff.call_args.kwargs["new_song_count"], 1)

    def test_unresolved_titles_become_provisional_songs_for_cli_agent(self):
        add_channel(self.conn, "UC_a", "kpop", "Artist A")
        add_video(self.conn, "a", "UC_a", title="Song A (Official MV)")
        add_video(self.conn, "b", "UC_a", title="Song B (Official MV)")
        self.conn.commit()

        grouped = regroup.regroup_all()

        self.assertEqual(grouped, 2)
        song_ids = self._song_ids()
        self.assertNotEqual(song_ids["a"], song_ids["b"])
        self.handoff.assert_called_once()
        self.assertEqual(self.handoff.call_args.kwargs["new_song_count"], 2)

    def test_genre_scoping_leaves_other_genres_ungrouped(self):
        add_channel(self.conn, "UC_k", "kpop", "Kpop Act")
        add_channel(self.conn, "UC_j", "jpop", "Jpop Act")
        add_video(self.conn, "k1", "UC_k", title="Kpop Song (Official MV)")
        add_video(self.conn, "k2", "UC_k", title="Kpop Song (Dance Practice)")
        add_video(self.conn, "j1", "UC_j", title="Jpop Song (Official MV)")
        add_video(self.conn, "j2", "UC_j", title="Jpop Song (Dance Practice)")
        self.conn.commit()

        grouped = regroup.regroup_all(genre="kpop")

        self.assertEqual(grouped, 2)
        song_ids = self._song_ids()
        self.assertEqual(song_ids["k1"], song_ids["k2"])
        self.assertIsNone(song_ids["j1"])
        self.assertIsNone(song_ids["j2"])

    def test_wikidata_channels_are_grouped_before_shared_channels(self):
        """
        A shared/label channel's video naming a confirmed artist should
        link to that artist's own already-grouped song instead of minting
        a duplicate - which only works if the artist's own channel is
        processed first. channel_id is chosen so alphabetical order would
        get this wrong, to pin that source ordering is what actually
        decides it.
        """
        add_channel(self.conn, "UC_z_artist", "kpop", "Solo Artist", "wikidata")
        add_channel(self.conn, "UC_a_label", "kpop", "Big Label", "manual")
        self.conn.execute(
            "UPDATE channels SET last_known_video_count = 1000 WHERE channel_id = 'UC_a_label'"
        )
        add_video(self.conn, "own", "UC_z_artist", title="Solo Artist - Firework (Official MV)")
        add_video(self.conn, "label_upload", "UC_a_label", title="Solo Artist - Firework (Dance Practice)")
        self.conn.commit()

        regroup.regroup_all()

        song_ids = self._song_ids()
        self.assertIsNotNone(song_ids["own"])
        self.assertEqual(song_ids["own"], song_ids["label_upload"])
        row = self.conn.execute(
            "SELECT channel_id FROM songs WHERE song_id = ?", (song_ids["own"],)
        ).fetchone()
        self.assertEqual(row["channel_id"], "UC_z_artist")

    def test_rerunning_is_a_no_op(self):
        add_channel(self.conn, "UC_a", "kpop", "Artist A")
        add_video(self.conn, "a", "UC_a", title="Song A (Official MV)")
        add_video(self.conn, "b", "UC_a", title="Song A (Dance Practice)")
        self.conn.commit()

        first = regroup.regroup_all()
        second = regroup.regroup_all()

        self.assertEqual(first, 2)
        self.assertEqual(second, 0)

    def test_no_ungrouped_videos_is_a_clean_no_op(self):
        add_channel(self.conn, "UC_a", "kpop", "Artist A")
        self.conn.commit()

        self.assertEqual(regroup.regroup_all(), 0)
        self.handoff.assert_not_called()

    def test_does_not_call_gemini(self):
        add_channel(self.conn, "UC_a", "kpop", "Artist A")
        add_video(self.conn, "a", "UC_a", title="Unique Title A")
        self.conn.commit()

        with mock.patch.object(regroup.song_grouping, "call_gemini") as call_gemini:
            regroup.regroup_all()
        call_gemini.assert_not_called()


if __name__ == "__main__":
    unittest.main()
