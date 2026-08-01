"""
Guards the order catalog applies its filters in.

Every rejection that needs only a title has to happen before durations are
fetched, because durations cost a YouTube quota unit per 50 videos - the
same rate as walking the playlist. Getting the order wrong is invisible in
the output (the resulting set is identical either way) and only shows up
as quota burned on answers that are then discarded, which is why it is
pinned here rather than left to review.

No test reaches the network: the API client is stubbed.
"""

import unittest
from unittest import mock

from .context import make_db, add_channel

from registry import catalog, db, song_grouping


class FilterOrderTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)

        # One confirmed artist, and a kworb channel far over the
        # shared-channel threshold - i.e. a broadcast/aggregator archive.
        add_channel(self.conn, "UC_bts", "kpop", "BTS", "wikidata")
        add_channel(self.conn, "UC_broadcast", "kpop", "KBS WORLD TV", "kworb")
        self.conn.execute(
            "UPDATE channels SET last_known_video_count = 0 WHERE channel_id = 'UC_broadcast'"
        )
        self.conn.commit()

        patcher = mock.patch.object(db, "get_connection", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_sync(self, uploads):
        """Sync the broadcast channel with `uploads`, returning fetched duration IDs."""
        fetched = []

        def _fake_durations(youtube, video_ids):
            fetched.extend(video_ids)
            return {vid: 200 for vid in video_ids}  # all inside the MV window

        with mock.patch.object(catalog, "_get_client", return_value=mock.Mock()), \
             mock.patch.object(catalog, "_channel_status", return_value=("PL_x", 9999)), \
             mock.patch.object(catalog, "_list_new_uploads", return_value=uploads), \
             mock.patch.object(catalog, "_fetch_durations", side_effect=_fake_durations), \
             mock.patch.object(song_grouping, "call_gemini", lambda p, model=None: None):
            catalog.sync_videos(["UC_broadcast"])
        return fetched

    @staticmethod
    def _upload(video_id, title):
        return {"video_id": video_id, "title": title, "published_at": "2024-01-01T00:00:00Z"}

    def test_durations_not_fetched_for_titles_without_a_confirmed_artist(self):
        """
        The expensive case: a broadcast archive where most uploads name no
        tracked artist. That check is pure text and must precede the spend.
        """
        uploads = [
            self._upload("keep", "BTS - Dynamite Official MV"),
            self._upload("news", "KBS News Today Full Episode"),
            self._upload("drama", "Korean Drama Highlight Clip"),
            self._upload("talk", "Talk Show Special Guest"),
        ]
        fetched = self._run_sync(uploads)
        self.assertEqual(fetched, ["keep"])

    def test_durations_not_fetched_for_blocklisted_titles(self):
        uploads = [
            self._upload("keep", "BTS - Dynamite Official MV"),
            self._upload("comp", "BTS Best Of Compilation"),
            self._upload("teaser", "BTS - Dynamite Teaser"),
        ]
        self.assertEqual(self._run_sync(uploads), ["keep"])

    def test_surviving_videos_are_still_duration_checked(self):
        """Pre-filtering must not let a too-short clip through."""
        uploads = [self._upload("short", "BTS - Dynamite Official MV")]
        with mock.patch.object(catalog, "_get_client", return_value=mock.Mock()), \
             mock.patch.object(catalog, "_channel_status", return_value=("PL_x", 9999)), \
             mock.patch.object(catalog, "_list_new_uploads", return_value=uploads), \
             mock.patch.object(catalog, "_fetch_durations", return_value={"short": 5}), \
             mock.patch.object(song_grouping, "call_gemini", lambda p, model=None: None):
            catalog.sync_videos(["UC_broadcast"])

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 0)

    def test_the_kept_video_is_actually_stored(self):
        self._run_sync([self._upload("keep", "BTS - Dynamite Official MV")])
        rows = self.conn.execute("SELECT video_id, channel_id FROM videos").fetchall()
        self.assertEqual([(r["video_id"], r["channel_id"]) for r in rows], [("keep", "UC_broadcast")])

    def test_new_song_anchors_to_the_confirmed_artists_own_channel(self):
        """
        What makes an aggregator worth cataloguing: its uploads seed songs
        that the artist's own channel later joins instead of duplicating.
        """
        self._run_sync([self._upload("keep", "BTS - Dynamite Official MV")])
        song = self.conn.execute("SELECT channel_id FROM songs").fetchone()
        self.assertEqual(song["channel_id"], "UC_bts")
        # ...and that anchor is now visible when UC_bts itself syncs.
        self.assertEqual(
            {r["video_id"] for r in catalog._existing_videos(self.conn, "UC_bts")}, {"keep"}
        )


if __name__ == "__main__":
    unittest.main()
