from __future__ import annotations

import json
import os
import struct
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Callable
from unittest import mock

from scripts import package_windows_portable as packager
from scripts import verify_windows_portable as verifier
from tests.test_verify_windows_portable import _bundle


ArchiveTransform = Callable[
    [list[tuple[zipfile.ZipInfo, bytes]]],
    list[tuple[str, bytes]],
]


def _package(source: Path, output: Path) -> tuple[Path, dict[str, object]]:
    digest = verifier.verify_portable_directory(source)["manifest_sha256"]
    path, result = packager.package_portable_directory(
        source,
        output,
        expected_manifest_sha256=digest,
    )
    return path, dict(result)


def _rewrite_archive(
    source: Path,
    destination: Path,
    transform: ArchiveTransform,
) -> None:
    with zipfile.ZipFile(source, "r") as archive:
        records = [(info, archive.read(info)) for info in archive.infolist()]
    transformed = transform(records)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in transformed:
            with archive.open(packager._zip_info(name, len(payload)), "w") as opened:
                opened.write(payload)


class WindowsPortablePackagerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_output_junction_is_rejected_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            target = root / "target"
            junction = root / "output-junction"
            source.mkdir()
            target.mkdir()
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode != 0:
                self.skipTest("Directory junction creation is unavailable")
            try:
                with self.assertRaises(packager.PackageError):
                    packager._validated_output_directory(junction, source)
            finally:
                os.rmdir(junction)

    def test_two_packages_are_byte_identical_and_reopen_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            first_output = root / "first"
            second_output = root / "second"
            source.mkdir()
            first_output.mkdir()
            second_output.mkdir()
            manifest_sha256 = _bundle(source)
            first, first_result = _package(source, first_output)
            second, second_result = _package(source, second_output)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                first_result["archive_sha256"],
                second_result["archive_sha256"],
            )
            self.assertEqual(first_result["manifest_sha256"], manifest_sha256)
            self.assertEqual(
                first_result["corresponding_source_path"],
                verifier.WINDOWS_MEDIA_SOURCE_PATH,
            )
            self.assertEqual(
                first_result["corresponding_source_sha256"],
                verifier._sha256_bytes((source / verifier.WINDOWS_MEDIA_SOURCE_PATH).read_bytes()),
            )
            verified = packager.verify_zip_archive(
                first,
                expected_manifest_sha256=manifest_sha256,
            )
            self.assertEqual(verified["archive_sha256"], first_result["archive_sha256"])

    def test_verifier_rejects_redundant_raw_archive_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            archive, _result = _package(source, output)

            for segment in ("//", "/./"):
                with self.subTest(segment=segment):
                    altered = root / f"altered-{segment.replace('/', 's').replace('.', 'd')}.zip"

                    def transform(
                        records: list[tuple[zipfile.ZipInfo, bytes]],
                    ) -> list[tuple[str, bytes]]:
                        result: list[tuple[str, bytes]] = []
                        for info, payload in records:
                            prefix, relative = info.filename.split("/", 1)
                            result.append((f"{prefix}{segment}{relative}", payload))
                        return result

                    _rewrite_archive(archive, altered, transform)
                    with self.assertRaisesRegex(
                        packager.PackageError,
                        "non-canonical member path",
                    ):
                        packager.verify_zip_archive(
                            altered,
                            expected_manifest_sha256=digest,
                        )

    def test_existing_versioned_zip_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            _bundle(source)
            archive, _result = _package(source, output)
            before = archive.read_bytes()
            with self.assertRaisesRegex(packager.PackageError, "replacement is refused"):
                _package(source, output)
            self.assertEqual(archive.read_bytes(), before)

    def test_source_tamper_is_rejected_before_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            (source / "tools" / "ffmpeg.exe").write_bytes(b"same-length-bad!")
            with self.assertRaises(verifier.VerificationError):
                verifier.verify_portable_directory(
                    source,
                    expected_manifest_sha256=digest,
                )
            with self.assertRaises(packager.PackageError):
                packager.package_portable_directory(
                    source,
                    output,
                    expected_manifest_sha256=digest,
                )
            self.assertEqual(list(output.iterdir()), [])

    def test_missing_corresponding_source_fails_closed_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            (source / verifier.WINDOWS_MEDIA_SOURCE_PATH).unlink()
            with self.assertRaises(packager.PackageError):
                packager.package_portable_directory(
                    source,
                    output,
                    expected_manifest_sha256=digest,
                )
            self.assertEqual(list(output.iterdir()), [])

    def test_tamper_collision_truncation_and_zip_slip_are_rejected(self) -> None:
        cases = ("tamper", "collision", "truncate", "zip-slip")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = root / "portable"
                output = root / "out"
                source.mkdir()
                output.mkdir()
                manifest_sha256 = _bundle(source)
                archive, _result = _package(source, output)
                altered = root / f"{case}.zip"
                if case == "truncate":
                    altered.write_bytes(archive.read_bytes()[:-32])
                else:

                    def transform(
                        records: list[tuple[zipfile.ZipInfo, bytes]],
                    ) -> list[tuple[str, bytes]]:
                        result = [(info.filename, payload) for info, payload in records]
                        if case == "tamper":
                            name, payload = result[1]
                            changed = bytes([payload[0] ^ 1]) + payload[1:]
                            result[1] = (name, changed)
                        elif case == "collision":
                            name, payload = result[1]
                            result.append((name.swapcase(), payload))
                        else:
                            _name, payload = result[1]
                            prefix = records[0][0].filename.split("/", 1)[0]
                            result[1] = (f"{prefix}/../escape.txt", payload)
                        return result

                    _rewrite_archive(archive, altered, transform)
                with self.assertRaises(packager.PackageError):
                    packager.verify_zip_archive(
                        altered,
                        expected_manifest_sha256=manifest_sha256,
                    )

    def test_cli_is_strict_json_and_nonzero_for_bad_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            _bundle(source)
            status = packager.main(
                [
                    "--directory",
                    str(source),
                    "--output-directory",
                    str(output),
                    "--expected-manifest-sha256",
                    "0" * 64,
                ]
            )
            self.assertNotEqual(status, 0)
            self.assertEqual(list(output.iterdir()), [])

    def test_cli_success_result_has_no_absolute_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            _path, result = packager.package_portable_directory(
                source,
                output,
                expected_manifest_sha256=digest,
            )
            rendered = json.dumps(result)
            self.assertNotIn(str(source), rendered)
            self.assertEqual(result["code_signing"], "unsigned")

    def test_concurrent_archive_winner_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            real_publish = packager.rename_no_replace
            winner = b"other process won\n"

            def install_winner(stage: Path, destination: Path) -> None:
                destination.write_bytes(winner)
                real_publish(stage, destination)

            with mock.patch.object(
                packager,
                "rename_no_replace",
                side_effect=install_winner,
            ):
                with self.assertRaisesRegex(packager.PackageError, "replacement is refused"):
                    packager.package_portable_directory(
                        source,
                        output,
                        expected_manifest_sha256=digest,
                    )

            outputs = list(output.iterdir())
            self.assertEqual(len(outputs), 1)
            self.assertEqual(outputs[0].read_bytes(), winner)

    def test_post_publish_failure_preserves_intervening_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            real_verify = packager.verify_zip_archive
            winner = b"intervening winner\n"
            calls = 0

            def replace_before_final_verify(
                path: Path,
                *,
                expected_manifest_sha256: str,
            ) -> packager.ArchiveVerification:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_verify(
                        path,
                        expected_manifest_sha256=expected_manifest_sha256,
                    )
                path.unlink()
                path.write_bytes(winner)
                raise packager.PackageError(
                    "archive_hash",
                    "simulated post-publication path swap",
                )

            with mock.patch.object(
                packager,
                "verify_zip_archive",
                side_effect=replace_before_final_verify,
            ):
                with self.assertRaisesRegex(packager.PackageError, "path swap"):
                    packager.package_portable_directory(
                        source,
                        output,
                        expected_manifest_sha256=digest,
                    )

            outputs = list(output.iterdir())
            self.assertEqual(len(outputs), 1)
            self.assertEqual(outputs[0].read_bytes(), winner)

    def test_rename_completion_before_interrupt_removes_published_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            real_publish = packager.rename_no_replace

            def publish_then_interrupt(stage: Path, destination: Path) -> None:
                real_publish(stage, destination)
                raise KeyboardInterrupt

            with (
                mock.patch.object(
                    packager,
                    "rename_no_replace",
                    side_effect=publish_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                packager.package_portable_directory(
                    source,
                    output,
                    expected_manifest_sha256=digest,
                )

            self.assertEqual(list(output.iterdir()), [])

    def test_interruption_never_double_closes_a_reused_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            victim = None

            def open_victim_then_interrupt(_stage: Path, _destination: Path) -> None:
                nonlocal victim
                victim = (root / "independent.txt").open("w+b")
                raise KeyboardInterrupt

            try:
                with (
                    mock.patch.object(
                        packager,
                        "_publish_new",
                        side_effect=open_victim_then_interrupt,
                    ),
                    self.assertRaises(KeyboardInterrupt),
                ):
                    packager.package_portable_directory(
                        source,
                        output,
                        expected_manifest_sha256=digest,
                    )
                self.assertIsNotNone(victim)
                assert victim is not None
                os.fstat(victim.fileno())
                victim.write(b"still open\n")
                victim.flush()
            finally:
                if victim is not None:
                    victim.close()

    def test_verifier_rejects_prefix_and_trailing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            archive, _result = packager.package_portable_directory(
                source,
                output,
                expected_manifest_sha256=digest,
            )
            original = archive.read_bytes()
            variants = (
                b"UNPROFILED-PREFIX" + original,
                original + b"UNPROFILED-TRAILER",
            )
            for payload in variants:
                with self.subTest(edge=payload[:20]):
                    archive.write_bytes(payload)
                    with self.assertRaisesRegex(
                        packager.PackageError,
                        "layout is not exact",
                    ):
                        packager.verify_zip_archive(
                            archive,
                            expected_manifest_sha256=digest,
                        )

    def test_verifier_rejects_unnormalized_zip_header_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            archive, _result = packager.package_portable_directory(
                source,
                output,
                expected_manifest_sha256=digest,
            )
            original = archive.read_bytes()
            central_offset = struct.unpack_from(
                "<L",
                original,
                len(original) - 22 + 16,
            )[0]
            mutations = {
                "local extract version": (4, 21),
                "local modified time": (10, 1),
                "local modified date": (12, 0x22),
                "central creator system/version": (central_offset + 4, 20),
                "central extract version": (central_offset + 6, 21),
                "central modified time": (central_offset + 12, 1),
                "central modified date": (central_offset + 14, 0x22),
                "central disk start": (central_offset + 34, 1),
                "central internal attributes": (central_offset + 36, 1),
            }
            for label, (offset, replacement) in mutations.items():
                with self.subTest(label=label):
                    altered = bytearray(original)
                    struct.pack_into("<H", altered, offset, replacement)
                    archive.write_bytes(altered)
                    with self.assertRaises(packager.PackageError):
                        packager.verify_zip_archive(
                            archive,
                            expected_manifest_sha256=digest,
                        )
            altered = bytearray(original)
            struct.pack_into("<H", altered, 6, 0x0800)
            struct.pack_into("<H", altered, central_offset + 8, 0x0800)
            archive.write_bytes(altered)
            with self.assertRaises(packager.PackageError):
                packager.verify_zip_archive(
                    archive,
                    expected_manifest_sha256=digest,
                )

    def test_same_handle_verifier_detects_mutation_after_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            archive, _result = packager.package_portable_directory(
                source,
                output,
                expected_manifest_sha256=digest,
            )
            real_layout = packager.zip_layout_is_exact
            appended = False

            def append_during_layout(*args: object, **kwargs: object) -> bool:
                nonlocal appended
                if not appended:
                    appended = True
                    with archive.open("ab") as destination:
                        destination.write(b"CONCURRENT-TRAILER")
                return real_layout(*args, **kwargs)  # type: ignore[arg-type]

            with (
                mock.patch.object(
                    packager,
                    "zip_layout_is_exact",
                    side_effect=append_during_layout,
                ),
                self.assertRaisesRegex(packager.PackageError, "changed during verification"),
            ):
                packager.verify_zip_archive(
                    archive,
                    expected_manifest_sha256=digest,
                )

    def test_end_to_end_final_verifier_rejects_trailing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "portable"
            output = root / "out"
            source.mkdir()
            output.mkdir()
            digest = _bundle(source)
            real_verify = packager.verify_zip_archive
            calls = 0

            def append_on_final(
                path: Path,
                *,
                expected_manifest_sha256: str,
            ) -> packager.ArchiveVerification:
                nonlocal calls
                calls += 1
                if calls == 2:
                    with path.open("ab") as destination:
                        destination.write(b"UNPROFILED-TRAILER")
                return real_verify(
                    path,
                    expected_manifest_sha256=expected_manifest_sha256,
                )

            with (
                mock.patch.object(
                    packager,
                    "verify_zip_archive",
                    side_effect=append_on_final,
                ),
                self.assertRaisesRegex(
                    packager.PackageError,
                    "layout is not exact",
                ) as caught,
            ):
                packager.package_portable_directory(
                    source,
                    output,
                    expected_manifest_sha256=digest,
                )

            archives = list(output.glob("*.zip"))
            self.assertEqual(len(archives), 1)
            self.assertTrue(archives[0].read_bytes().endswith(b"UNPROFILED-TRAILER"))
            self.assertTrue(
                any("lost ownership" in note for note in getattr(caught.exception, "__notes__", ()))
            )


if __name__ == "__main__":
    unittest.main()
