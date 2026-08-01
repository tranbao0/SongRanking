"""
Guards a sync against one channel taking the whole run down with it.

A roster of several hundred always contains channels that are deleted,
private, region-blocked, or that have never uploaded - a channel with zero
videos still reports an uploads playlist ID, but asking for that
playlist's items is a 404. A bootstrap that aborts on the first of those
never reaches the remaining hundreds, and the failure is unrecoverable
without a code change.

The exception is running out of quota, where every remaining channel would
fail identically and stopping is correct.

No test reaches the network.
"""

import unittest
from unittest import mock

from .context import make_db, add_channel

from registry import catalog, db, song_grouping


class _FakeResp:
    def __init__(self, status):
        self.status = status
        self.reason = "err"


def _http_error(status, reason_detail):
    """Build something shaped like googleapiclient's HttpError."""
    err = catalog.HttpError(_FakeResp(status), b"{}")
    err.error_details = [{"reason": reason_detail}]
    return err


class SyncResilienceTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        for i in range(3):
            add_channel(self.conn, f"UC_{i}", "kpop", f"Artist {i}", "wikidata")
        self.conn.commit()
        patcher = mock.patch.object(db, "get_connection", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sync(self, status_side_effect, uploads_side_effect=None):
        with mock.patch.object(catalog, "get_client", return_value=mock.Mock()), \
             mock.patch.object(catalog, "_channel_status", side_effect=status_side_effect), \
             mock.patch.object(catalog, "_list_new_uploads",
                               side_effect=uploads_side_effect or (lambda *a: [])), \
             mock.patch.object(catalog, "_fetch_durations", return_value={}), \
             mock.patch.object(song_grouping, "call_gemini", lambda p, model=None: None):
            return catalog.sync_videos()

    def _synced(self):
        return {
            r["channel_id"]
            for r in self.conn.execute(
                "SELECT channel_id FROM channels WHERE last_catalog_sync IS NOT NULL"
            )
        }

    def test_a_channel_with_no_uploads_is_skipped_not_walked(self):
        """
        The reported crash: videoCount 0 still yields a playlist ID, but
        that playlist 404s. It must never be paginated at all.
        """
        walked = []

        def _uploads(youtube, playlist_id, known):
            walked.append(playlist_id)
            return []

        self._sync(lambda yt, cid: ("PL_" + cid, 0), _uploads)
        self.assertEqual(walked, [], "a zero-upload channel must not be paginated")
        self.assertEqual(len(self._synced()), 3)

    def test_a_404_on_one_channel_does_not_end_the_run(self):
        def _uploads(youtube, playlist_id, known):
            if playlist_id == "PL_UC_1":
                raise _http_error(404, "playlistNotFound")
            return []

        self._sync(lambda yt, cid: ("PL_" + cid, 5), _uploads)
        # All three attempted; the broken one recorded so it isn't retried.
        self.assertEqual(self._synced(), {"UC_0", "UC_1", "UC_2"})

    def test_a_private_channel_does_not_end_the_run(self):
        def _uploads(youtube, playlist_id, known):
            if playlist_id == "PL_UC_0":
                raise _http_error(403, "forbidden")
            return []

        self._sync(lambda yt, cid: ("PL_" + cid, 5), _uploads)
        self.assertEqual(self._synced(), {"UC_0", "UC_1", "UC_2"})

    def test_quota_exhaustion_stops_the_run(self):
        """
        The opposite case: every remaining channel would fail the same
        way, so continuing just logs the same error hundreds of times.
        """
        def _uploads(youtube, playlist_id, known):
            if playlist_id == "PL_UC_1":
                raise _http_error(403, "quotaExceeded")
            return []

        self._sync(lambda yt, cid: ("PL_" + cid, 5), _uploads)
        self.assertNotIn("UC_2", self._synced(), "should stop, not carry on")
        self.assertIn("UC_0", self._synced(), "work already done stays committed")

    def test_quota_errors_are_recognised_by_reason(self):
        for reason in ["quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"]:
            with self.subTest(reason=reason):
                self.assertTrue(catalog._is_quota_error(_http_error(403, reason)))
        for reason in ["playlistNotFound", "forbidden", "notFound"]:
            with self.subTest(reason=reason):
                self.assertFalse(catalog._is_quota_error(_http_error(404, reason)))

    def test_a_skipped_channel_costs_one_unit_next_run(self):
        """Recording the count is what makes the next run cheap."""
        self._sync(lambda yt, cid: ("PL_" + cid, 0))
        counts = {
            r["channel_id"]: r["last_known_video_count"]
            for r in self.conn.execute("SELECT channel_id, last_known_video_count FROM channels")
        }
        self.assertEqual(set(counts.values()), {0})


if __name__ == "__main__":
    unittest.main()
