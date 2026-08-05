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


def _channel_item(channel_id, playlist_id, count):
    """One channels().list() response item, shaped like the real API's."""
    return {
        "id": channel_id,
        "contentDetails": {"relatedPlaylists": {"uploads": playlist_id}},
        "statistics": {"videoCount": str(count)},
    }


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
        # sync_videos looks channel status up in _prefetch_channel_statuses'
        # returned dict now (see catalog.py), not via a per-channel
        # _channel_status call - this stands in for that batch, answering
        # every requested channel the same way a per-channel call would
        # have. See PrefetchChannelStatusesTest below for the batching
        # itself, and the two tests below this class for the fallback path
        # a channel _prefetch_channel_statuses couldn't answer takes.
        def _prefetch(youtube, channel_ids):
            return {cid: status_side_effect(youtube, cid) for cid in channel_ids}

        with mock.patch.object(catalog, "get_client", return_value=mock.Mock()), \
             mock.patch.object(catalog, "_prefetch_channel_statuses", side_effect=_prefetch), \
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

    def test_quota_exhaustion_during_status_prefetch_stops_before_any_channel(self):
        """
        Quota running out during the upfront batch prefetch itself (see
        _prefetch_channel_statuses) has to degrade the same way a
        QuotaExceededError on this run's very first channel always has -
        zero channels committed, nothing crashes - even though no
        individual channel's own status call ever ran.
        """
        with mock.patch.object(catalog, "get_client", return_value=mock.Mock()), \
             mock.patch.object(catalog, "_prefetch_channel_statuses",
                               side_effect=_http_error(403, "quotaExceeded")):
            result = catalog.sync_videos()
        self.assertEqual(result, 0)
        self.assertEqual(self._synced(), set())

    def test_a_channel_missed_by_the_batch_prefetch_still_gets_processed(self):
        """
        A channel absent from _prefetch_channel_statuses' returned dict
        (its chunk hit a transient, non-quota error) falls back to a
        single _channel_status call rather than being silently skipped
        for the whole run.
        """
        def _prefetch(youtube, channel_ids):
            return {cid: ("PL_" + cid, 5) for cid in channel_ids if cid != "UC_1"}

        with mock.patch.object(catalog, "get_client", return_value=mock.Mock()), \
             mock.patch.object(catalog, "_prefetch_channel_statuses", side_effect=_prefetch), \
             mock.patch.object(catalog, "_channel_status", return_value=("PL_UC_1", 5)) as status, \
             mock.patch.object(catalog, "_list_new_uploads", return_value=[]), \
             mock.patch.object(catalog, "_fetch_durations", return_value={}), \
             mock.patch.object(song_grouping, "call_gemini", lambda p, model=None: None):
            catalog.sync_videos()

        status.assert_called_once_with(mock.ANY, "UC_1")
        self.assertEqual(self._synced(), {"UC_0", "UC_1", "UC_2"})


class PrefetchChannelStatusesTest(unittest.TestCase):
    """
    Guards _prefetch_channel_statuses: the batched replacement for a
    _channel_status call per channel (see catalog.py's module docstring on
    the quota cost that batching avoids). No test reaches the network -
    youtube.channels().list() is a fake queue of canned responses/errors.
    """

    def _fake_client(self, responses):
        queue = list(responses)
        calls = []

        def _list(part=None, id=None):
            calls.append(id)
            resp = queue.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return mock.Mock(execute=mock.Mock(return_value=resp))

        youtube = mock.Mock()
        youtube.channels.return_value.list = _list
        return youtube, calls

    def test_batches_ids_into_chunks_of_50(self):
        channel_ids = [f"UC_{i}" for i in range(120)]
        youtube, calls = self._fake_client([{"items": []}, {"items": []}, {"items": []}])

        catalog._prefetch_channel_statuses(youtube, channel_ids)

        self.assertEqual([len(c.split(",")) for c in calls], [50, 50, 20])

    def test_returns_playlist_and_count_per_channel(self):
        youtube, _ = self._fake_client([
            {"items": [_channel_item("UC_0", "PL_0", 5), _channel_item("UC_1", "PL_1", 10)]},
        ])
        result = catalog._prefetch_channel_statuses(youtube, ["UC_0", "UC_1"])
        self.assertEqual(result, {"UC_0": ("PL_0", 5), "UC_1": ("PL_1", 10)})

    def test_channel_missing_from_the_response_gets_none_none(self):
        """Deleted/private channel - same as _channel_status's own empty-items case."""
        youtube, _ = self._fake_client([{"items": [_channel_item("UC_0", "PL_0", 5)]}])
        result = catalog._prefetch_channel_statuses(youtube, ["UC_0", "UC_1"])
        self.assertEqual(result["UC_1"], (None, None))

    def test_quota_error_propagates_and_stops_further_chunks(self):
        channel_ids = [f"UC_{i}" for i in range(120)]
        youtube, calls = self._fake_client([
            {"items": []},
            _http_error(403, "quotaExceeded"),
            {"items": []},
        ])
        with self.assertRaises(catalog.HttpError):
            catalog._prefetch_channel_statuses(youtube, channel_ids)
        self.assertEqual(len(calls), 2, "must stop at the quota error, never attempting the third chunk")

    def test_non_quota_chunk_error_is_skipped_not_fatal(self):
        channel_ids = [f"UC_{i}" for i in range(50)] + ["UC_50"]
        youtube, calls = self._fake_client([
            _http_error(500, "backendError"),
            {"items": [_channel_item("UC_50", "PL_50", 1)]},
        ])
        result = catalog._prefetch_channel_statuses(youtube, channel_ids)
        self.assertEqual(len(calls), 2, "a non-quota failure must not stop later chunks")
        self.assertNotIn("UC_0", result, "the failed chunk's channels are simply absent, not (None, None)")
        self.assertEqual(result["UC_50"], ("PL_50", 1))


if __name__ == "__main__":
    unittest.main()
