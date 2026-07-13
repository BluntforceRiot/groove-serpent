from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from groove_serpent.audacity import discover_audacity


class AudacityDiscoveryTests(unittest.TestCase):
    def test_missing_audacity_is_non_fatal(self) -> None:
        with mock.patch("groove_serpent.audacity._candidate_executables", return_value=[]):
            status = discover_audacity()
        self.assertFalse(status.installed)
        self.assertFalse(status.script_pipe_enabled)

    def test_installed_new_module_is_not_reported_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Audacity" / "Audacity.exe"
            executable.parent.mkdir()
            executable.touch()
            module = executable.parent / "modules" / "mod-script-pipe.dll"
            module.parent.mkdir()
            module.touch()
            appdata = root / "AppData"
            config = appdata / "audacity" / "audacity.cfg"
            config.parent.mkdir(parents=True)
            config.write_text("[Module]\nmod-script-pipe=4\n", encoding="utf-8")
            with mock.patch(
                "groove_serpent.audacity._candidate_executables",
                return_value=[executable],
            ), mock.patch.dict(
                "groove_serpent.audacity.os.environ",
                {"APPDATA": str(appdata)},
                clear=True,
            ):
                status = discover_audacity()

        self.assertTrue(status.installed)
        self.assertTrue(status.script_module_installed)
        self.assertEqual(status.script_module_state, "new")
        self.assertFalse(status.script_pipe_enabled)
        self.assertIn("not enabled", status.message)

    def test_enabled_state_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Audacity.exe"
            executable.touch()
            module = root / "modules" / "mod-script-pipe.dll"
            module.parent.mkdir()
            module.touch()
            config = root / "prefs" / "audacity" / "audacity.cfg"
            config.parent.mkdir(parents=True)
            config.write_text("[Module]\nmod-script-pipe=1\n", encoding="utf-8")
            with mock.patch(
                "groove_serpent.audacity._candidate_executables",
                return_value=[executable],
            ), mock.patch.dict(
                "groove_serpent.audacity.os.environ",
                {"APPDATA": str(root / "prefs")},
                clear=True,
            ):
                status = discover_audacity()

        self.assertTrue(status.script_pipe_enabled)
        self.assertEqual(status.script_module_state, "enabled")


if __name__ == "__main__":
    unittest.main()
