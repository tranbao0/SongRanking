"""
Guards catalog.sync_videos's `genres` scope, added so `run.py sync --genre
kpop` actually restricts the catalog/snapshot stages to that genre instead
of silently walking every tracked channel regardless of the flag.

No test reaches the network.
"""

import unittest
from unittest import mock

from .context import make_db, add_channel

from registry import catalog, db


class SyncVideosGenreScopeTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        add_channel(self.conn, "UC_kpop", "kpop", "Kpop Act", "wikidata")
        add_channel(self.conn, "UC_jpop", "jpop", "Jpop Act", "wikidata")
        self.conn.commit()
        patcher = mock.patch.object(db, "get_connection", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sync(self, **kwargs):
        walked = []

        def _status(youtube, channel_id):
            walked.append(channel_id)
            return f"PL_{channel_id}", 0

        with mock.patch.object(catalog, "get_client", return_value=mock.Mock()), \
             mock.patch.object(catalog, "_channel_status", side_effect=_status):
            catalog.sync_videos(**kwargs)
        return walked

    def test_no_scope_walks_every_channel(self):
        self.assertEqual(set(self._sync()), {"UC_kpop", "UC_jpop"})

    def test_genres_narrows_to_that_genre_only(self):
        self.assertEqual(self._sync(genres=["kpop"]), ["UC_kpop"])

    def test_channel_ids_and_genres_intersect(self):
        self.assertEqual(self._sync(channel_ids=["UC_kpop", "UC_jpop"], genres=["jpop"]), ["UC_jpop"])


if __name__ == "__main__":
    unittest.main()
