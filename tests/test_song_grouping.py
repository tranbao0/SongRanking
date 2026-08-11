"""
Guards song_grouping's artist matching, which gained a combined-alternation
prefilter and a per-title memo.

Two things must not have moved:
  - which artist a title resolves to, since that feeds the match key and so
    decides whether two uploads become one song;
  - how many Gemini requests a sync makes, since grouping is additive and a
    call that doesn't happen (or fails) leaves videos permanently
    ungrouped rather than being retried later.

No test here reaches the network: call_gemini is always patched out.
"""

import random
import re
import unittest
from unittest import mock

from .context import make_db, add_channel

from registry import song_grouping
from registry.song_grouping import normalize_title


def _reference_matched_artist(title, patterns):
    """The pre-index implementation, kept as an oracle. Do not optimise."""
    for name, pattern in patterns:
        if pattern.search(title):
            return name
    return None


def _compile(names):
    return [(n, song_grouping.artist_pattern(n)) for n in names]


# Chosen to cover what real display_names actually contain: regex
# metacharacters, names whose first or last character isn't a word
# character, one name being a substring of another, case variation, and
# non-ASCII scripts.
_ARTIST_NAMES = [
    "BTS", "IU", "IU BAND", "(G)I-DLE", "f(x)", "IZ*ONE", "NCT 127",
    "aespa", "에스파", "TWICE", "Red Velvet", "Velvet", "PSY", "NewJeans",
    "MAMAMOO+", "100%", "ICHILLIN'",
]


class ArtistIndexTest(unittest.TestCase):
    def test_matches_reference_on_randomised_titles(self):
        rng = random.Random(20260801)
        patterns = _compile(_ARTIST_NAMES)
        index = song_grouping.build_artist_index(patterns)

        fragments = _ARTIST_NAMES + [
            "Official MV", "Dance Practice", "Supernova", "2024", "|", "-",
            "Making Film", "feat.", "그룹", "bts", "iu", "nobody",
        ]
        titles = [
            " ".join(rng.choice(fragments) for _ in range(rng.randint(1, 6)))
            for _ in range(3000)
        ]
        titles += ["", "   ", "(((", "BTSX", "XIU", "IU'S SONG", "IU-BAND"]

        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(index.match(title), _reference_matched_artist(title, patterns))

    def test_prefilter_agrees_with_any_pattern_matching(self):
        """
        The catalogue filter switched from any(p.search(...)) to
        `index.match(...) is not None`; those must be the same predicate.
        """
        patterns = _compile(_ARTIST_NAMES)
        index = song_grouping.build_artist_index(patterns)
        for title in ["BTS - Dynamite", "unrelated upload", "f(x) 4 Walls",
                      "에스파 Supernova", "IZ*ONE La Vie en Rose", "", "BTSX only"]:
            with self.subTest(title=title):
                self.assertEqual(
                    index.match(title) is not None,
                    any(p.search(title) for _, p in patterns),
                )

    def test_memo_returns_consistent_results(self):
        index = song_grouping.build_artist_index(_compile(_ARTIST_NAMES))
        title = "BTS - Dynamite (Official MV)"
        self.assertEqual(index.match(title), index.match(title))
        self.assertEqual(index.match(title), "BTS")

    def test_empty_index_is_falsy_and_never_matches(self):
        """Non-shared channels pass no patterns; that must stay the cheap path."""
        index = song_grouping.build_artist_index([])
        self.assertFalse(index)
        self.assertIsNone(index.match("BTS - Dynamite"))

    def test_build_artist_index_is_idempotent(self):
        index = song_grouping.build_artist_index(_compile(_ARTIST_NAMES))
        self.assertIs(song_grouping.build_artist_index(index), index)

    def test_names_ending_in_a_non_word_character_are_matchable(self):
        """
        Regression test. These were previously unmatchable: \\b after "+"
        finds no word/non-word transition, so r"\\bMAMAMOO\\+\\b" never
        matched "MAMAMOO+ - HIP" and that artist's uploads were filtered
        off shared channels entirely.
        """
        index = song_grouping.build_artist_index(_compile(_ARTIST_NAMES))
        for title, expected in [
            ("MAMAMOO+ - HIP (Official MV)", "MAMAMOO+"),
            ("f(x) 4 Walls (Official MV)", "f(x)"),
            ("100% - Better Day", "100%"),
            ("ICHILLIN' - Fresh", "ICHILLIN'"),
            ("[(G)I-DLE] Queencard (Official MV)", "(G)I-DLE"),
        ]:
            with self.subTest(title=title):
                self.assertEqual(index.match(title), expected)

    def test_names_glued_to_a_longer_word_still_do_not_match(self):
        """The looser anchoring must not start matching substrings."""
        index = song_grouping.build_artist_index(_compile(_ARTIST_NAMES))
        for title in ["BTSX - Not Them", "XIU - Different", "f(x)4WALLS", "100%COTTON"]:
            with self.subTest(title=title):
                self.assertIsNone(index.match(title))

    def test_word_edged_names_anchor_exactly_as_word_boundaries_did(self):
        """
        The other 623 of 631 confirmed artists start and end with word
        characters, and for those the new anchoring must be a no-op.
        """
        for name in ["BTS", "IU", "aespa", "에스파", "NCT 127", "NewJeans"]:
            old = re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
            new = song_grouping.artist_pattern(name)
            for title in [f"{name} - Song (Official MV)", f"X{name}Y", f"[{name}] Song",
                          f"song by {name}", name, f"{name}!", f"({name})"]:
                with self.subTest(name=name, title=title):
                    self.assertEqual(bool(new.search(title)), bool(old.search(title)))

    def test_ordered_scan_still_decides_collaborations(self):
        """
        On a title naming two confirmed artists, list order wins - not
        position in the title. The prefilter must not change that.
        """
        patterns = _compile(["TWICE", "BTS"])
        index = song_grouping.build_artist_index(patterns)
        title = "BTS X TWICE - Collab Stage"
        self.assertEqual(index.match(title), "TWICE")
        self.assertEqual(index.match(title), _reference_matched_artist(title, patterns))


class GroupPromptTest(unittest.TestCase):
    """
    Pins two grouping rules that are decisions rather than mechanics, so
    they can't be edited out of the prompt unnoticed.
    """

    def test_rearranged_versions_are_declared_the_same_song(self):
        """
        2026-08-10 rule reversal: a remix/acoustic/instrumental/language
        version now merges with the original instead of staying split -
        see song_grouping's module docstring. The general "different
        song" rule (native-script mismatch, a numbered sequel) must still
        be present, just no longer tied to these terms.
        """
        prompt = song_grouping._GROUP_PROMPT.lower()
        for term in ("remix", "acoustic", "instrumental"):
            self.assertIn(term, prompt)
        self.assertIn("same song", prompt)
        self.assertIn("different song", prompt)  # still true for the general case

    def test_behind_the_scenes_content_is_excluded(self):
        prompt = song_grouping._GROUP_PROMPT.lower()
        self.assertIn("behind-the-scenes", prompt)
        self.assertIn("singleton", prompt)

    def test_tags_are_declared_a_hint_not_authoritative(self):
        """
        Tags must never be allowed to override what the title (especially
        native-script text) says - see song_grouping's module docstring on
        why only the AI tier, never a deterministic tier, reads them at all.
        """
        prompt = song_grouping._GROUP_PROMPT.lower()
        self.assertIn("tags", prompt)
        self.assertIn("hint", prompt)


class VideoTagsLabelTest(unittest.TestCase):
    """
    _video_tags_label formats a video's tags for the AI prompt only - see
    song_grouping's module docstring for why this stays out of every
    deterministic tier's matching key.
    """

    def test_json_encoded_tags_are_parsed_and_joined(self):
        v = {"title": "x", "tags": '["Dynamite", "BTS", "Official"]'}
        self.assertEqual(song_grouping._video_tags_label(v), "Dynamite, BTS, Official")

    def test_a_list_is_accepted_directly(self):
        """A video dict built fresh this sync run (not yet round-tripped
        through the database's JSON-encoded column) has a plain list."""
        v = {"title": "x", "tags": ["Dynamite", "BTS"]}
        self.assertEqual(song_grouping._video_tags_label(v), "Dynamite, BTS")

    def test_missing_tags_column_yields_empty_string(self):
        """A video dict with no 'tags' key at all - e.g. from before this
        column existed, or a caller that never populated it."""
        self.assertEqual(song_grouping._video_tags_label({"title": "x"}), "")

    def test_null_and_empty_tags_yield_empty_string(self):
        for tags in (None, "", "[]", []):
            with self.subTest(tags=tags):
                self.assertEqual(song_grouping._video_tags_label({"title": "x", "tags": tags}), "")

    def test_unparseable_tags_yield_empty_string_not_a_crash(self):
        v = {"title": "x", "tags": "not valid json"}
        self.assertEqual(song_grouping._video_tags_label(v), "")

    def test_a_non_list_json_value_yields_empty_string(self):
        v = {"title": "x", "tags": '"just a string"'}
        self.assertEqual(song_grouping._video_tags_label(v), "")

    def test_long_tag_lists_are_capped(self):
        v = {"title": "x", "tags": [f"tag{i}" for i in range(50)]}
        label = song_grouping._video_tags_label(v)
        self.assertEqual(label.count(","), song_grouping._MAX_TAGS_IN_PROMPT - 1)

    def test_a_very_long_individual_tag_is_truncated(self):
        v = {"title": "x", "tags": ["x" * 500]}
        label = song_grouping._video_tags_label(v)
        self.assertEqual(len(label), song_grouping._MAX_TAG_LEN)


class NormalizeTitleTest(unittest.TestCase):
    """
    Tier 2 is the only tier that can link two uploads of a song reliably -
    the AI tier only sees one chunk at a time, so anything it must link
    across a chunk boundary it structurally cannot. Measured on a
    141-title set with known groupings, stripping trailing unbracketed
    upload-type phrases took tier 2 from 0/79 correct links to 79/79 with
    no wrong merges, and cut titles reaching the AI by 74%.
    """

    def _same(self, a, b):
        return normalize_title(a) == normalize_title(b)

    def test_unbracketed_upload_markers_are_stripped(self):
        """The house style on most label channels - no brackets."""
        for other in ["Dynamite Official MV", "Dynamite Dance Practice", "Dynamite Lyric Video",
                      "Dynamite Official Music Video", "Dynamite Performance Video",
                      "Dynamite Special Performance Video", "Dynamite Choreography Version"]:
            with self.subTest(other=other):
                self.assertTrue(self._same("Dynamite", other), f"{other!r} should match 'Dynamite'")

    def test_bracketed_markers_still_stripped(self):
        self.assertTrue(self._same("Dynamite", "Dynamite (Official Video)"))
        self.assertTrue(self._same("Dynamite", "Dynamite [M/V]"))

    def test_real_titles_ending_in_a_marker_word_are_preserved(self):
        """
        'Last Dance' and 'Video Games' are real songs. Stripping a bare
        trailing 'dance' or 'video' would truncate them into a key that
        could collide with a genuinely different song.
        """
        self.assertEqual(normalize_title("Last Dance"), "last dance")
        self.assertEqual(normalize_title("Video Games"), "video games")
        self.assertFalse(self._same("Last Dance", "Last"))
        # ...while still linking their own alternate uploads.
        self.assertTrue(self._same("Last Dance", "Last Dance (Official MV)"))
        self.assertTrue(self._same("Video Games", "Video Games Official MV"))

    def test_numbered_sequels_stay_distinct(self):
        self.assertFalse(self._same("Supernova", "Supernova 2"))

    def test_behind_the_scenes_does_not_collapse_into_the_song(self):
        self.assertFalse(self._same("Super Shy", "Super Shy Making Film"))
        self.assertFalse(self._same("Pink Venom", "Pink Venom Behind The Scenes"))

    def test_all_marker_title_keeps_a_usable_key(self):
        """A song genuinely called 'Audio' must not normalise to nothing."""
        self.assertEqual(normalize_title("Audio"), "audio")
        self.assertTrue(self._same("Audio", "Audio (Official MV)"))

    def test_hangul_titles_are_preserved(self):
        self.assertEqual(normalize_title("에스파 Supernova Official MV"), "에스파 supernova")

    def test_award_show_live_performances_match_the_official_upload(self):
        """
        "Live at <award show>" and "live from <event>" name a performance
        the same way "Official MV" names a studio cut - both should
        normalize to the same key so tier 1 links them without needing
        the AI tier to recognise it every time.
        """
        self.assertTrue(self._same(
            "ROSÉ & Bruno Mars - APT. (Official Music Video)",
            "ROSÉ & Bruno Mars - APT. (Live at the 68th Annual Grammy Awards)",
        ))
        self.assertTrue(self._same(
            "ROSÉ & Bruno Mars - APT. (Official Music Video)",
            "ROSÉ & Bruno Mars - APT. (live from 2024 MAMA AWARDS)",
        ))

    def test_camera_angle_qualifiers_on_dance_practice_match_the_mv(self):
        """
        A pure camera/take qualifier - "(Moving Cam ver.)", "(Fix ver.)" -
        names which camera recorded the same dance practice, not a
        different arrangement. Found missing a match entirely (title
        survived normalization with "dance practice video moving cam ver"
        still attached) in a 2026-08-05 whole-registry audit of VIXX's
        "향 (Scentist)" - neither the bracket-keyword check nor the
        trailing-marker regex recognized bare "moving"/"cam"/"fix" words.
        """
        self.assertTrue(self._same(
            "빅스(VIXX) - '향 (Scentist)' Official M/V",
            "빅스(VIXX) - 향 (Scentist) Dance Practice Video (Moving Cam ver.)",
        ))
        self.assertTrue(self._same(
            "ATEEZ(에이티즈) - 'BAD' Official MV",
            "ATEEZ(에이티즈) - 'BAD' Dance Practice (FIX Ver.)",
        ))

    def test_any_ver_tag_on_a_dance_or_performance_video_matches_the_mv(self):
        """
        On a Dance Practice / Performance Video / Choreography Video, a
        named Ver. tag is a costume theme or occasion, never a producer
        alias or a rearrangement (there's no separate audio mix to credit
        on a dance recording) - so it always merges, unlike the same-
        looking tag on a Lyric Video or Visualizer (see the do-not-merge
        counterexample below).
        """
        self.assertTrue(self._same(
            "OH MY GIRL(오마이걸) - BLACK MIRROR M/V",
            "OH MY GIRL(오마이걸) - BLACK MIRROR Dance Practice (Halloween ver.)",
        ))
        self.assertTrue(self._same(
            "에이스(A.C.E) 'My Girl' M/V",
            "에이스(A.C.E) 'My Girl' Performance Video (Moving ver.)",
        ))

    def test_language_ver_tag_merges_regardless_of_video_type(self):
        """
        2026-08-10 rule reversal: a different-language re-recording now
        merges with the original - see song_grouping's module docstring.
        RIIZE ("Love 119 (Japanese Ver.)" Dance Practice / plain
        Performance Video) and ZHOUMI (Chinese-Ver/Korean-Ver "Uncontact
        LIVE Performance Video" pair) previously stayed split here.
        """
        self.assertTrue(self._same(
            "RIIZE 라이즈 'Love 119' Performance Video",
            "RIIZE 라이즈 'Love 119 (Japanese Ver.)' Dance Practice",
        ))
        self.assertTrue(self._same(
            "ZHOUMI 'Starry Night' (Chinese Ver.) Uncontact LIVE Performance Video",
            "ZHOUMI 'Starry Night' (Korean Ver.) Uncontact LIVE Performance Video",
        ))

    def test_named_ver_tag_off_a_dance_or_performance_video_now_merges(self):
        """
        2026-08-10 rule reversal: a producer/DJ-alias Ver. tag (a remix
        credit) and a language Ver. tag on a Lyric Video/Official MV now
        both merge with the plain upload, same as any other rearrangement
        or language version - see song_grouping's module docstring.
        """
        self.assertTrue(self._same(
            "ATEEZ(에이티즈) - 'BAD' Official MV",
            "ATEEZ(에이티즈) - 'BAD (Steve Aoki Ver.)' Lyric Video",
        ))
        self.assertTrue(self._same(
            "ATEEZ(에이티즈) - 'BAD' Official MV",
            "ATEEZ(에이티즈) - 'BAD (Japanese Ver.)' Official MV",
        ))

    def test_bare_numbered_ver_tag_matches_regardless_of_video_type(self):
        """"Decided: merge bare (Ver.) tags too" - a number/letter alone is
        never a producer alias, so this holds even off a dance/performance
        video (contrast with the named-tag test above)."""
        self.assertTrue(self._same("OH MY GIRL 'LIAR LIAR'", "OH MY GIRL 'LIAR LIAR (ver.2)'"))

    def test_remix_and_named_producer_remix_merge_with_the_original(self):
        """2026-08-10 rule reversal: a remix - bare or named after its
        producer/DJ - is now the same song as the original."""
        self.assertTrue(self._same("Dynamite", "Dynamite (Remix)"))
        self.assertTrue(self._same("LEMONADE", "LEMONADE (2Spade Remix)"))
        self.assertTrue(self._same("LEMONADE", "LEMONADE (Sara Landry Remix)"))

    def test_arrangement_variants_merge_with_the_original(self):
        """2026-08-10 rule reversal: acoustic/instrumental/vocal/karaoke/
        a cappella/sped-up/slowed versions are now the same song."""
        for other in [
            "Dynamite (Acoustic Ver.)", "Dynamite (Instrumental)", "Dynamite (Inst.)",
            "Dynamite (Vocal Ver.)", "Dynamite (Karaoke Ver.)", "Dynamite (A Cappella)",
            "Dynamite (Acapella)", "Dynamite (Sped Up Ver.)", "Dynamite (Slowed)",
        ]:
            with self.subTest(other=other):
                self.assertTrue(self._same("Dynamite", other), f"{other!r} should match 'Dynamite'")

    def test_bare_language_bracket_merges_with_the_original(self):
        """A bracketed language name alone, with no "Ver." tag, also merges -
        the language check doesn't depend on _VER_TAG_RE firing too."""
        self.assertTrue(self._same("Dynamite", "Dynamite (Japanese)"))

    def test_nfd_titles_match_their_nfc_counterpart(self):
        """
        Some titles arrive with Hangul stored as NFD (decomposed Jamo)
        rather than NFC (precomposed syllables) - visually identical, a
        different code point sequence. A 2026-08-05 whole-registry audit
        found 138 existing video titles stored this way.
        """
        import unicodedata
        nfc_title = "장나라 Jang nara - 아마도 사랑이겠죠 M/V"
        nfd_title = unicodedata.normalize("NFD", nfc_title)
        self.assertNotEqual(nfc_title, nfd_title, "test fixture should actually differ byte-for-byte")
        self.assertTrue(self._same(nfc_title, nfd_title))


class MatchKeyTest(unittest.TestCase):
    def test_key_is_bare_title_without_artists(self):
        index = song_grouping.build_artist_index([])
        self.assertEqual(song_grouping._match_key("Dynamite (Official MV)", index), "dynamite")

    def test_key_is_artist_scoped_with_artists(self):
        index = song_grouping.build_artist_index(_compile(_ARTIST_NAMES))
        self.assertEqual(
            song_grouping._match_key("BTS Dynamite (Official MV)", index), ("BTS", "bts dynamite")
        )

    def test_same_title_different_artists_do_not_collide(self):
        """The whole reason artist tagging exists on shared channels."""
        index = song_grouping.build_artist_index(_compile(_ARTIST_NAMES))
        self.assertNotEqual(
            song_grouping._match_key("BTS - Spring Day", index),
            song_grouping._match_key("TWICE - Spring Day", index),
        )

    def test_untitled_after_normalisation_is_none(self):
        index = song_grouping.build_artist_index([])
        self.assertIsNone(song_grouping._match_key("(Official MV)", index))


class GroupChannelVideosTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        # These tests exercise the AI tier, which is skipped entirely when
        # no key is configured - and the test environment has none. The
        # call itself is still stubbed per-test; this only gets us past the
        # availability check that guards the tier.
        patcher = mock.patch.object(song_grouping.gemini_client, "is_available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        add_channel(self.conn, "UC_a", display_name="Artist A")
        self.conn.commit()

    def _group(self, existing, new, **kwargs):
        """Runs grouping with Gemini stubbed out, returning (map, call_count)."""
        calls = []

        def _fake_call_gemini(prompt, model=None, **kwargs):
            calls.append(prompt)
            return None  # forces song_id NULL (recoverable via regroup)

        with mock.patch.object(song_grouping, "call_gemini", _fake_call_gemini):
            result = song_grouping.group_channel_videos(self.conn, "UC_a", existing, new, **kwargs)
        return result, len(calls)

    def test_no_new_videos_makes_no_gemini_calls(self):
        result, calls = self._group([], [])
        self.assertEqual(result, {})
        self.assertEqual(calls, 0)

    def test_exact_title_match_against_existing_skips_gemini(self):
        """Tier 1 must stay free - this is the common duplicate-upload case."""
        existing = [{"video_id": "old", "title": "Dynamite (Official MV)", "song_id": 7}]
        new = [{"video_id": "new", "title": "Dynamite [M/V]"}]

        result, calls = self._group(existing, new)
        self.assertEqual(result, {"new": 7})
        self.assertEqual(calls, 0)

    def test_same_batch_duplicates_group_without_gemini(self):
        """Tier 2 must stay free too."""
        new = [
            {"video_id": "a", "title": "Supernova (Official MV)"},
            {"video_id": "b", "title": "Supernova (Performance Video)"},
        ]
        result, calls = self._group([], new)
        self.assertEqual(calls, 0)
        self.assertEqual(result["a"], result["b"])

    def test_ai_prompt_includes_tags_for_a_video_that_has_them(self):
        """Tags reach the AI tier's prompt (as a hint, not a matching key -
        see GroupPromptTest) but only for videos that actually carry them."""
        calls = []

        def _fake_call_gemini(prompt, model=None, **kwargs):
            calls.append(prompt)
            return None

        new = [
            {"video_id": "a", "title": "Distinct Song A", "tags": '["Dynamite", "BTS"]'},
            {"video_id": "b", "title": "Distinct Song B"},
        ]
        with mock.patch.object(song_grouping, "call_gemini", _fake_call_gemini):
            song_grouping.group_channel_videos(self.conn, "UC_a", [], new)

        prompt = calls[0]
        self.assertIn("Distinct Song A [tags: Dynamite, BTS]", prompt)
        self.assertIn("Distinct Song B", prompt)
        self.assertNotIn("Distinct Song B [tags:", prompt)

    def test_gemini_call_count_is_one_per_chunk_of_unresolved_titles(self):
        """
        The budget-relevant invariant: requests scale with the number of
        chunks of genuinely unresolved titles, and nothing else.
        """
        from shared.gemini_client import CHUNK_SIZE

        for count in (1, CHUNK_SIZE, CHUNK_SIZE + 1, CHUNK_SIZE * 3):
            with self.subTest(unresolved=count):
                new = [{"video_id": f"v{i}", "title": f"Distinct Song {i}"} for i in range(count)]
                _, calls = self._group([], new)
                self.assertEqual(calls, -(-count // CHUNK_SIZE))  # ceil division

    def test_parallel_chunks_produce_the_same_result_as_sequential(self):
        """
        Concurrency must be invisible in the output. Every chunk gets the
        same existing-songs snapshot and no song row is written until all
        have returned, so results - including assigned song_ids - have to
        match what a sequential run produces.
        """
        from shared.gemini_client import CHUNK_SIZE

        new = [{"video_id": f"v{i}", "title": f"Distinct Song {i}"} for i in range(CHUNK_SIZE * 3)]

        def _fake(prompt, model=None, **kwargs):
            return None

        with mock.patch.object(song_grouping, "call_gemini", _fake):
            with mock.patch.object(song_grouping, "_AI_CHUNK_WORKERS", 1):
                sequential = song_grouping.group_channel_videos(self.conn, "UC_a", [], list(new))
            self.conn.execute("DELETE FROM songs")
            self.conn.commit()
            with mock.patch.object(song_grouping, "_AI_CHUNK_WORKERS", 4):
                parallel = song_grouping.group_channel_videos(self.conn, "UC_a", [], list(new))

        # Same partitioning, and the same video ends up with the same
        # position in song_id order either way.
        self.assertEqual(set(sequential), set(parallel))
        rank = lambda m: {v: sorted(set(m.values())).index(s) for v, s in m.items()}
        self.assertEqual(rank(sequential), rank(parallel))

    def test_chunks_all_see_the_same_existing_songs_snapshot(self):
        """
        The property that makes parallelism safe: no chunk observes another
        chunk's results, so ordering between them cannot matter.
        """
        from shared.gemini_client import CHUNK_SIZE

        existing = [{"video_id": "old", "title": "Tracked Song", "song_id": 5}]
        new = [{"video_id": f"v{i}", "title": f"Distinct Song {i}"} for i in range(CHUNK_SIZE * 2)]
        seen = []

        def _fake(prompt, model=None, **kwargs):
            seen.append(prompt.split("New titles to classify")[0])
            return None

        with mock.patch.object(song_grouping, "call_gemini", _fake):
            song_grouping.group_channel_videos(self.conn, "UC_a", existing, new)

        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])

    def _group_with_responses(self, new, responses):
        """Runs grouping with call_gemini returning `responses` in order."""
        calls = []

        def _fake_call_gemini(prompt, model=None, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return responses[min(len(calls) - 1, len(responses) - 1)]

        with mock.patch.object(song_grouping, "call_gemini", _fake_call_gemini):
            result = song_grouping.group_channel_videos(self.conn, "UC_a", [], new)
        return result, len(calls), calls

    def test_ai_calls_pass_structured_response_schema(self):
        """API-enforced JSON shape is what lets the prompt drop format instructions."""
        new = [{"video_id": "a", "title": "Song One"}]
        good = '[{"existing_id": null, "members": [1]}]'
        _, calls, recorded = self._group_with_responses(new, [good])
        self.assertEqual(calls, 1)
        self.assertEqual(recorded[0]["response_schema"], song_grouping._GROUP_RESPONSE_SCHEMA)
        self.assertEqual(recorded[0]["thinking_level"], "low")

    def test_unreadable_response_leaves_videos_unassigned(self):
        """
        Structured outputs make a second parse attempt almost never worth
        the billed request - leave song_id NULL so regroup can finish.
        """
        new = [{"video_id": "a", "title": "Song One"}, {"video_id": "b", "title": "Song One Alt"}]

        result, calls, _ = self._group_with_responses(new, ["not json at all", '[{"existing_id": null, "members": [1, 2]}]'])
        self.assertEqual(calls, song_grouping._MAX_PARSE_ATTEMPTS)
        self.assertEqual(calls, 1)
        self.assertEqual(result, {})

    def test_transport_failure_is_not_re_asked(self):
        """
        call_gemini returns None only after exhausting its own retries, so
        asking again here just bills the same failure a second time.
        """
        new = [{"video_id": "a", "title": "Song One"}]
        result, calls, _ = self._group_with_responses(new, [None])
        self.assertEqual(calls, 1)
        self.assertEqual(result, {})

    def test_valid_response_costs_exactly_one_request(self):
        new = [{"video_id": "a", "title": "Song One"}, {"video_id": "b", "title": "Song Two"}]
        good = '[{"existing_id": null, "members": [1]}, {"existing_id": null, "members": [2]}]'

        result, calls, _ = self._group_with_responses(new, [good])
        self.assertEqual(calls, 1)
        self.assertNotEqual(result["a"], result["b"])

    def test_json_that_parses_but_is_odd_is_not_re_asked(self):
        """
        Content-level weirdness is already handled tolerantly - skipped
        entries, hallucinated ids, unmentioned videos - so it must not
        trigger a retry and spend another request.
        """
        new = [{"video_id": "a", "title": "Song One"}, {"video_id": "b", "title": "Song Two"}]
        odd = '[{"existing_id": 999, "members": [1, 47]}]'  # bad id, out-of-range member, b unmentioned

        result, calls, _ = self._group_with_responses(new, [odd])
        self.assertEqual(calls, 1)
        self.assertEqual(set(result), {"a", "b"})

    def test_failed_gemini_call_leaves_videos_unassigned(self):
        """Failure must not mint locked singletons - NULL stays regroupable."""
        new = [{"video_id": f"v{i}", "title": f"Distinct Song {i}"} for i in range(3)]
        result, _ = self._group([], new)

        self.assertEqual(result, {})

    def test_use_ai_false_skips_gemini_but_still_assigns(self):
        """Regroup mode: no Gemini; leftovers still become provisional songs."""
        new = [
            {"video_id": "a", "title": "Song A (Official MV)"},
            {"video_id": "b", "title": "Song A (Dance Practice)"},
            {"video_id": "c", "title": "Unrelated Song"},
        ]
        with mock.patch.object(song_grouping, "call_gemini") as call_gemini:
            result = song_grouping.group_channel_videos(
                self.conn, "UC_a", [], new, use_ai=False,
            )
        call_gemini.assert_not_called()
        self.assertEqual(result["a"], result["b"])
        self.assertIn("c", result)
        self.assertNotEqual(result["a"], result["c"])

    def test_existing_videos_are_never_regrouped(self):
        """Additive-only: settled song_ids must survive untouched."""
        existing = [{"video_id": "old", "title": "Old Song", "song_id": 42}]
        new = [{"video_id": "new", "title": "Old Song (Dance Practice)"}]

        result, calls = self._group(existing, new)
        self.assertNotIn("old", result)
        self.assertEqual(result["new"], 42)
        self.assertEqual(calls, 0)

    def test_new_song_anchors_to_the_artists_home_channel(self):
        add_channel(self.conn, "UC_home", display_name="TWICE")
        self.conn.commit()
        index = song_grouping.build_artist_index(_compile(["TWICE"]))
        new = [{"video_id": "v1", "title": "TWICE - Brand New Song"}]
        good = '[{"existing_id": null, "members": [1]}]'

        with mock.patch.object(song_grouping, "call_gemini", lambda p, model=None, **kwargs: good):
            result = song_grouping.group_channel_videos(
                self.conn, "UC_a", [], new,
                artist_patterns=index, artist_home_channels={"TWICE": "UC_home"},
            )
        anchored = self.conn.execute(
            "SELECT channel_id FROM songs WHERE song_id = ?", (result["v1"],)
        ).fetchone()
        self.assertEqual(anchored["channel_id"], "UC_home")

    def test_accepts_a_raw_pattern_list_as_well_as_an_index(self):
        """catalog passes an index; the older list form must still work."""
        patterns = _compile(["TWICE"])
        new = [{"video_id": "v1", "title": "TWICE - Some Song"}]

        from_list = song_grouping.group_channel_videos(
            self.conn, "UC_a", [], list(new), artist_patterns=patterns, use_ai=False,
        )
        from_index = song_grouping.group_channel_videos(
            self.conn, "UC_a", [], list(new),
            artist_patterns=song_grouping.build_artist_index(patterns), use_ai=False,
        )
        self.assertEqual(set(from_list), set(from_index))


if __name__ == "__main__":
    unittest.main()
