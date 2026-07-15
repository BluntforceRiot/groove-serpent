from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

try:
    from scripts import build_handoff
except ImportError:  # Sanitized public releases intentionally omit private handoff tooling.
    build_handoff = None  # type: ignore[assignment]
from scripts import build_windows_portable as portable


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, payload: bytes) -> portable.ExactInput:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return portable.ExactInput(path, _sha256(path), path.name)


def _wheel(
    path: Path,
    distribution: str,
    version: str,
    files: dict[str, bytes],
) -> portable.WheelInput:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = distribution.replace("-", "_")
    prefix = f"{normalized}-{version}.dist-info"
    members = {
        **files,
        f"{prefix}/METADATA": (
            f"Metadata-Version: 2.4\nName: {distribution}\nVersion: {version}\n\n"
        ).encode("utf-8"),
        f"{prefix}/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
    }
    record_name = f"{prefix}/RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, payload in members.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        writer.writerow((name, f"sha256={encoded.decode('ascii')}", len(payload)))
    writer.writerow((record_name, "", ""))
    members[record_name] = record.getvalue().encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    exact = portable.ExactInput(path, _sha256(path), f"{distribution} wheel")
    return portable.WheelInput(exact, distribution, version)


def _runtime(path: Path) -> portable.ExactInput:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("python.exe", b"synthetic-python")
        archive.writestr("python311.dll", b"synthetic-dll")
        archive.writestr("python311.zip", b"synthetic-stdlib")
        archive.writestr("python311._pth", "python311.zip\n.\n#import site\n")
        archive.writestr("LICENSE.txt", b"Synthetic PSF license fixture\n")
    return portable.ExactInput(path, _sha256(path), "Python embedded runtime")


def _write_zip_with_sums(path: Path, members: dict[str, bytes]) -> portable.ExactInput:
    sums = "".join(
        f"{hashlib.sha256(payload).hexdigest()}  {name}\n"
        for name, payload in sorted(members.items())
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
        archive.writestr("SHA256SUMS", sums)
    return portable.ExactInput(path, _sha256(path), path.name)


def _media_pair(root: Path) -> tuple[portable.ExactInput, portable.ExactInput]:
    source_payload = b"synthetic source input"
    source_members = {
        "inputs/synthetic-source.tar.xz": source_payload,
        **{
            name: b"# synthetic corresponding-source recipe\n"
            for name in portable.WINDOWS_MEDIA_SOURCE_RECIPE
        },
    }
    source = _write_zip_with_sums(
        root / portable.WINDOWS_MEDIA_SOURCE_FILENAME,
        source_members,
    )
    smoke = {
        "schema": portable.WINDOWS_MEDIA_SMOKE_SCHEMA,
        "result": "passed",
        "runtime": {
            "ffmpeg": "ffmpeg version 8.1.2 synthetic",
            "ffprobe": "ffprobe version 8.1.2 synthetic",
            "network_protocols_absent": ["http", "https", "tcp", "tls", "udp"],
        },
        "source_decode": [{"container": "wav"}, {"container": "aiff"}],
        "cover_art_stream_copy": {
            key: {} for key in ("jpg-flac", "jpg-m4a", "png-flac", "png-m4a")
        },
        "speed_correction": {"filter": "asetrate + libsoxr"},
        "chromaprint": {
            "backend": "FFmpeg chromaprint muxer + Chromaprint 1.6.0 kissfft",
            "repeat_equal": True,
        },
    }
    smoke_payload = (json.dumps(smoke, sort_keys=True) + "\n").encode("utf-8")
    runtime_members = {
        name: f"synthetic:{name}\n".encode("utf-8") for name in portable.WINDOWS_MEDIA_BINARIES
    }
    runtime_members.update(
        {
            "CAPABILITY-SMOKE.json": smoke_payload,
            "FFMPEG-CONFIGURE.txt": b"FFmpeg 8.1.2 synthetic configure\n",
        }
    )
    runtime_files = [
        {
            "path": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        for name, payload in sorted(runtime_members.items())
    ]
    build_manifest = {
        "schema": portable.WINDOWS_MEDIA_RUNTIME_SCHEMA,
        "artifact": {
            "architecture": "x86_64-w64-mingw32",
            "ffmpeg_version": "8.1.2",
            "profile": "groove-serpent-minimal-audio-shared-v1",
            "source_date_epoch": 1_781_664_539,
        },
        "license_evidence": {
            "ffmpeg_gpl_flag": False,
            "ffmpeg_nonfree_flag": False,
            "ffmpeg_version3_flag": False,
            "linking": "shared FFmpeg, Chromaprint, and libsoxr libraries",
        },
        "runtime_files": runtime_files,
        "capability_smoke_sha256": hashlib.sha256(smoke_payload).hexdigest(),
        "source_inputs": [
            {
                "name": "synthetic-source.tar.xz",
                "sha256": hashlib.sha256(source_payload).hexdigest(),
            }
        ],
    }
    runtime_members["BUILD-MANIFEST.json"] = (
        json.dumps(build_manifest, sort_keys=True) + "\n"
    ).encode("utf-8")
    runtime = _write_zip_with_sums(
        root / portable.WINDOWS_MEDIA_RUNTIME_FILENAME,
        runtime_members,
    )
    return runtime, source


def _inputs(root: Path) -> portable.PortableInputs:
    web = {
        f"groove_serpent/web/{name}": f"fixture:{name}\n".encode("utf-8")
        for name in (
            "index.html",
            "app.js",
            "styles.css",
            "album.html",
            "album.js",
            "album.css",
        )
    }
    app = _wheel(
        root / "groove_serpent-1.0.0-py3-none-any.whl",
        "groove-serpent",
        "1.0.0",
        {
            "groove_serpent/__init__.py": b'__version__ = "1.0.0"\n',
            "groove_serpent/__main__.py": b"raise SystemExit(0)\n",
            **web,
        },
    )
    numpy = _wheel(
        root / "numpy-2.4.6-cp311-cp311-win_amd64.whl",
        "numpy",
        "2.4.6",
        {
            "numpy/__init__.py": b'__version__ = "2.4.6"\n',
            "numpy-licenses/LICENSE.txt": b"Synthetic NumPy license fixture\n",
        },
    )
    skill_root = root / "skill"
    skill_files = tuple(
        portable.ResourceInput(
            _write(skill_root / relative, f"fixture:{relative}\n".encode("utf-8")),
            portable._safe_relative_path(relative, "fixture skill"),
        )
        for relative in (
            "SKILL.md",
            "agents/openai.yaml",
            "references/authority-contract.json",
        )
    )
    media_runtime, media_source = _media_pair(root)
    notice = (
        "Groove Serpent 1.0.0; Python 3.11.9; NumPy 2.4.6\n"
        "ffmpeg version synthetic\nffprobe version synthetic\n"
        f"{media_runtime.sha256}\n{media_source.sha256}\n"
        f"{portable.WINDOWS_MEDIA_SOURCE_DESTINATION}\n"
    ).encode("utf-8")
    return portable.PortableInputs(
        app_wheel=app,
        dependency_wheels=(numpy,),
        python_embed=_runtime(root / "python-3.11.9-embed-amd64.zip"),
        python_version="3.11.9",
        windows_media_runtime=media_runtime,
        windows_media_corresponding_source=media_source,
        groove_license=_write(root / "LICENSE", b"Synthetic Apache license fixture\n"),
        third_party_notices=_write(
            root / "THIRD-PARTY-NOTICES.txt",
            notice,
        ),
        portable_verifier=_write(
            root / "verify-portable.py",
            b"# Synthetic portable verifier fixture.\n",
        ),
        skill_files=skill_files,
    )


def _synthetic_smoke(
    root: Path,
    inputs: portable.PortableInputs,
    media_evidence: portable.WindowsMediaEvidence,
    media_smoke: dict[str, object],
) -> dict[str, object]:
    del root, media_evidence
    return {
        "app_version": inputs.app_wheel.version,
        "python_version": inputs.python_version,
        "architecture": "64-bit",
        "ffmpeg_version": "ffmpeg version synthetic",
        "ffprobe_version": "ffprobe version synthetic",
        "libsoxr": "exercised-ready",
        "chromaprint": "ffmpeg-muxer-exercised-ready",
        "app_fingerprint_parity": "exact-match",
        "media_runtime_profile": "groove-serpent-minimal-audio-shared-v1",
        **media_smoke,
        "web_resources": "six-required-assets-readable",
        "doctor_ready": True,
    }


def _synthetic_media_smoke(
    root: Path,
    capability_script: Path,
    expected_sha256: str,
) -> dict[str, object]:
    del root, capability_script
    return {
        "synthetic_supported_formats": "fresh-capability-smoke-exact-match",
        "media_capability_smoke_sha256": expected_sha256,
    }


def _synthetic_verifier(root: Path, expected: str) -> dict[str, object]:
    del root
    return {
        "ok": True,
        "manifest_sha256": expected,
        "authenticity": "anchored-to-expected-manifest-sha256",
    }


class WindowsPortableBuildTests(unittest.TestCase):
    def test_builder_and_notices_are_in_private_handoff_scope(self) -> None:
        if build_handoff is None:
            self.skipTest("Private handoff builder is intentionally absent.")
        self.assertIn("scripts", build_handoff.TREES)
        self.assertIn("packaging", build_handoff.TREES)
        members = {
            path.relative_to(build_handoff.ROOT).as_posix()
            for path in build_handoff.included_files()
        }
        self.assertIn("scripts/build_windows_portable.py", members)
        self.assertIn("packaging/windows/README.md", members)
        self.assertIn("packaging/windows/THIRD-PARTY-NOTICES.txt", members)

    def test_synthetic_build_is_exact_deterministic_and_side_by_side(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = _inputs(root / "inputs")
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            with (
                mock.patch.object(
                    portable,
                    "_smoke_bundle",
                    side_effect=_synthetic_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_windows_media_runtime",
                    side_effect=_synthetic_media_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_packaged_verifier",
                    side_effect=_synthetic_verifier,
                ),
                mock.patch.object(
                    portable,
                    "_publish_new_directory",
                    side_effect=lambda stage, final: os.rename(stage, final),
                ),
            ):
                first = portable.build_portable_directory(inputs, first_root)
                second = portable.build_portable_directory(inputs, second_root)
                with self.assertRaises(FileExistsError):
                    portable.build_portable_directory(inputs, first_root)

            first_inventory = portable._inventory(first)
            second_inventory = portable._inventory(second)
            self.assertEqual(first_inventory, second_inventory)
            self.assertFalse((first / ".verified-inputs").exists())
            self.assertTrue((first / "runtime" / "python.exe").is_file())
            self.assertTrue((first / "app" / "groove_serpent" / "web" / "app.js").is_file())
            self.assertTrue((first / "skills" / "groove-serpent" / "SKILL.md").is_file())
            carried_source = first.joinpath(
                *portable.PurePosixPath(portable.WINDOWS_MEDIA_SOURCE_DESTINATION).parts
            )
            self.assertEqual(
                carried_source.read_bytes(),
                inputs.windows_media_corresponding_source.path.read_bytes(),
            )
            self.assertIn(b"runtime\\python.exe", (first / "groove-serpent.cmd").read_bytes())
            self.assertIn(
                b"NoDefaultCurrentDirectoryInExePath=1",
                (first / "groove-serpent.cmd").read_bytes(),
            )
            self.assertEqual(
                (first / "verify-portable.py").read_bytes(),
                inputs.portable_verifier.path.read_bytes(),
            )
            self.assertIn(
                b"verify-portable.py",
                (first / "verify-portable.cmd").read_bytes(),
            )
            self.assertIn(b'--root "%~dp0."', (first / "verify-portable.cmd").read_bytes())

            manifest = portable._verify_manifest(first)
            self.assertEqual(manifest["app"]["version"], "1.0.0")
            self.assertEqual(
                manifest["builder"]["sha256"],
                _sha256(Path(portable.__file__).resolve()),
            )
            self.assertEqual(manifest["code_signing"]["status"], "unsigned")
            verifier_inputs = [
                item for item in manifest["inputs"] if item["role"] == "portable-verifier"
            ]
            self.assertEqual(len(verifier_inputs), 1)
            media_inputs = {
                item["role"]: item
                for item in manifest["inputs"]
                if str(item["role"]).startswith("windows-media-")
            }
            self.assertEqual(
                media_inputs["windows-media-corresponding-source"]["sha256"],
                inputs.windows_media_corresponding_source.sha256,
            )
            self.assertEqual(
                media_inputs["windows-media-runtime"]["sha256"],
                inputs.windows_media_runtime.sha256,
            )
            encoded = (first / "PORTABLE-MANIFEST.json").read_text(encoding="utf-8")
            self.assertNotIn(str(root), encoded)
            self.assertNotIn("owner", " ".join(item["filename"] for item in manifest["inputs"]))

    def test_exact_input_hash_mismatch_removes_partial_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = _write(root / "source.bin", b"expected")
            wrong = portable.ExactInput(source.path, "0" * 64, source.label)
            destination = root / "copied.bin"
            with self.assertRaisesRegex(portable.PortableBuildError, "SHA-256 mismatch"):
                portable._copy_exact_input(wrong, destination, portable.DEFAULT_EPOCH)
            self.assertFalse(destination.exists())

    def test_unsafe_and_duplicate_archive_members_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unsafe = root / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("../escape", b"escape")
            with (
                zipfile.ZipFile(unsafe) as archive,
                self.assertRaisesRegex(
                    portable.PortableBuildError,
                    "traversal",
                ),
            ):
                portable._archive_members(archive, "unsafe")

            duplicate = root / "duplicate.zip"
            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("Name.txt", b"first")
                archive.writestr("name.TXT", b"second")
            with (
                zipfile.ZipFile(duplicate) as archive,
                self.assertRaisesRegex(
                    portable.PortableBuildError,
                    "duplicate portable paths",
                ),
            ):
                portable._archive_members(archive, "duplicate")

            for index, raw_name in enumerate(
                (".//python.exe", "./python.exe", "nested//python.exe", "nested//")
            ):
                with self.subTest(raw_name=raw_name):
                    noncanonical = root / f"noncanonical-{index}.zip"
                    with zipfile.ZipFile(noncanonical, "w") as archive:
                        archive.writestr(raw_name, b"payload")
                    with (
                        zipfile.ZipFile(noncanonical) as archive,
                        self.assertRaisesRegex(
                            portable.PortableBuildError,
                            "canonical|relative path",
                        ),
                    ):
                        portable._archive_members(archive, "noncanonical")

    def test_wheel_record_tampering_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            wheel = _wheel(
                root / "groove_serpent-1.0.0-py3-none-any.whl",
                "groove-serpent",
                "1.0.0",
                {"groove_serpent/__init__.py": b"original"},
            )
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(wheel.exact.path, "a") as archive:
                    archive.writestr("groove_serpent/__init__.py", b"tampered")
            tampered = portable.WheelInput(
                portable.ExactInput(
                    wheel.exact.path,
                    _sha256(wheel.exact.path),
                    wheel.exact.label,
                ),
                wheel.distribution,
                wheel.version,
            )
            with self.assertRaisesRegex(portable.PortableBuildError, "duplicate portable paths"):
                archive, _ = portable._verify_wheel(tampered.exact.path, tampered)
                archive.close()

    def test_manifest_reopen_detects_payload_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = _inputs(root / "inputs")
            output_root = root / "output"
            output_root.mkdir()
            with (
                mock.patch.object(
                    portable,
                    "_smoke_bundle",
                    side_effect=_synthetic_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_windows_media_runtime",
                    side_effect=_synthetic_media_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_packaged_verifier",
                    side_effect=_synthetic_verifier,
                ),
                mock.patch.object(
                    portable,
                    "_publish_new_directory",
                    side_effect=lambda stage, final: os.rename(stage, final),
                ),
            ):
                output = portable.build_portable_directory(inputs, output_root)
            (output / "README-PORTABLE.txt").write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(portable.PortableBuildError, "does not exactly match"):
                portable._verify_manifest(output)

    def test_private_path_in_explicit_notice_fails_output_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = _inputs(root / "inputs")
            private_notice = _write(
                root / "inputs" / "private-notice.txt",
                (
                    inputs.third_party_notices.path.read_bytes()
                    + str(Path.home()).encode("utf-8")
                    + b"\\private.flac\n"
                ),
            )
            inputs = portable.PortableInputs(
                app_wheel=inputs.app_wheel,
                dependency_wheels=inputs.dependency_wheels,
                python_embed=inputs.python_embed,
                python_version=inputs.python_version,
                windows_media_runtime=inputs.windows_media_runtime,
                windows_media_corresponding_source=(inputs.windows_media_corresponding_source),
                groove_license=inputs.groove_license,
                third_party_notices=private_notice,
                portable_verifier=inputs.portable_verifier,
                skill_files=inputs.skill_files,
            )
            output_root = root / "output"
            output_root.mkdir()
            with (
                mock.patch.object(
                    portable,
                    "_smoke_bundle",
                    side_effect=_synthetic_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_windows_media_runtime",
                    side_effect=_synthetic_media_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_packaged_verifier",
                    side_effect=_synthetic_verifier,
                ),
                mock.patch.object(
                    portable,
                    "_publish_new_directory",
                    side_effect=lambda stage, final: os.rename(stage, final),
                ),
                self.assertRaisesRegex(portable.PortableBuildError, "private material"),
            ):
                portable.build_portable_directory(inputs, output_root)
            self.assertEqual(list(output_root.iterdir()), [])

    def test_non_windows_publication_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            stage = root / "stage"
            final = root / "final"
            stage.mkdir()
            with (
                mock.patch.object(portable.os, "name", "posix"),
                self.assertRaisesRegex(
                    portable.PortableBuildError,
                    "native Windows",
                ),
            ):
                portable._publish_new_directory(stage, final)
            self.assertTrue(stage.is_dir())
            self.assertFalse(final.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_destination_junction_is_rejected_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            junction = root / "output-junction"
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
                self.assertFalse(junction.is_symlink())
                with self.assertRaisesRegex(
                    portable.PortableBuildError,
                    "plain, existing directory ancestry",
                ):
                    portable._validate_destination(junction, "1.0.0")
            finally:
                os.rmdir(junction)

    def test_failed_build_cleanup_preserves_swapped_stage_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = _inputs(root / "inputs")
            output_root = root / "output"
            displaced = root / "displaced-owned-stage"
            output_root.mkdir()
            swapped_stage: Path | None = None

            def swap_stage(stage: Path, *_args: object) -> dict[str, object]:
                nonlocal swapped_stage
                swapped_stage = stage
                os.rename(stage, displaced)
                stage.mkdir()
                (stage / "winner.txt").write_text(
                    "independent owner\n",
                    encoding="utf-8",
                )
                raise portable.PortableBuildError("synthetic build failure")

            with (
                mock.patch.object(
                    portable,
                    "_smoke_bundle",
                    side_effect=swap_stage,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_windows_media_runtime",
                    side_effect=_synthetic_media_smoke,
                ),
                self.assertRaisesRegex(
                    portable.PortableBuildError,
                    "synthetic build failure",
                ) as caught,
            ):
                portable.build_portable_directory(inputs, output_root)

            self.assertIsNotNone(swapped_stage)
            assert swapped_stage is not None
            self.assertEqual(
                (swapped_stage / "winner.txt").read_text(encoding="utf-8"),
                "independent owner\n",
            )
            self.assertTrue((displaced / "README-PORTABLE.txt").is_file())
            self.assertTrue(
                any("lost ownership" in note for note in getattr(caught.exception, "__notes__", ()))
            )

    def test_rename_completion_before_interrupt_removes_published_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = _inputs(root / "inputs")
            output_root = root / "output"
            output_root.mkdir()

            def publish_then_interrupt(stage: Path, final: Path) -> None:
                os.rename(stage, final)
                raise KeyboardInterrupt

            with (
                mock.patch.object(
                    portable,
                    "_smoke_bundle",
                    side_effect=_synthetic_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_windows_media_runtime",
                    side_effect=_synthetic_media_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_packaged_verifier",
                    side_effect=_synthetic_verifier,
                ),
                mock.patch.object(
                    portable,
                    "_publish_new_directory",
                    side_effect=publish_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                portable.build_portable_directory(inputs, output_root)

            self.assertEqual(list(output_root.iterdir()), [])

    def test_manifest_records_every_payload_member_except_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = _inputs(root / "inputs")
            output_root = root / "output"
            output_root.mkdir()
            with (
                mock.patch.object(
                    portable,
                    "_smoke_bundle",
                    side_effect=_synthetic_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_windows_media_runtime",
                    side_effect=_synthetic_media_smoke,
                ),
                mock.patch.object(
                    portable,
                    "_smoke_packaged_verifier",
                    side_effect=_synthetic_verifier,
                ),
                mock.patch.object(
                    portable,
                    "_publish_new_directory",
                    side_effect=lambda stage, final: os.rename(stage, final),
                ),
            ):
                output = portable.build_portable_directory(inputs, output_root)
            manifest = json.loads((output / "PORTABLE-MANIFEST.json").read_text(encoding="utf-8"))
            recorded = {item["path"] for item in manifest["payload"]["members"]}
            actual = {
                path.relative_to(output).as_posix()
                for path in output.rglob("*")
                if path.is_file() and path.name != "PORTABLE-MANIFEST.json"
            }
            self.assertEqual(recorded, actual)


if __name__ == "__main__":
    unittest.main()
