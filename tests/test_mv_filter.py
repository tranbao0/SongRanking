"""
Guards the split of is_valid_mv into a title-only half.

catalog.py now rejects blocklisted titles before spending a YouTube quota
unit learning their duration, so the two halves must stay consistent: any
title is_blocked_title rejects has to be one is_valid_mv would have
rejected too, at every possible duration.
"""

import unittest

from . import context  # noqa: F401  (puts src/ on sys.path)

from shared import mv_filter
from shared.mv_filter import MAX_DURATION, MIN_DURATION, is_blocked_title, is_valid_mv


class BlockedTitleTest(unittest.TestCase):
    def test_pre_filtering_never_changes_the_outcome(self):
        """
        The property the quota saving rests on: dropping blocked titles
        early must be indistinguishable from filtering them at the end.
        """
        titles = [
            "BTS - Dynamite (Official MV)", "Top 10 Kpop Songs 2024",
            "aespa 'Supernova' Teaser", "Best of BLACKPINK", "kpop mix 2024",
            "NewJeans 'Super Shy' Dance Practice", "Full Album Stream",
            "Greatest Hits Collection", "TWICE - The Feels M/V", "Song Mash-Up",
            "(G)I-DLE - Queencard", "IVE LOG Vlog", "Making Film 1",
        ]
        for title in titles:
            for duration in (0, MIN_DURATION - 1, MIN_DURATION, 200, MAX_DURATION, MAX_DURATION + 1):
                with self.subTest(title=title, duration=duration):
                    end_filtered = is_valid_mv(title, duration)
                    pre_filtered = (not is_blocked_title(title)) and is_valid_mv(title, duration)
                    self.assertEqual(end_filtered, pre_filtered)

    def test_blocked_titles_can_never_be_valid(self):
        for title in ["Top 10 Kpop Songs", "Best of BLACKPINK", "kpop mix 2024",
                      "aespa Teaser", "Full Album", "Greatest Hits", "Mash-Up", "Compilation"]:
            with self.subTest(title=title):
                self.assertTrue(is_blocked_title(title))
                self.assertFalse(is_valid_mv(title, 200))

    def test_ordinary_mv_titles_are_not_blocked(self):
        for title in ["BTS - Dynamite (Official MV)", "aespa 에스파 'Supernova' MV",
                      "NewJeans 'Super Shy' Dance Practice", "IU 'Knees' Lyric Video",
                      "SEVENTEEN 'God of Music' Special Performance Video",
                      "TWICE 'The Feels' Encore Stage", "[Live Clip] Mad Clown - Kong"]:
            with self.subTest(title=title):
                self.assertFalse(is_blocked_title(title))
                self.assertTrue(is_valid_mv(title, 200))

    def test_titles_naming_no_song_focal_type_are_rejected(self):
        """
        The allowlist's deliberate tradeoff. Unmarked titles were measured
        to be majority non-song content (vlogs, photo-shoot sketches, album
        intros, variety segments), so they are rejected - at the cost of
        also losing genuine bare "Artist - Song" and numbered album-track
        uploads. Pinned so the tradeoff can't be reversed unknowingly.
        """
        for title in ["(G)I-DLE - Queencard", "BTS - Dynamite", "01. REBEL HEART",
                      "[Real 2PM] Fun Photo Shoot Spot Together",
                      "AMBER 엠버 'Beautiful' Special Video"]:
            with self.subTest(title=title):
                self.assertTrue(is_blocked_title(title))
                self.assertFalse(is_valid_mv(title, 200))

    def test_promotional_formats_are_rejected(self):
        for title in ["ENHYPEN 'THE SIN' Trailer vol.1 Film",
                      "f(x) 'NU ABO' Making Film", "IVE LOG WONYOUNG in SPAIN Vlog",
                      "BABYMONSTER 'HOT SAUCE' RECORDING BEHIND THE SCENES",
                      "EXO Drama Episode #2", "Cast Interview", "Concept Photo Clip",
                      "SMTOWN Recap Video", "Album Unboxing"]:
            with self.subTest(title=title):
                self.assertTrue(is_blocked_title(title))

    def test_highlight_the_group_is_not_caught_by_the_highlight_rule(self):
        """
        'Highlight' is a K-pop group. The rule is phrase-anchored to
        highlight clip/medley/video/reel so the group keeps its catalogue.
        """
        self.assertFalse(is_blocked_title("Highlight - Alone (Official MV)"))
        self.assertTrue(is_blocked_title("2024 Comeback Highlight Medley"))

    def test_remix_is_not_caught_by_the_mix_blocklist_entry(self):
        """'mix' is word-bounded so 'remix' must survive - it's a real upload."""
        self.assertFalse(is_blocked_title("BTS - Dynamite (Remix) Official MV"))
        self.assertTrue(is_blocked_title("Kpop Mix 2024 Official MV"))

    def test_duration_window_still_applies_to_unblocked_titles(self):
        t = "BTS - Dynamite (Official MV)"
        self.assertFalse(is_valid_mv(t, MIN_DURATION - 1))
        self.assertFalse(is_valid_mv(t, MAX_DURATION + 1))
        self.assertTrue(is_valid_mv(t, MIN_DURATION))
        self.assertTrue(is_valid_mv(t, MAX_DURATION))

    def test_a_full_concert_film_is_still_rejected_by_duration(self):
        """Live formats are allowed, but only at single-song length."""
        self.assertTrue(is_valid_mv("[Live Clip] Artist - Song", 300))
        self.assertFalse(is_valid_mv("SMTOWN LIVE Concert Stage", 7200))

    def test_award_show_live_performances_are_not_blocked(self):
        """
        "Live at/from <event>" names an award-show or broadcast performance
        the same way "Live Clip" or "Performance" does, just without either
        of those exact words - it should survive equally.
        """
        for title in ["ROSÉ & Bruno Mars - APT. (Live at the 68th Annual Grammy Awards)",
                      "ROSÉ & Bruno Mars - APT. (live from 2024 MAMA AWARDS)",
                      "ROSÉ & PSY - APT. (live from SUMMERSWAG 2025)"]:
            with self.subTest(title=title):
                self.assertFalse(is_blocked_title(title))
                self.assertTrue(is_valid_mv(title, 200))

    def test_bare_live_without_a_venue_is_still_rejected(self):
        """Only "live at/from/in" is allowlisted - a bare "Live" is not."""
        self.assertTrue(is_blocked_title("BABYMONSTER Live Q&A"))

    def test_missing_duration_defaults_to_rejection(self):
        """catalog passes 0 when a duration lookup failed."""
        self.assertFalse(is_valid_mv("BTS - Dynamite (Official MV)", 0))


if __name__ == "__main__":
    unittest.main()
