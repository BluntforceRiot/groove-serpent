from __future__ import annotations

import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import _release_fs


def _exact_zip_payload(name: str = "groove-serpent/example.txt") -> bytes:
    destination = io.BytesIO()
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.reserved = 0
    info.flag_bits = 0
    info.volume = 0
    info.internal_attr = 0
    info.external_attr = 0o100644 << 16
    info.extra = b""
    info.comment = b""
    info.compress_type = zipfile.ZIP_STORED
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr(info, b"reviewed\n")
    return destination.getvalue()


def _layout_is_exact(payload: bytes) -> bool:
    stream = io.BytesIO(payload)
    with zipfile.ZipFile(stream, "r") as archive:
        return _release_fs.zip_layout_is_exact(
            stream,
            len(payload),
            archive.infolist(),
        )


class ExactZipLayoutTests(unittest.TestCase):
    def test_normalized_archive_is_accepted(self) -> None:
        self.assertTrue(_layout_is_exact(_exact_zip_payload()))
        self.assertTrue(_layout_is_exact(_exact_zip_payload("groove-serpent/café.txt")))

    def test_every_normalized_local_and_central_field_is_bound(self) -> None:
        payload = _exact_zip_payload()
        eocd_offset = len(payload) - 22
        central_offset = struct.unpack_from("<L", payload, eocd_offset + 16)[0]
        mutations = {
            "local extract version": (4, 21),
            "local modified time": (10, 1),
            "local modified date": (12, 0x22),
            "central creator system/version": (central_offset + 4, 20),
            "central extract version": (central_offset + 6, 21),
            "central flags": (central_offset + 8, 1),
            "central modified time": (central_offset + 12, 1),
            "central modified date": (central_offset + 14, 0x22),
            "central disk start": (central_offset + 34, 1),
            "central internal attributes": (central_offset + 36, 1),
        }
        for label, (offset, replacement) in mutations.items():
            with self.subTest(label=label):
                altered = bytearray(payload)
                struct.pack_into("<H", altered, offset, replacement)
                self.assertFalse(_layout_is_exact(bytes(altered)))

    def test_ascii_filename_cannot_claim_the_utf8_flag(self) -> None:
        payload = _exact_zip_payload()
        central_offset = struct.unpack_from("<L", payload, len(payload) - 6)[0]
        altered = bytearray(payload)
        struct.pack_into("<H", altered, 6, 0x0800)
        struct.pack_into("<H", altered, central_offset + 8, 0x0800)
        self.assertFalse(_layout_is_exact(bytes(altered)))


class PortableReleasePathTests(unittest.TestCase):
    def test_canonical_path_returns_a_portable_collision_key(self) -> None:
        sharp_s = chr(0x00DF)
        path, key = _release_fs.canonical_portable_relative_path(
            f"docs/{sharp_s}.txt",
            "fixture",
        )
        self.assertEqual(path, f"docs/{sharp_s}.txt")
        self.assertEqual(key, "docs/ss.txt")

    def test_noncanonical_or_windows_unsafe_paths_are_rejected(self) -> None:
        values = (
            "bad\\name.txt",
            "bad//name.txt",
            "bad/./name.txt",
            "../escape.txt",
            "bad\nname.txt",
            "CON.txt",
            "trailing. ",
            "cafe\u0301.txt",
            f"{'x' * 256}.txt",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                _release_fs.canonical_portable_relative_path(value, "fixture")

    def test_mutable_ctime_filesystem_is_refused_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            output = parent / "future" / "release.zip"
            with (
                mock.patch.object(
                    _release_fs,
                    "_path_incarnation",
                    return_value=("ctime-fallback", 1),
                ),
                self.assertRaisesRegex(RuntimeError, "before creating outputs"),
            ):
                _release_fs.require_stable_creation_identity(
                    output.parent,
                    "Fixture output",
                )
            self.assertFalse(output.parent.exists())


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux inode-reuse regression")
class ReleaseFilesystemIncarnationTests(unittest.TestCase):
    def test_reused_file_inode_never_authorizes_unrelated_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stage.tmp"
            path.write_bytes(b"owned")
            identity = _release_fs.capture_identity(path)
            original_inode = path.lstat().st_ino
            path.unlink()

            for _attempt in range(4096):
                path.write_bytes(b"other")
                if path.lstat().st_ino == original_inode:
                    break
                path.unlink()
            else:
                self.skipTest("Filesystem did not reuse the file inode within the bound")

            self.assertFalse(_release_fs.unlink_owned_file_candidates((path,), identity))
            self.assertEqual(path.read_bytes(), b"other")

    def test_reused_directory_inode_never_authorizes_unrelated_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stage"
            path.mkdir()
            identity = _release_fs.capture_identity(path)
            original_inode = path.lstat().st_ino
            path.rmdir()

            for _attempt in range(4096):
                path.mkdir()
                if path.lstat().st_ino == original_inode:
                    break
                path.rmdir()
            else:
                self.skipTest("Filesystem did not reuse the directory inode within the bound")

            (path / "winner.txt").write_text("independent\n", encoding="utf-8")
            self.assertFalse(_release_fs.remove_owned_tree_candidates((path,), identity))
            self.assertEqual(
                (path / "winner.txt").read_text(encoding="utf-8"),
                "independent\n",
            )


if __name__ == "__main__":
    unittest.main()
