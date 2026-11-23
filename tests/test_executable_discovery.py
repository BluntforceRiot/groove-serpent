from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

from groove_serpent import executable_discovery
from groove_serpent.executable_discovery import find_executable


def _executable_name(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _write_executable(directory: Path, name: str) -> Path:
    executable = directory / _executable_name(name)
    if os.name == "nt":
        executable.write_bytes(_windows_pe_image())
    else:
        executable.write_bytes(b"test executable")
        executable.chmod(0o755)
    return executable.resolve()


def _windows_pe_image() -> bytes:
    dos_header = bytearray(64)
    dos_header[:2] = b"MZ"
    dos_header[60:64] = (64).to_bytes(4, "little")
    coff_header = bytearray(20)
    coff_header[:2] = (0x8664).to_bytes(2, "little")
    coff_header[2:4] = (1).to_bytes(2, "little")
    coff_header[16:18] = (2).to_bytes(2, "little")
    coff_header[18:20] = (0x0002).to_bytes(2, "little")
    return bytes(dos_header) + b"PE\x00\x00" + bytes(coff_header) + b"\x0b\x02"


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class ExecutableDiscoveryTests(unittest.TestCase):
    def test_windows_candidates_ignore_command_and_custom_pathext_entries(self) -> None:
        with mock.patch.object(executable_discovery.os, "name", "nt"), mock.patch.dict(
            os.environ,
            {"PATHEXT": os.pathsep.join((".CMD", ".BAT", ".PY", ".EXE"))},
            clear=False,
        ):
            self.assertEqual(
                executable_discovery._candidate_names("ffmpeg"),
                ("ffmpeg.exe",),
            )
            for name in ("ffmpeg.cmd", "ffmpeg.bat", "ffmpeg.py", "ffmpeg.com"):
                with self.subTest(name=name):
                    self.assertEqual(executable_discovery._candidate_names(name), ())

    def test_windows_native_validation_rejects_scripts_and_invalid_exes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            native = root / "ffmpeg.exe"
            command_script = root / "ffmpeg.cmd"
            invalid_exe = root / "ffprobe.exe"
            native.write_bytes(_windows_pe_image())
            command_script.write_bytes(_windows_pe_image())
            invalid_exe.write_bytes(b"@echo off\r\necho not a PE image\r\n")

            self.assertTrue(executable_discovery._is_windows_native_executable(native))
            self.assertFalse(
                executable_discovery._is_windows_native_executable(command_script)
            )
            self.assertFalse(
                executable_discovery._is_windows_native_executable(invalid_exe)
            )

    @unittest.skipUnless(os.name == "nt", "Windows executable discovery behavior")
    def test_command_scripts_before_native_exe_cannot_win_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trusted = Path(temp_dir) / "trusted"
            trusted.mkdir()
            command_script = trusted / "ffmpeg.cmd"
            batch_script = trusted / "ffmpeg.bat"
            command_script.write_text("@echo off\r\n", encoding="ascii")
            batch_script.write_text("@echo off\r\n", encoding="ascii")
            expected = _write_executable(trusted, "ffmpeg")

            with mock.patch.dict(
                os.environ,
                {
                    "PATH": str(trusted.resolve()),
                    "PATHEXT": os.pathsep.join((".CMD", ".BAT", ".EXE")),
                },
                clear=False,
            ):
                self.assertEqual(find_executable("ffmpeg"), str(expected))
                self.assertIsNone(
                    find_executable("ffmpeg", explicit=str(command_script))
                )
                self.assertIsNone(
                    find_executable("ffmpeg", explicit=str(batch_script))
                )

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
