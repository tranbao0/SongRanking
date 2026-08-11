"""
Guards merge_versions, the one-off migration that merges already-grouped
`songs` rows split apart by a remix/acoustic/language/etc. tag under the
DO-NOT-merge rule song_grouping.py enforced before the 2026-08-10 reversal.

What is pinned here mirrors test_decouple.py's concerns for the same
reason: this also merges view counts across chart entries, so it refuses to
act without confirmation, takes a backup first, never touches videos
directly, and its clustering must respect the same channel-anchor and
artist scoping song_grouping's own matching already does - a merge across
the wrong scope silently fuses two different songs' view counts.
"""

import unittest
from pathlib import Path
from unittest import mock

from .context import make_db, add_channel, add_video

from registry import db, merge_versions


class MergeVersionsTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        add_channel(self.conn, "UC_k", "kpop", "Kpop Act", "wikidata")
        add_channel(self.conn, "UC_j", "jpop", "Jpop Act", "wikidata")
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) VALUES "
            "(1,'UC_k','Dynamite','2026-01-01'),"                 # survives, gains video 3
            "(2,'UC_k','Dynamite (Remix)','2026-01-01'),"         # merges into 1
            "(3,'UC_k','Lonely Song','2026-01-01'),"               # unrelated, stays alone
            "(4,'UC_j','Dynamite','2026-01-01')"                   # other genre/channel, stays alone
        )
        add_video(self.conn, "k1", "UC_k", title="Dynamite (Official MV)", song_id=1)
        add_video(self.conn, "k2", "UC_k", title="Dynamite (Remix)", song_id=2)
        add_video(self.conn, "k3", "UC_k", title="Lonely Song (Official MV)", song_id=3)
        add_video(self.conn, "j1", "UC_j", title="Dynamite (Official MV)", song_id=4)
        self.conn.commit()

        for target in ("get_connection",):
            p = mock.patch.object(db, target, return_value=self.conn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(
            merge_versions, "_write_backup",
            return_value=Path("data/registry.20260101-000000.pre-merge-versions.db"),
        )
        self.write_backup = p.start()
        self.addCleanup(p.stop)

    def _song_ids(self):
        return {r["song_id"] for r in self.conn.execute("SELECT song_id FROM songs")}

    def _video_song_map(self):
        return {r["video_id"]: r["song_id"] for r in self.conn.execute("SELECT video_id, song_id FROM videos")}

    def test_find_clusters_groups_a_remix_with_the_plain_song_only(self):
        clusters = merge_versions.find_clusters(self.conn)
        self.assertEqual(clusters, [[1, 2]])

    def test_cross_channel_songs_never_cluster_even_with_the_same_title(self):
        """Song 4 (UC_j, 'Dynamite') must never merge with song 1 (UC_k,
        'Dynamite') - clustering is anchor-channel-scoped, same as
        song_grouping's own matching."""
        clusters = merge_versions.find_clusters(self.conn)
        merged_ids = {sid for cluster in clusters for sid in cluster}
        self.assertNotIn(4, merged_ids)

    def test_summarise_counts_the_merge(self):
        clusters = merge_versions.find_clusters(self.conn)
        s = merge_versions.summarise(self.conn, clusters)
        self.assertEqual(s["songs_before"], 4)
        self.assertEqual(s["clusters"], 1)
        self.assertEqual(s["songs_after"], 3)
        self.assertEqual(s["videos_touched"], 2)

    def test_cancelling_changes_nothing(self):
        with mock.patch("builtins.input", return_value="no"):
            self.assertEqual(merge_versions.merge_versions(), -1)
        self.assertEqual(self._song_ids(), {1, 2, 3, 4})
        self.write_backup.assert_not_called()

    def test_a_bare_yes_does_not_confirm(self):
        for answer in ("y", "yes", "Y", ""):
            with self.subTest(answer=answer):
                with mock.patch("builtins.input", return_value=answer):
                    self.assertEqual(merge_versions.merge_versions(), -1)
        self.assertEqual(self._song_ids(), {1, 2, 3, 4})

    def test_confirming_merges_lowest_song_id_survives(self):
        with mock.patch("builtins.input", return_value="merge"):
            removed = merge_versions.merge_versions()
        self.assertEqual(removed, 1)
        self.assertEqual(self._song_ids(), {1, 3, 4})  # song 2 gone, nothing else touched

    def test_videos_are_reassigned_to_the_survivor(self):
        with mock.patch("builtins.input", return_value="merge"):
            merge_versions.merge_versions()
        songs = self._video_song_map()
        self.assertEqual(songs["k1"], 1)
        self.assertEqual(songs["k2"], 1)  # was song 2, now merged into 1
        self.assertEqual(songs["k3"], 3)  # unrelated song untouched
        self.assertEqual(songs["j1"], 4)  # other channel untouched

    def test_canonical_title_resets_to_the_shortest_video_title(self):
        """'Dynamite (Remix)' (17 chars) is shorter than 'Dynamite (Official
        MV)' (23 chars) - the convention is literal shortest string, same
        as docs/manual-grouping-prompt.md's "How to apply a merge"."""
        with mock.patch("builtins.input", return_value="merge"):
            merge_versions.merge_versions()
        title = self.conn.execute(
            "SELECT canonical_title FROM songs WHERE song_id = 1"
        ).fetchone()["canonical_title"]
        self.assertEqual(title, "Dynamite (Remix)")

    def test_no_videos_are_deleted(self):
        before = self.conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        with mock.patch("builtins.input", return_value="merge"):
            merge_versions.merge_versions()
        after = self.conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        self.assertEqual(before, after)

    def test_a_backup_is_taken_before_writing(self):
        with mock.patch("builtins.input", return_value="merge"):
            merge_versions.merge_versions()
        self.write_backup.assert_called_once_with()

    def test_assume_yes_skips_the_prompt(self):
        with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertEqual(merge_versions.merge_versions(assume_yes=True), 1)

    def test_nothing_to_merge_is_a_no_op_without_prompting(self):
        merge_versions.merge_versions(assume_yes=True)  # merges the one real cluster
        with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
            self.assertEqual(merge_versions.merge_versions(), 0)

    def test_genre_scope_leaves_other_genres_alone(self):
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) VALUES "
            "(5,'UC_j','Dynamite (Remix)','2026-01-01')"
        )
        add_video(self.conn, "j2", "UC_j", title="Dynamite (Remix)", song_id=5)
        self.conn.commit()

        merge_versions.merge_versions(genre="kpop", assume_yes=True)
        self.assertEqual(self._song_ids(), {1, 3, 4, 5})  # 4/5 (jpop) untouched, 2 merged away


if __name__ == "__main__":
    unittest.main()
