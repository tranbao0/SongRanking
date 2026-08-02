"""
Guards discovery.sync_channels's per-genre commit and failure isolation.

Before this, the whole function committed once at the end and let a
discover_genre() failure propagate - so a Wikidata blip on the second of
two genres would discard the first genre's already-discovered channels too
(never committed) and abort the run instead of just skipping the broken
genre.

No test reaches the network.
"""

import unittest
from unittest import mock

import requests

from .context import make_db

from registry import db, discovery


class SyncChannelsResilienceTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        patcher = mock.patch.object(db, "get_connection", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _channel(self, channel_id, genre):
        return {
            "channel_id": channel_id, "genre": genre, "display_name": channel_id,
            "source": "wikidata", "source_ref": None, "added_date": "2026-01-01",
        }

    def test_a_later_genres_failure_does_not_discard_an_earlier_genres_work(self):
        def _discover(genre):
            if genre == "jpop":
                raise requests.RequestException("boom")
            return [self._channel("UC_kpop", "kpop")]

        with mock.patch.object(discovery, "discover_genre", side_effect=_discover):
            counts = discovery.sync_channels(["kpop", "jpop"])

        self.assertEqual(counts, {"kpop": 1})
        self.assertEqual(
            [r["channel_id"] for r in self.conn.execute("SELECT channel_id FROM channels")],
            ["UC_kpop"],
        )

    def test_a_failed_genre_is_omitted_from_the_result_not_raised(self):
        with mock.patch.object(discovery, "discover_genre", side_effect=requests.RequestException("boom")):
            counts = discovery.sync_channels(["kpop"])
        self.assertEqual(counts, {})

    def test_all_genres_succeed_normally(self):
        with mock.patch.object(discovery, "discover_genre",
                                side_effect=lambda g: [self._channel(f"UC_{g}", g)]):
            counts = discovery.sync_channels(["kpop", "jpop"])
        self.assertEqual(counts, {"kpop": 1, "jpop": 1})


class WikidataRetryTest(unittest.TestCase):
    """discover_channels retries a transient failure before giving up."""

    def test_succeeds_after_a_transient_failure(self):
        from registry.providers import wikidata

        calls = {"n": 0}

        def _get(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("temporary")
            resp = mock.Mock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"results": {"bindings": []}}
            return resp

        with mock.patch("registry.providers.wikidata.requests.get", side_effect=_get), \
             mock.patch("registry.providers.wikidata.time.sleep"):
            result = wikidata.discover_channels("kpop")

        self.assertEqual(result, [])
        self.assertEqual(calls["n"], 2)

    def test_raises_after_exhausting_retries(self):
        from registry.providers import wikidata

        with mock.patch("registry.providers.wikidata.requests.get",
                         side_effect=requests.ConnectionError("down")), \
             mock.patch("registry.providers.wikidata.time.sleep"):
            with self.assertRaises(requests.ConnectionError):
                wikidata.discover_channels("kpop")


if __name__ == "__main__":
    unittest.main()
