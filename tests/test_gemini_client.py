"""
Guards call_gemini's retry policy.

The reason this matters is not robustness in the abstract: song_grouping
treats a failed call as "leave these ungrouped", and grouping is additive,
so a video that loses its one chance is never revisited. A transient 429
that isn't retried permanently splits a song.

The opposite mistake costs money - every attempt is a billed request, so
an error that cannot succeed must not be retried at all.

No test here reaches the network; the SDK call is always patched.
"""

import unittest
from unittest import mock

from . import context  # noqa: F401  (puts src/ on sys.path)

from shared import api_budget, gemini_client


class IsRetryableTest(unittest.TestCase):
    def test_rate_limits_and_transient_failures_retry(self):
        for message in ["429 RESOURCE_EXHAUSTED", "rate limit exceeded", "503 Service Unavailable",
                        "deadline exceeded", "500 internal error"]:
            with self.subTest(message=message):
                self.assertTrue(gemini_client._is_retryable(Exception(message)))

    def test_permanent_errors_do_not_retry(self):
        """Retrying these just bills the same failure three times."""
        for message in ["API key not valid", "400 INVALID_ARGUMENT", "permission denied"]:
            with self.subTest(message=message):
                self.assertFalse(gemini_client._is_retryable(Exception(message)))


class CallGeminiRetryTest(unittest.TestCase):
    def setUp(self):
        # Neutralise the real usage file and the backoff sleep.
        recorded = []
        self.recorded = recorded
        patches = [
            mock.patch.object(api_budget, "record_gemini_request", lambda: recorded.append(1)),
            mock.patch.object(gemini_client.time, "sleep", lambda s: None),
            mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
            mock.patch.object(gemini_client, "_SDK_AVAILABLE", True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _client_returning(text):
        step = mock.Mock()
        step.text = text
        interaction = mock.Mock()
        interaction.steps = [step]
        client = mock.Mock()
        client.interactions.create.return_value = interaction
        return client

    def test_succeeds_first_time_without_retrying(self):
        with mock.patch.object(gemini_client.genai, "Client", return_value=self._client_returning("[]")):
            self.assertEqual(gemini_client.call_gemini("p"), "[]")
        self.assertEqual(len(self.recorded), 1)

    def test_retries_a_rate_limit_then_succeeds(self):
        good = self._client_returning("[]")
        calls = {"n": 0}

        def _create(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("429 RESOURCE_EXHAUSTED")
            return good.interactions.create(**kwargs)

        client = mock.Mock()
        client.interactions.create.side_effect = _create
        with mock.patch.object(gemini_client.genai, "Client", return_value=client):
            self.assertEqual(gemini_client.call_gemini("p"), "[]")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(self.recorded), 2)  # each attempt is a billed request

    def test_gives_up_after_max_attempts(self):
        client = mock.Mock()
        client.interactions.create.side_effect = Exception("503 Service Unavailable")
        with mock.patch.object(gemini_client.genai, "Client", return_value=client):
            self.assertIsNone(gemini_client.call_gemini("p"))
        self.assertEqual(len(self.recorded), gemini_client._MAX_ATTEMPTS)

    def test_permanent_error_is_not_retried(self):
        client = mock.Mock()
        client.interactions.create.side_effect = Exception("API key not valid")
        with mock.patch.object(gemini_client.genai, "Client", return_value=client):
            self.assertIsNone(gemini_client.call_gemini("p"))
        self.assertEqual(len(self.recorded), 1)

    def test_budget_exhaustion_stops_immediately(self):
        """The budget guard must not be defeated by the retry loop."""
        def _boom():
            raise api_budget.QuotaExceededError("budget spent")

        client = mock.Mock()
        with mock.patch.object(api_budget, "record_gemini_request", _boom), \
             mock.patch.object(gemini_client.genai, "Client", return_value=client):
            self.assertIsNone(gemini_client.call_gemini("p"))
        client.interactions.create.assert_not_called()

    def test_markdown_fences_are_stripped(self):
        fenced = "```json\n[{\"existing_id\": null, \"members\": [1]}]\n```"
        with mock.patch.object(gemini_client.genai, "Client", return_value=self._client_returning(fenced)):
            self.assertEqual(gemini_client.call_gemini("p"),
                             '[{"existing_id": null, "members": [1]}]')

    def test_missing_api_key_skips_without_spending_budget(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            self.assertIsNone(gemini_client.call_gemini("p"))
        self.assertEqual(len(self.recorded), 0)


class ChunkedTest(unittest.TestCase):
    def test_splits_into_chunk_size_pieces(self):
        items = list(range(gemini_client.CHUNK_SIZE * 2 + 1))
        chunks = gemini_client.chunked(items)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([len(c) for c in chunks],
                         [gemini_client.CHUNK_SIZE, gemini_client.CHUNK_SIZE, 1])
        self.assertEqual([i for c in chunks for i in c], items)

    def test_empty_input_produces_no_chunks_and_so_no_requests(self):
        self.assertEqual(gemini_client.chunked([]), [])


if __name__ == "__main__":
    unittest.main()
