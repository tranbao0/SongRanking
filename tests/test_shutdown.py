"""Guards force-quit cleanup of helper subprocesses (ffmpeg / yt-dlp)."""

import unittest
from unittest import mock

from . import context  # noqa: F401

from shared import shutdown


class KillHelperSubprocessesTest(unittest.TestCase):
    def test_windows_kills_each_child_tree_then_sweeps_ffmpeg(self):
        calls = []

        def _run(argv, **kwargs):
            calls.append(list(argv))
            result = mock.Mock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with mock.patch.object(shutdown.sys, "platform", "win32"), \
             mock.patch.object(shutdown.os, "getpid", return_value=111), \
             mock.patch.object(shutdown, "_child_pids", return_value=[222, 333]), \
             mock.patch.object(shutdown.subprocess, "run", side_effect=_run):
            n = shutdown.kill_helper_subprocesses()

        self.assertEqual(n, 2)
        self.assertEqual(
            calls[:2],
            [
                ["taskkill", "/F", "/T", "/PID", "222"],
                ["taskkill", "/F", "/T", "/PID", "333"],
            ],
        )
        self.assertEqual(calls[-1], ["taskkill", "/IM", "ffmpeg.exe", "/F"])

    def test_force_sigint_invokes_helper_kill(self):
        shutdown._stop_requested = True
        self.addCleanup(lambda: setattr(shutdown, "_stop_requested", False))

        with mock.patch.object(shutdown, "kill_helper_subprocesses", return_value=3) as kill, \
             mock.patch.object(shutdown.signal, "signal"):
            with self.assertRaises(KeyboardInterrupt):
                shutdown._handle_sigint(None, None)

        kill.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
