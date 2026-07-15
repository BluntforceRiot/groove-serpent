from __future__ import annotations

import base64
import ctypes
import gzip
import hashlib
import io
import os
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from typing import cast
from unittest import mock

from scripts import _release_fs, build_python_distributions as builder


ROOT = Path(__file__).resolve().parent.parent
WHEEL_NAME = "groove_serpent-1.0.0-py3-none-any.whl"


def _require_publication_filesystem(path: Path, context: str) -> None:
    try:
        _release_fs.require_stable_creation_identity(path, context)
    except RuntimeError as exc:
        raise unittest.SkipTest(str(exc)) from exc


def _wheel_payload(
    *,
    tamper_after_record: bool = False,
    contradictory_identity: bool = False,
    contradictory_compatibility: bool = False,
) -> bytes:
    prefix = "groove_serpent-1.0.0.dist-info"
    metadata = b"Metadata-Version: 2.4\nName: groove-serpent\nVersion: 1.0.0\n"
    if contradictory_identity:
        metadata += b"Name: another-project\nVersion: 9.9.9\n"
    metadata += b"\n"
    wheel = b"Wheel-Version: 1.0\nGenerator: test\n"
    if contradictory_compatibility:
        wheel += b"Root-Is-Purelib: false\nTag: cp313-cp313-win_amd64\n"
    wheel += b"Root-Is-Purelib: true\nTag: py3-none-any\n\n"
    members = {
        "groove_serpent/__init__.py": b'__version__ = "1.0.0"\n',
        "groove_serpent/web/index.html": b"<!doctype html>\n",
        "groove_serpent/web/app.js": b"export {};\n",
        "groove_serpent/web/styles.css": b"body {}\n",
        f"{prefix}/METADATA": metadata,
        f"{prefix}/WHEEL": wheel,
    }
    record_name = f"{prefix}/RECORD"
    rows = []
    for relative, payload in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        rows.append(f"{relative},sha256={digest.decode()},{len(payload)}\n")
    rows.append(f"{record_name},,\n")
    members[record_name] = "".join(rows).encode("utf-8")
    if tamper_after_record:
        members["groove_serpent/__init__.py"] += b"# changed\n"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, payload in members.items():
            info = zipfile.ZipInfo(relative, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return stream.getvalue()


def _raw_sdist(entries: list[tarfile.TarInfo], payloads: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for info in entries:
            payload = payloads.get(info.name, b"")
            archive.addfile(info, io.BytesIO(payload) if info.isfile() else None)
    return stream.getvalue()


def _wheel_with_declared_deflate_junk(payload: bytes) -> bytes:
    junk = b"DECLARED-COMPRESSED-JUNK"
    eocd = list(struct.unpack("<4s4H2LH", payload[-22:]))
    central_offset = cast(int, eocd[6])
    local = struct.unpack("<4s5H3L2H", payload[:30])
    compressed_size = local[7]
    data_end = 30 + local[9] + local[10] + compressed_size
    forged = bytearray(payload[:data_end] + junk + payload[data_end:])
    struct.pack_into("<L", forged, 18, compressed_size + len(junk))

    shifted_central = central_offset + len(junk)
    central_cursor = shifted_central
    for index in range(cast(int, eocd[4])):
        central = struct.unpack_from("<4s6H3L5H2L", forged, central_cursor)
        if index == 0:
            struct.pack_into("<L", forged, central_cursor + 20, central[8] + len(junk))
        else:
            struct.pack_into("<L", forged, central_cursor + 42, central[16] + len(junk))
        central_cursor += 46 + central[10] + central[11] + central[12]
    eocd[6] = shifted_central
    forged[-22:] = struct.pack("<4s4H2LH", *eocd)
    return bytes(forged)


def _pid_is_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return int(kernel32.WaitForSingleObject(handle, 0)) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


class BuildPythonDistributionsTests(unittest.TestCase):
    def test_constraints_pin_one_backend_with_both_expected_hashes(self) -> None:
        payload = (ROOT / "packaging" / "python-build-constraints.txt").read_bytes()
        receipt = builder._constraints(payload)

        self.assertEqual(receipt["requirement"], "setuptools==83.0.0")
        hashes = cast(list[str], receipt["distribution_hashes"])
        self.assertEqual(set(hashes), builder.SETUPTOOLS_HASHES)
        self.assertEqual(receipt["file_sha256"], hashlib.sha256(payload).hexdigest())

    def test_private_path_audit_rejects_both_windows_separator_forms(self) -> None:
        backslash = b"\\"
        slash = b"/"
        private_album = b"Mystery" + b".flac"
        private_payloads = (
            backslash.join((b"C:", b"Users", b"Owner", b"private.flac")),
            slash.join((b"C:", b"Users", b"Owner", b"private.flac")),
            backslash.join((b"N:", b"HomelabForge", b"Groove Serpent", private_album)),
            slash.join((b"N:", b"HomelabForge", b"Groove Serpent", private_album)),
        )
        for payload in private_payloads:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                RuntimeError,
                "private material",
            ):
                builder._audit_payload("module.txt", payload, "Test payload")

    def test_package_snapshot_excludes_generated_bytecode(self) -> None:
        records = builder._package_records(ROOT)
        names = {record.relative for record in records}

        self.assertIn("pyproject.toml", names)
        self.assertIn("src/groove_serpent/web/index.html", names)
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))
        self.assertEqual(len(names), len(records))

    def test_package_authority_rejects_an_in_tree_backend_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = (ROOT / "pyproject.toml").read_bytes()
            payload = payload.replace(
                b'build-backend = "setuptools.build_meta"\n',
                b'build-backend = "setuptools.build_meta"\n'
                b'backend-path = ["src/groove_serpent/vendor"]\n',
                1,
            )
            (root / "pyproject.toml").write_bytes(payload)

            with self.assertRaisesRegex(RuntimeError, "exact external setuptools"):
                builder._package_metadata(root)

    def test_windows_wrapper_disables_bytecode_before_importing_builder(self) -> None:
        bootstrap = builder.WINDOWS_WRAPPER_BOOTSTRAP
        self.assertLess(
            bootstrap.index("sys.dont_write_bytecode=True"),
            bootstrap.index("from scripts.build_python_distributions"),
        )

    def test_builder_help_bootstraps_from_a_clean_isolated_interpreter(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                str(ROOT / "scripts" / "build_python_distributions.py"),
                "--help",
            ],
            cwd=Path(tempfile.gettempdir()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertIn(b"audited, reproducible", completed.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows process-job wrapper only")
    def test_windows_wrapper_bootstraps_without_site_packages(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                builder.WINDOWS_WRAPPER_BOOTSTRAP,
                str(ROOT),
                sys.executable,
                "-c",
                "pass",
            ],
            cwd=Path(tempfile.gettempdir()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))

    def test_normalized_sdist_is_exact_ustar_in_standard_gzip(self) -> None:
        records = (
            builder.FileRecord("groove_serpent-1.0.0/PKG-INFO", b"Name: groove-serpent\n"),
            builder.FileRecord("groove_serpent-1.0.0/src/module.py", b"VALUE = 1\n"),
        )

        first = builder._normalized_sdist(records)
        second = builder._normalized_sdist(records)

        self.assertEqual(first, second)
        tar_payload = gzip.decompress(first)
        self.assertEqual(tar_payload, builder._canonical_tar(records))
        with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
            members = archive.getmembers()
            self.assertEqual([member.name for member in members], [r.relative for r in records])
            for member in members:
                self.assertTrue(member.isfile())
                self.assertEqual(member.mtime, builder.SOURCE_DATE_EPOCH)
                self.assertEqual((member.uid, member.gid), (0, 0))
                self.assertEqual(member.mode, 0o644)
                self.assertEqual((member.uname, member.gname), ("", ""))

    def test_sdist_rejects_suffix_concatenation_and_nonzero_tar_padding(self) -> None:
        records = (
            builder.FileRecord("groove_serpent-1.0.0/PKG-INFO", b"Name: groove-serpent\n"),
        )
        valid = builder._normalized_sdist(records)
        tar_payload = bytearray(builder._canonical_tar(records))
        tar_payload[-1] = 1
        cases = (
            (valid + b"EXTRA-SUFFIX", "one exact bounded gzip stream"),
            (valid + valid, "one exact bounded gzip stream"),
            (builder._stored_gzip(bytes(tar_payload)), "malformed tar ending"),
        )

        for payload, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                builder._sdist_records(payload, "groove_serpent-1.0.0")

    def test_sdist_rejects_links_traversal_and_portable_collisions(self) -> None:
        cases: list[tuple[list[tarfile.TarInfo], dict[str, bytes], str]] = []
        link = tarfile.TarInfo("groove_serpent-1.0.0/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "target"
        cases.append(([link], {}, "link or special"))
        traversal = tarfile.TarInfo("groove_serpent-1.0.0/../outside")
        traversal.size = 1
        cases.append(([traversal], {traversal.name: b"x"}, "traversal"))
        upper = tarfile.TarInfo("groove_serpent-1.0.0/File.txt")
        upper.size = 1
        lower = tarfile.TarInfo("groove_serpent-1.0.0/file.txt")
        lower.size = 1
        cases.append(
            ([upper, lower], {upper.name: b"a", lower.name: b"b"}, "Portable sdist collision")
        )

        for entries, payloads, message in cases:
            with self.subTest(message=message):
                archive = _raw_sdist(entries, payloads)
                with self.assertRaisesRegex(RuntimeError, message):
                    builder._sdist_records(archive, "groove_serpent-1.0.0")

    def test_wheel_record_is_exact_and_tampering_is_rejected(self) -> None:
        valid = _wheel_payload()
        audit = builder._wheel_audit(valid, WHEEL_NAME, "groove-serpent", "1.0.0")
        self.assertEqual(audit.member_count, 7)

        with self.assertRaisesRegex(RuntimeError, "RECORD does not bind"):
            builder._wheel_audit(
                _wheel_payload(tamper_after_record=True),
                WHEEL_NAME,
                "groove-serpent",
                "1.0.0",
            )

    def test_wheel_rejects_contradictory_identity_and_compatibility_metadata(self) -> None:
        cases = (
            (_wheel_payload(contradictory_identity=True), "exactly one authoritative"),
            (
                _wheel_payload(contradictory_compatibility=True),
                "exactly one pure-Python tag",
            ),
        )
        for payload, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                builder._wheel_audit(
                    payload,
                    WHEEL_NAME,
                    "groove-serpent",
                    "1.0.0",
                )

    def test_wheel_rejects_unbound_bytes_between_local_and_central_records(self) -> None:
        valid = _wheel_payload()
        eocd = list(struct.unpack("<4s4H2LH", valid[-22:]))
        central_offset = cast(int, eocd[6])
        gap = b"UNBOUND-WHEEL-BYTES!!X"
        self.assertEqual(len(gap), 22)
        eocd[6] = central_offset + len(gap)
        malformed = (
            valid[:central_offset]
            + gap
            + valid[central_offset:-22]
            + struct.pack("<4s4H2LH", *eocd)
        )

        with self.assertRaisesRegex(RuntimeError, "noncanonical ZIP data"):
            builder._wheel_audit(
                malformed,
                WHEEL_NAME,
                "groove-serpent",
                "1.0.0",
            )

    def test_wheel_rejects_trailing_bytes_after_end_record(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "noncanonical ZIP data"):
            builder._wheel_audit(
                _wheel_payload() + b"TRAILING-JUNK",
                WHEEL_NAME,
                "groove-serpent",
                "1.0.0",
            )

    def test_wheel_rejects_junk_inside_a_declared_deflate_span(self) -> None:
        malformed = _wheel_with_declared_deflate_junk(_wheel_payload())
        with zipfile.ZipFile(io.BytesIO(malformed)) as archive:
            self.assertIsNone(archive.testzip())

        with self.assertRaisesRegex(RuntimeError, "noncanonical ZIP data"):
            builder._wheel_audit(
                malformed,
                WHEEL_NAME,
                "groove-serpent",
                "1.0.0",
            )

    def test_declared_oversized_wheel_member_is_rejected_before_read(self) -> None:
        with (
            mock.patch.object(builder, "MAX_ARCHIVE_FILE_BYTES", 1),
            mock.patch.object(
                zipfile.ZipFile,
                "read",
                side_effect=AssertionError("archive.read must not run"),
            ),
            self.assertRaisesRegex(RuntimeError, "byte ceiling"),
        ):
            builder._wheel_audit(
                _wheel_payload(),
                WHEEL_NAME,
                "groove-serpent",
                "1.0.0",
            )

    def test_no_replace_directory_rename_preserves_a_collision_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            output = root / "output"
            stage.mkdir()
            output.mkdir()
            (stage / "owned.txt").write_text("owned\n", encoding="utf-8")
            (output / "winner.txt").write_text("winner\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                _release_fs.rename_no_replace(stage, output)

            self.assertEqual((stage / "owned.txt").read_text(encoding="utf-8"), "owned\n")
            self.assertEqual((output / "winner.txt").read_text(encoding="utf-8"), "winner\n")

    def test_existing_output_is_refused_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _require_publication_filesystem(
                Path(temporary), "Python distribution output"
            )
            output = Path(temporary) / "dist"
            output.mkdir()
            sentinel = output / "sentinel.txt"
            sentinel.write_text("keep\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                builder.build_python_distributions(output, root=ROOT)

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_work_root_inside_package_source_is_refused_without_mutation(self) -> None:
        before = builder._package_records(ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            _require_publication_filesystem(
                Path(temporary), "Python distribution output"
            )
            output = Path(temporary) / "dist"
            with self.assertRaisesRegex(RuntimeError, "work root overlaps"):
                builder.build_python_distributions(
                    output,
                    root=ROOT,
                    work_parent=ROOT / "src" / "groove_serpent",
                )

        self.assertEqual(builder._package_records(ROOT), before)

    def test_timeout_reaps_owned_tool_process_tree(self) -> None:
        if sys.platform == "darwin":
            self.skipTest("Exact process-tree containment is supported on Linux and Windows.")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid = root / "child.pid"
            grandchild_pid = root / "grandchild.pid"
            probe = root / "probe.py"
            probe.write_text(
                "import subprocess, sys, time\n"
                "code = (\"import os,pathlib,signal,subprocess,sys,time;\"\n"
                "        \"signal.signal(signal.SIGTERM,signal.SIG_IGN);\"\n"
                "        \"grand=subprocess.Popen([sys.executable,'-c',\"\n"
                "        \"'import signal,time;signal.signal(signal.SIGTERM,\"\n"
                "        \"signal.SIG_IGN);time.sleep(30)']);\"\n"
                "        \"pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));\"\n"
                "        \"pathlib.Path(sys.argv[2]).write_text(str(grand.pid));\"\n"
                "        \"time.sleep(30)\")\n"
                "child = subprocess.Popen([sys.executable, '-c', code, sys.argv[1], "
                "sys.argv[2]])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            environment = dict(os.environ)
            with self.assertRaisesRegex(RuntimeError, "exceeded|survived forced cleanup"):
                builder._run_bounded(
                    (sys.executable, str(probe), str(child_pid), str(grandchild_pid)),
                    root,
                    environment,
                    timeout_seconds=1.0,
                )
            self.assertTrue(child_pid.is_file())
            self.assertTrue(grandchild_pid.is_file())
            pids = {
                int(child_pid.read_text(encoding="ascii")),
                int(grandchild_pid.read_text(encoding="ascii")),
            }
            deadline = time.monotonic() + 3.0
            while any(_pid_is_alive(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(0.02)
            survivors = sorted(pid for pid in pids if _pid_is_alive(pid))
            self.assertFalse(survivors, f"owned stubborn descendants survived: {survivors}")

    def test_tool_output_ceiling_stops_the_owned_process_without_log_files(self) -> None:
        if sys.platform == "darwin":
            self.skipTest("Exact process-tree containment is supported on Linux and Windows.")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = (
                sys.executable,
                "-c",
                "import os,time;os.write(1,b'x'*65536);time.sleep(30)",
            )
            with (
                mock.patch.object(builder, "MAX_TOOL_OUTPUT_BYTES", 1024),
                self.assertRaisesRegex(RuntimeError, "output byte ceiling"),
            ):
                builder._run_bounded(
                    command,
                    root,
                    dict(os.environ),
                    timeout_seconds=5.0,
                )

            self.assertEqual(list(root.iterdir()), [])

    @unittest.skipUnless(sys.platform == "linux", "Linux subreaper containment only")
    def test_timeout_reaps_descendant_that_escapes_the_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            escaped_pid = root / "escaped.pid"
            probe = root / "probe.py"
            probe.write_text(
                "import subprocess, sys, time\n"
                "code = ('import os,pathlib,sys,time;'\n"
                "        'os.setsid();'\n"
                "        'pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));'\n"
                "        'time.sleep(30)')\n"
                "subprocess.Popen([sys.executable, '-c', code, sys.argv[1]])\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "exceeded|survived forced cleanup"):
                builder._run_bounded(
                    (sys.executable, str(probe), str(escaped_pid)),
                    root,
                    dict(os.environ),
                    timeout_seconds=1.0,
                )

            deadline = time.monotonic() + 2.0
            while not escaped_pid.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(escaped_pid.exists())
            escaped = int(escaped_pid.read_text(encoding="ascii"))
            self.assertFalse(_pid_is_alive(escaped), f"escaped descendant survived: {escaped}")


if __name__ == "__main__":
    unittest.main()
