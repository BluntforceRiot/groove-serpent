from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import build_public_archive


class BuildPublicArchiveTests(unittest.TestCase):
    @staticmethod
    def _member_bytes(platform: str) -> bytes:
        output = io.BytesIO()
        with mock.patch.object(zipfile.sys, "platform", platform):
            with zipfile.ZipFile(output, "w") as archive:
                build_public_archive.zip_bytes(archive, "README.md", b"same\n")
        return output.getvalue()

    def test_member_bytes_are_platform_independent(self) -> None:
        windows = self._member_bytes("win32")
        self.assertEqual(windows, self._member_bytes("linux"))
        self.assertEqual(windows, self._member_bytes("darwin"))
        with zipfile.ZipFile(io.BytesIO(windows)) as archive:
            info = archive.infolist()[0]
        self.assertEqual(info.create_system, build_public_archive.ZIP_CREATE_SYSTEM_UNIX)
        self.assertEqual(info.external_attr, build_public_archive.ZIP_REGULAR_FILE_MODE << 16)
        self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
        self.assertEqual(info.extra, b"")

    def test_build_archive_is_reproducible_and_manifested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_text("hello\n", encoding="utf-8")
            first = base / "first.zip"
            second = base / "second.zip"
            first_manifest = base / "first.sha256"
            second_manifest = base / "second.sha256"
            first_result = build_public_archive.build_archive(
                first, first_manifest, root=source, require_git_checkout=False
            )
            second_result = build_public_archive.build_archive(
                second, second_manifest, root=source, require_git_checkout=False
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_result, second_result)
            self.assertEqual(first_manifest.read_bytes(), second_manifest.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        "groove-serpent-0.5.0-alpha.1/README.md",
                        "groove-serpent-0.5.0-alpha.1/SOURCE_MANIFEST.sha256",
                    ],
                )


if __name__ == "__main__":
    unittest.main()
