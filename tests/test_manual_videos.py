"""
Guards the manual-video pinning path (discovery.py's `<genre>_manual_videos.yaml`
and catalog.py's `_sync_manual_video_channel`), added so a song whose only
upload sits on a channel that's mostly a different genre entirely - a
movie studio, a late-night show - can be tracked without adopting that
channel's whole upload history via the full `<genre>_manual.yaml` path.

No test reaches the network.
"""

import unittest
from unittest import mock

from .context import make_db, add_channel, add_video

from registry import catalog, db, discovery, song_grouping
from shared import api_budget


class ManualVideosParsingTest(unittest.TestCase):
    def test_parses_entries_with_genre_and_default_display_name(self):
        entries = [
            {"video_id": "vid1", "channel_id": "UC_studio", "display_name": "Song A"},
            {"video_id": "vid2", "channel_id": "UC_studio"},
        ]
        with mock.patch.object(discovery, "_load_yaml_list", return_value=entries):
            result = discovery.manual_videos("kpop")
        self.assertEqual(result, [
            {"video_id": "vid1", "channel_id": "UC_studio", "genre": "kpop", "display_name": "Song A"},
            {"video_id": "vid2", "channel_id": "UC_studio", "genre": "kpop", "display_name": "vid2"},
        ])

    def test_no_file_yields_no_entries(self):
        with mock.patch.object(discovery, "_load_yaml_list", return_value=[]):
            self.assertEqual(discovery.manual_videos("kpop"), [])


class DiscoverGenrePinnedChannelTest(unittest.TestCase):
    def _pinned(self, video_id="vid1", channel_id="UC_studio"):
        return [{"video_id": video_id, "channel_id": channel_id, "genre": "kpop", "display_name": "Song A"}]

    def test_a_pinned_videos_channel_gets_a_manual_video_placeholder(self):
        with mock.patch.object(discovery, "manual_videos", return_value=self._pinned()), \
             mock.patch.object(discovery.wikidata, "discover_channels", return_value=[]), \
             mock.patch.object(discovery, "_manual_channels", return_value=[]), \
             mock.patch.object(discovery, "_excluded_channel_ids", return_value=set()):
            result = discovery.discover_genre("kpop")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["channel_id"], "UC_studio")
        self.assertEqual(result[0]["source"], "manual_video")

    def test_a_fully_tracked_channel_is_not_downgraded_to_a_placeholder(self):
        """
        A channel already tracked via Wikidata or the full manual yaml
        already reaches the pinned video through its normal walk, so the
        fuller entry (and its real source) must win.
        """
        full_entry = {
            "channel_id": "UC_studio", "genre": "kpop", "display_name": "Real Artist",
            "source": "wikidata", "source_ref": "Q1", "added_date": "2026-01-01",
        }
        with mock.patch.object(discovery, "manual_videos", return_value=self._pinned()), \
             mock.patch.object(discovery.wikidata, "discover_channels", return_value=[full_entry]), \
             mock.patch.object(discovery, "_manual_channels", return_value=[]), \
             mock.patch.object(discovery, "_excluded_channel_ids", return_value=set()):
            result = discovery.discover_genre("kpop")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "wikidata")

    def test_excluding_a_pinned_channel_still_works(self):
        with mock.patch.object(discovery, "manual_videos", return_value=self._pinned()), \
             mock.patch.object(discovery.wikidata, "discover_channels", return_value=[]), \
             mock.patch.object(discovery, "_manual_channels", return_value=[]), \
             mock.patch.object(discovery, "_excluded_channel_ids", return_value={"UC_studio"}):
            result = discovery.discover_genre("kpop")
        self.assertEqual(result, [])


class _FakeVideosResource:
    """Mimics youtube.videos() for a fixed id->snippet map."""

    def __init__(self, snippets: dict[str, dict]):
        self._snippets = snippets

    def list(self, part, id):
        ids = id.split(",")
        items = [{"id": vid, "snippet": self._snippets[vid]} for vid in ids if vid in self._snippets]
        return mock.Mock(execute=lambda: {"items": items})


def _fake_youtube(snippets: dict[str, dict]):
    return mock.Mock(videos=mock.Mock(return_value=_FakeVideosResource(snippets)))


class SyncManualVideoChannelTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        add_channel(self.conn, "UC_studio", "kpop", "Some Movie Studio", "manual_video")
        self.conn.commit()

        # Must not depend on real accumulated daily quota on disk (see
        # api_budget.USAGE_FILE) - every other sync test avoids this by
        # mocking out the caller that would touch it; this one calls
        # _sync_manual_video_channel directly, which records real usage.
        patcher = mock.patch.object(api_budget, "record_youtube_units")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pinned(self, entries):
        return mock.patch.object(discovery, "manual_videos", return_value=entries)

    def test_fetches_only_the_pinned_video_never_a_playlist(self):
        pinned = [{"video_id": "song1", "channel_id": "UC_studio", "genre": "kpop", "display_name": "Song"}]
        youtube = _fake_youtube({"song1": {"title": "Artist - Song (Official Video)", "publishedAt": "2025-01-01T00:00:00Z"}})

        with self._pinned(pinned), \
             mock.patch.object(catalog, "_channel_status", side_effect=AssertionError("must not list a playlist")):
            added = catalog._sync_manual_video_channel(self.conn, youtube, "UC_studio", "kpop", ["UC_studio"])

        self.assertEqual(added, 1)
        rows = self.conn.execute("SELECT video_id, channel_id, title FROM videos").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["video_id"], "song1")
        self.assertEqual(rows[0]["channel_id"], "UC_studio")

    def test_a_title_mv_filter_would_reject_is_kept_anyway(self):
        """A pinned video is a deliberate choice - mv_filter never runs on it."""
        pinned = [{"video_id": "teaser1", "channel_id": "UC_studio", "genre": "kpop", "display_name": "Song"}]
        youtube = _fake_youtube({"teaser1": {"title": "Official Trailer", "publishedAt": "2025-01-01T00:00:00Z"}})

        with self._pinned(pinned):
            added = catalog._sync_manual_video_channel(self.conn, youtube, "UC_studio", "kpop", ["UC_studio"])

        self.assertEqual(added, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)

    def test_already_known_pinned_video_is_not_refetched(self):
        add_video(self.conn, "song1", "UC_studio", title="Old Title", song_id=None)
        self.conn.commit()
        pinned = [{"video_id": "song1", "channel_id": "UC_studio", "genre": "kpop", "display_name": "Song"}]

        def _boom(*a, **k):
            raise AssertionError("must not call the API for an already-known video")

        youtube = mock.Mock(videos=_boom)
        with self._pinned(pinned):
            added = catalog._sync_manual_video_channel(self.conn, youtube, "UC_studio", "kpop", ["UC_studio"])
        self.assertEqual(added, 0)

    def test_sibling_channels_existing_videos_are_offered_for_grouping(self):
        """
        A performance clip pinned to a different real channel than the
        studio upload should still be able to join the same song - the
        sibling's already-tracked videos must reach group_channel_videos.
        """
        add_channel(self.conn, "UC_talkshow", "kpop", "Some Talk Show", "manual_video")
        self.conn.execute(
            "INSERT INTO songs (song_id, channel_id, canonical_title, grouped_at) "
            "VALUES (1, 'UC_studio', 'Song', '2026-01-01')"
        )
        add_video(self.conn, "studio_upload", "UC_studio", title="Song (Official Video)", song_id=1)
        self.conn.commit()

        pinned = [{"video_id": "live_clip", "channel_id": "UC_talkshow", "genre": "kpop", "display_name": "Song Live"}]
        youtube = _fake_youtube({"live_clip": {"title": "Artist performs Song live", "publishedAt": "2025-02-01T00:00:00Z"}})

        captured = {}
        real_group = song_grouping.group_channel_videos

        def _spy(conn, channel_id, existing_videos, new_videos, **kwargs):
            captured["existing_video_ids"] = {v["video_id"] for v in existing_videos}
            return real_group(conn, channel_id, existing_videos, new_videos, **kwargs)

        with self._pinned(pinned), \
             mock.patch.object(song_grouping, "call_gemini", lambda p, model=None: None), \
             mock.patch.object(catalog.song_grouping, "group_channel_videos", side_effect=_spy):
            catalog._sync_manual_video_channel(
                self.conn, youtube, "UC_talkshow", "kpop", ["UC_studio", "UC_talkshow"],
            )

        self.assertIn("studio_upload", captured["existing_video_ids"])


class SyncVideosSkipsManualVideoChannelsTest(unittest.TestCase):
    """The main sync_videos loop must route "manual_video" rows away from the normal channel walk."""

    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)
        add_channel(self.conn, "UC_normal", "kpop", "Real Act", "wikidata")
        add_channel(self.conn, "UC_studio", "kpop", "Studio", "manual_video")
        self.conn.commit()
        patcher = mock.patch.object(db, "get_connection", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_the_normal_channel_is_walked(self):
        walked = []

        # sync_videos excludes "manual_video" rows before it even asks
        # _prefetch_channel_statuses for status (see catalog.py) - this
        # stands in for that batch and records exactly what it was asked
        # about, same as the old per-channel _channel_status stub did.
        def _prefetch(youtube, channel_ids):
            walked.extend(channel_ids)
            return {cid: (f"PL_{cid}", 0) for cid in channel_ids}

        with mock.patch.object(catalog, "get_client", return_value=mock.Mock()), \
             mock.patch.object(catalog, "_prefetch_channel_statuses", side_effect=_prefetch), \
             mock.patch.object(discovery, "manual_videos", return_value=[]):
            catalog.sync_videos()

        self.assertEqual(walked, ["UC_normal"])
        synced = {
            r["channel_id"] for r in
            self.conn.execute("SELECT channel_id FROM channels WHERE last_catalog_sync IS NOT NULL")
        }
        self.assertEqual(synced, {"UC_normal", "UC_studio"})


if __name__ == "__main__":
    unittest.main()
