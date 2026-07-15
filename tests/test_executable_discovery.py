from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

from groove_serpent.executable_discovery import find_executable


def _executable_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _write_executable(directory: Path, name: str) -> Path:
    executable = directory / _executable_name(name)
    executable.write_bytes(b"test executable")
    if os.name != "nt":
        executable.chmod(0o755)
    return executable.resolve()


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class ExecutableDiscoveryTests(unittest.TestCase):
    def test_current_empty_and_relative_path_entries_cannot_win(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current"
            relative = current / "relative-bin"
            trusted = root / "trusted"
            current.mkdir()
            relative.mkdir()
            trusted.mkdir()
            _write_executable(current, "ffmpeg")
            _write_executable(relative, "ffmpeg")
            expected = _write_executable(trusted, "ffmpeg")
            path_value = os.pathsep.join(
                (str(current.resolve()), "", relative.name, str(trusted.resolve()))
            )

            with _working_directory(current), mock.patch.dict(
                os.environ,
                {"PATH": path_value, "PATHEXT": ".EXE"},
                clear=False,
            ):
                observed = find_executable("ffmpeg")

            self.assertEqual(observed, str(expected))

    def test_trusted_absolute_path_tool_resolves_cross_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trusted = Path(temp_dir) / "trusted tools"
            trusted.mkdir()
            expected = _write_executable(trusted, "ffprobe")
            with mock.patch.dict(
                os.environ,
                {"PATH": str(trusted.resolve()), "PATHEXT": ".EXE"},
                clear=False,
            ):
                observed = find_executable("ffprobe")
            self.assertEqual(observed, str(expected))

    def test_explicit_absolute_override_is_resolved_but_relative_path_is_refused(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            current = root / "current"
            current.mkdir()
            expected = _write_executable(root, "fp calc")
            _write_executable(current, "fpcalc")
            with _working_directory(current), mock.patch.dict(
                os.environ,
                {"PATH": "", "PATHEXT": ".EXE"},
                clear=False,
            ):
                self.assertEqual(
                    find_executable("fpcalc", explicit=str(expected)),
                    str(expected),
                )
                self.assertIsNone(
                    find_executable(
                        "fpcalc",
                        explicit=f".{os.sep}{_executable_name('fpcalc')}",
                    )
                )


if __name__ == "__main__":
    unittest.main()
