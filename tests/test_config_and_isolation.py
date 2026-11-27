import os
import subprocess
import sys
import tempfile
import unittest

from . import context


class ConfigurationTest(unittest.TestCase):
    def run_from_another_directory(self, source):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.pop("YOUTUBE_API_KEY", None)
            env.pop("GEMINI_API_KEY", None)
            return subprocess.run(
                [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(context._SRC)!r})\n" + source],
                cwd=directory, env=env, capture_output=True, text=True,
            )

    def test_default_paths_work_from_another_directory(self):
        result = self.run_from_another_directory(
            "from pathlib import Path\n"
            "from render import chart_state, encoding, overlay\n"
            "assert overlay.load_style()['canvas']['width'] > 0\n"
            "assert Path(chart_state.DATA_FILE).is_absolute()\n"
            "assert Path(encoding.RAW_CACHE_DIR).is_absolute()\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_does_not_configure_runtime(self):
        result = self.run_from_another_directory(
            "import dotenv, sys\n"
            "from unittest.mock import patch\n"
            "with patch.object(dotenv, 'load_dotenv') as load, "
            "patch.object(sys.stdout, 'reconfigure') as stdout, "
            "patch.object(sys.stderr, 'reconfigure') as stderr:\n"
            "    import render.pipeline\n"
            "    assert not any(mock.called for mock in (load, stdout, stderr))\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_default_quota_accounting_uses_temporary_storage(self):
        from shared import api_budget

        production_file = context._SRC.parent / "data" / ".api_usage.json"
        self.assertNotEqual(api_budget.USAGE_FILE.resolve(), production_file)
        before = production_file.read_bytes() if production_file.exists() else None
        api_budget.record_youtube_units(1)
        self.assertTrue(api_budget.USAGE_FILE.is_file())
        self.assertEqual(
            production_file.read_bytes() if production_file.exists() else None, before,
        )
