"""
Guards the three audit triggers and the state machine around them - not
the agent's own judgment, which is out of reach of a unit test. No test
here ever actually launches a subprocess; subprocess.Popen is mocked
throughout.
"""

import json
import unittest
from unittest import mock

from .context import make_db, add_channel, add_video

from registry import db, grouping_audit


class _FakeLineStream:
    """Stands in for a Popen.stdout that yields `lines` then closes -
    _stream_stdout's reader thread iterates this exactly like a real
    pipe and sees a clean EOF afterward."""

    def __init__(self, lines):
        self._lines = iter(lines)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._lines)


class _BlockingStdout:
    """Stands in for a Popen.stdout that never produces output - used to
    exercise _invoke_agent's timeout path without an hour-long sleep."""

    def __iter__(self):
        return self

    def __next__(self):
        import queue as _queue
        return _queue.Queue().get()  # blocks forever


class GroupingAuditTest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(keep_open=True)
        self.addCleanup(self.conn.really_close)

        patcher = mock.patch.object(db, "get_connection", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _use_tmp_state(self, tmp_path):
        """Route STATE_FILE to a throwaway path so a test's persistence
        never touches the real data/.grouping_audit_state.json."""
        patcher = mock.patch.object(grouping_audit, "STATE_FILE", tmp_path / "state.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _use_tmp_log(self, tmp_path):
        """Route LOG_FILE to a throwaway path so a test's transcript
        never touches the real data/.grouping_audit_log.jsonl."""
        patcher = mock.patch.object(grouping_audit, "LOG_FILE", tmp_path / "log.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_snapshot_before_sync_captures_channel_ids_and_video_counts(self):
        add_channel(self.conn, "UC_a")
        add_video(self.conn, "v1", "UC_a")
        add_video(self.conn, "v2", "UC_a")
        self.conn.commit()

        snap = grouping_audit.snapshot_before_sync()

        self.assertEqual(snap["channel_ids"], {"UC_a"})
        self.assertEqual(snap["video_counts"], {"UC_a": 2})

    def test_new_channel_triggers_audit(self):
        add_channel(self.conn, "UC_old")
        self.conn.commit()
        before = grouping_audit.snapshot_before_sync()

        add_channel(self.conn, "UC_new", display_name="New Group")
        self.conn.commit()

        reason, details = grouping_audit._decide_trigger(before)

        self.assertEqual(reason, "new_channels")
        self.assertEqual(details["channels"], [("UC_new", "New Group")])

    def test_volume_spike_on_existing_channel_triggers_audit(self):
        add_channel(self.conn, "UC_a", display_name="Existing Artist")
        add_video(self.conn, "v0", "UC_a")
        self.conn.commit()
        before = grouping_audit.snapshot_before_sync()

        for i in range(grouping_audit.VOLUME_THRESHOLD):
            add_video(self.conn, f"v{i+1}", "UC_a")
        self.conn.commit()

        reason, details = grouping_audit._decide_trigger(before)

        self.assertEqual(reason, "volume_spike")
        self.assertEqual(details["channels"], [("UC_a", "Existing Artist", grouping_audit.VOLUME_THRESHOLD)])

    def test_small_new_upload_count_does_not_trigger_volume_spike(self):
        add_channel(self.conn, "UC_a")
        self.conn.commit()
        before = grouping_audit.snapshot_before_sync()

        add_video(self.conn, "v1", "UC_a")
        self.conn.commit()

        reason, _ = grouping_audit._decide_trigger(before)

        self.assertIsNone(reason)

    def test_periodic_trigger_fires_after_n_quiet_syncs(self):
        with mock.patch.object(grouping_audit, "_load_state",
                                return_value={"syncs_since_audit": grouping_audit.AUDIT_EVERY_N_SYNCS - 1}):
            add_channel(self.conn, "UC_a")
            self.conn.commit()
            before = grouping_audit.snapshot_before_sync()
            self.conn.commit()

            reason, _ = grouping_audit._decide_trigger(before)

        self.assertEqual(reason, "periodic")

    def test_periodic_trigger_does_not_fire_early(self):
        with mock.patch.object(grouping_audit, "_load_state",
                                return_value={"syncs_since_audit": 0}):
            add_channel(self.conn, "UC_a")
            self.conn.commit()
            before = grouping_audit.snapshot_before_sync()

            reason, _ = grouping_audit._decide_trigger(before)

        self.assertIsNone(reason)

    def test_no_trigger_increments_and_persists_the_counter(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            self._use_tmp_state(Path(d))
            grouping_audit._save_state({"syncs_since_audit": 2})

            add_channel(self.conn, "UC_a")
            self.conn.commit()
            before = grouping_audit.snapshot_before_sync()

            with mock.patch.object(grouping_audit, "_invoke_agent") as mock_invoke:
                grouping_audit.audit_after_sync(before)
                mock_invoke.assert_not_called()

            self.assertEqual(grouping_audit._load_state()["syncs_since_audit"], 3)

    def test_triggered_audit_invokes_agent_and_resets_counter_on_success(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            self._use_tmp_state(Path(d))
            grouping_audit._save_state({"syncs_since_audit": 5})

            add_channel(self.conn, "UC_old")
            self.conn.commit()
            before = grouping_audit.snapshot_before_sync()
            add_channel(self.conn, "UC_new")
            self.conn.commit()

            with mock.patch.object(grouping_audit, "_invoke_agent", return_value=True) as mock_invoke:
                grouping_audit.audit_after_sync(before)
                mock_invoke.assert_called_once()
                prompt = mock_invoke.call_args[0][0]
                self.assertIn("UC_new", prompt)

            self.assertEqual(grouping_audit._load_state()["syncs_since_audit"], 0)

    def test_failed_agent_launch_does_not_reset_the_counter(self):
        """
        A missing CLI or a timeout shouldn't make the periodic trigger go
        quiet for another AUDIT_EVERY_N_SYNCS runs over what's likely a
        one-time environment problem - it should keep asking every sync
        until the launch actually succeeds.
        """
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            self._use_tmp_state(Path(d))
            grouping_audit._save_state({"syncs_since_audit": 5})

            add_channel(self.conn, "UC_old")
            self.conn.commit()
            before = grouping_audit.snapshot_before_sync()
            add_channel(self.conn, "UC_new")
            self.conn.commit()

            with mock.patch.object(grouping_audit, "_invoke_agent", return_value=False):
                grouping_audit.audit_after_sync(before)

            self.assertEqual(grouping_audit._load_state()["syncs_since_audit"], 5)

    def test_invoke_agent_handles_missing_cli_without_raising(self):
        with mock.patch("registry.grouping_audit.subprocess.Popen", side_effect=FileNotFoundError):
            self.assertFalse(grouping_audit._invoke_agent("some prompt"))

    def test_invoke_agent_handles_timeout_without_raising(self):
        import tempfile
        from pathlib import Path

        fake_process = mock.Mock()
        fake_process.stdout = _BlockingStdout()

        with tempfile.TemporaryDirectory() as d:
            self._use_tmp_log(Path(d))
            with mock.patch("registry.grouping_audit.subprocess.Popen", return_value=fake_process), \
                 mock.patch.object(grouping_audit, "AGENT_TIMEOUT_SECONDS", 0.05):
                self.assertFalse(grouping_audit._invoke_agent("some prompt"))
            fake_process.kill.assert_called_once()

    def test_invoke_agent_reports_failure_on_nonzero_exit(self):
        import tempfile
        from pathlib import Path

        fake_process = mock.Mock()
        fake_process.stdout = _FakeLineStream(['{"type": "result", "num_turns": 1}\n'])
        fake_process.wait.return_value = 1

        with tempfile.TemporaryDirectory() as d:
            self._use_tmp_log(Path(d))
            with mock.patch("registry.grouping_audit.subprocess.Popen", return_value=fake_process):
                self.assertFalse(grouping_audit._invoke_agent("some prompt"))

    def test_invoke_agent_succeeds_on_zero_exit(self):
        import tempfile
        from pathlib import Path

        fake_process = mock.Mock()
        fake_process.stdout = _FakeLineStream(['{"type": "result", "num_turns": 1}\n'])
        fake_process.wait.return_value = 0

        with tempfile.TemporaryDirectory() as d:
            self._use_tmp_log(Path(d))
            with mock.patch("registry.grouping_audit.subprocess.Popen", return_value=fake_process):
                self.assertTrue(grouping_audit._invoke_agent("some prompt"))
            fake_process.stdin.write.assert_called_once_with("some prompt")
            fake_process.stdin.close.assert_called_once()

    def test_new_channels_prompt_names_the_channel(self):
        prompt = grouping_audit._build_prompt(
            "new_channels", {"channels": [("UC_xyz", "Xyz Group")]}
        )
        self.assertIn("UC_xyz", prompt)
        self.assertIn("Xyz Group", prompt)
        self.assertIn("manual-grouping-prompt.md", prompt)

    def test_volume_spike_prompt_names_the_channel_and_delta(self):
        prompt = grouping_audit._build_prompt(
            "volume_spike", {"channels": [("UC_abc", "Abc Group", 120)]}
        )
        self.assertIn("UC_abc", prompt)
        self.assertIn("120", prompt)

    def test_periodic_prompt_describes_a_general_sweep(self):
        prompt = grouping_audit._build_prompt("periodic", {})
        self.assertIn("periodic", prompt.lower())
        self.assertIn("cross-channel", prompt.lower())


if __name__ == "__main__":
    unittest.main()
