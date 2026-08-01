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
                      "NewJeans 'Super Shy' Dance Practice", "(G)I-DLE - Queencard"]:
            with self.subTest(title=title):
                self.assertFalse(is_blocked_title(title))
                self.assertTrue(is_valid_mv(title, 200))

    def test_remix_is_not_caught_by_the_mix_blocklist_entry(self):
        """'mix' is word-bounded so 'remix' must survive - it's a real upload."""
        self.assertFalse(is_blocked_title("BTS - Dynamite (Remix)"))
        self.assertTrue(is_blocked_title("Kpop Mix 2024"))

    def test_duration_window_still_applies_to_unblocked_titles(self):
        self.assertFalse(is_valid_mv("BTS - Dynamite", MIN_DURATION - 1))
        self.assertFalse(is_valid_mv("BTS - Dynamite", MAX_DURATION + 1))
        self.assertTrue(is_valid_mv("BTS - Dynamite", MIN_DURATION))
        self.assertTrue(is_valid_mv("BTS - Dynamite", MAX_DURATION))

    def test_missing_duration_defaults_to_rejection(self):
        """catalog passes 0 when a duration lookup failed."""
        self.assertFalse(is_valid_mv("BTS - Dynamite", 0))


if __name__ == "__main__":
    unittest.main()
