from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts._release_fs import require_stable_creation_identity
from scripts import _release_evidence, build_public_archive
from tests._release_evidence_fixtures import write_public_release_commit


class BuildPublicArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        if self._testMethodName == "test_member_bytes_are_platform_independent":
            return
        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                require_stable_creation_identity(
                    Path(temp_dir),
                    "Public source archive test output",
                )
            except RuntimeError as exc:
                self.skipTest(str(exc))

    @staticmethod
    def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [build_public_archive._git_executable(), "-C", str(root), *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @classmethod
    def _commit(cls, root: Path) -> None:
        write_public_release_commit(root, release_version=build_public_archive.VERSION)
        cls._git(root, "init", "--quiet")
        cls._git(root, "config", "user.name", "Release Test")
        cls._git(root, "config", "user.email", "release-test@example.invalid")
        cls._git(root, "add", "--all")
        cls._git(root, "commit", "--quiet", "--message", "Release fixture")

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
            (source / "README.md").write_bytes(b"hello\n")
            self._commit(source)
            first_dir = base / "first"
            second_dir = base / "second"
            first_dir.mkdir()
            second_dir.mkdir()
            first = first_dir / f"{build_public_archive.RELEASE_NAME}-source.zip"
            second = second_dir / f"{build_public_archive.RELEASE_NAME}-source.zip"
            first_manifest = first_dir / "SOURCE_MANIFEST.sha256"
            second_manifest = second_dir / "SOURCE_MANIFEST.sha256"
            first_marker = first_dir / build_public_archive.MARKER_NAME
            second_marker = second_dir / build_public_archive.MARKER_NAME
            first_result = build_public_archive.build_archive(
                first,
                first_manifest,
                root=source,
                marker_path=first_marker,
            )
            second_result = build_public_archive.build_archive(
                second,
                second_manifest,
                root=source,
                marker_path=second_marker,
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_result, second_result)
            self.assertEqual(first_manifest.read_bytes(), second_manifest.read_bytes())
            self.assertEqual(first_marker.read_bytes(), second_marker.read_bytes())
            build_public_archive.verify_source_archive_commit(
                first_marker,
                first,
                first_manifest,
                expected_commit=self._git(source, "rev-parse", "HEAD").stdout.decode().strip(),
            )
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        f"{build_public_archive.RELEASE_NAME}/PUBLIC_RELEASE_COMMIT.json",
                        f"{build_public_archive.RELEASE_NAME}/README.md",
                        f"{build_public_archive.RELEASE_NAME}/SOURCE_MANIFEST.sha256",
                    ],
                )

    def test_candidate_archive_accepts_pr_commit_but_release_mode_rejects_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            readme = source / "README.md"
            readme.write_bytes(b"release payload\n")
            self._commit(source)

            readme.write_bytes(b"ordinary pull-request payload\n")
            self._git(source, "add", "README.md")
            self._git(source, "commit", "--quiet", "--message", "PR payload")
            commit = self._git(source, "rev-parse", "HEAD").stdout.decode().strip()

            archive = base / f"{build_public_archive.RELEASE_NAME}-source.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"
            marker = base / build_public_archive.MARKER_NAME
            with self.assertRaisesRegex(
                RuntimeError,
                "Public release commit does not match the exact public payload",
            ):
                build_public_archive.build_archive(
                    archive,
                    manifest,
                    root=source,
                    marker_path=marker,
                )

            build_public_archive.build_archive(
                archive,
                manifest,
                root=source,
                marker_path=marker,
                authority_mode="candidate",
            )
            build_public_archive.verify_source_archive_commit(
                marker,
                archive,
                manifest,
                expected_commit=commit,
                authority_mode="candidate",
            )
            candidate_marker = _release_evidence.strict_json_object(
                marker.read_bytes(),
                "Candidate source marker",
            )
            self.assertEqual(
                candidate_marker["schema"],
                build_public_archive.CANDIDATE_MARKER_SCHEMA,
            )
            with self.assertRaisesRegex(RuntimeError, "keys are invalid|schema or version"):
                build_public_archive.verify_source_archive_commit(
                    marker,
                    archive,
                    manifest,
                    expected_commit=commit,
                )

    def test_git_authority_ignores_inherited_repository_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            forged = base / "forged"
            source.mkdir()
            forged.mkdir()
            (source / "README.md").write_bytes(b"canonical\n")
            (forged / "README.md").write_bytes(b"forged\n")
            self._commit(source)
            self._commit(forged)
            # Match the foreign tree while making the authoritative checkout
            # dirty. A Git environment override used to bless these bytes.
            (source / "README.md").write_bytes(b"forged\n")
            archive = base / "release.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "GIT_DIR": str(forged / ".git"),
                        "GIT_WORK_TREE": str(source),
                        "GIT_INDEX_FILE": str(forged / ".git" / "index"),
                    },
                    clear=False,
                ),
                self.assertRaisesRegex(RuntimeError, "clean Git checkout"),
            ):
                build_public_archive.build_archive(archive, manifest, root=source)

            self.assertFalse(archive.exists())
            self.assertFalse(manifest.exists())

    def test_git_replacement_ref_cannot_rewrite_release_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            readme = source / "README.md"
            readme.write_bytes(b"canonical\n")
            self._commit(source)
            canonical = self._git(source, "rev-parse", "HEAD").stdout.strip()
            readme.write_bytes(b"forged\n")
            self._git(source, "add", "--all")
            self._git(source, "commit", "--quiet", "--message", "Forged tree")
            forged = self._git(source, "rev-parse", "HEAD").stdout.strip()
            self._git(source, "replace", canonical.decode(), forged.decode())
            self._git(source, "reset", "--hard", canonical.decode())
            archive = base / "release.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"

            with self.assertRaisesRegex(
                RuntimeError,
                "clean Git checkout|Checkout bytes differ",
            ):
                build_public_archive.build_archive(archive, manifest, root=source)

            self.assertFalse(archive.exists())
            self.assertFalse(manifest.exists())

    def test_generated_manifest_refuses_portable_case_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "source_manifest.sha256").write_bytes(b"tracked collision\n")
            self._commit(source)
            archive = base / "release.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"

            with self.assertRaisesRegex(RuntimeError, "collides"):
                build_public_archive.build_archive(archive, manifest, root=source)

            self.assertFalse(archive.exists())
            self.assertFalse(manifest.exists())

    def test_clean_git_blob_with_forward_slash_private_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(
                "/".join(("diagnostic workspace: X:", "Users", "synthetic-owner", "release\n"))
                .encode("utf-8")
            )
            self._commit(source)
            archive = base / "release.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"

            with self.assertRaisesRegex(RuntimeError, "private material"):
                build_public_archive.build_archive(archive, manifest, root=source)

            self.assertFalse(archive.exists())
            self.assertFalse(manifest.exists())
            self.assertFalse((base / build_public_archive.MARKER_NAME).exists())

    def test_archive_requires_external_policy_and_rejects_literal_before_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            canary = "synthetic-owner-recording-privacy-canary.flac"
            (source / "fixture.py").write_bytes(
                f"CAPTURE = {canary[:20]!r} {canary[20:]!r}\n".encode("utf-8")
            )
            self._commit(source)
            policy = base / "private-policy.json"
            policy.write_bytes(_release_evidence.canonical_json_bytes({
                "schema": _release_evidence.PRIVATE_POLICY_SCHEMA,
                "forbidden_literals": [canary],
            }))
            for mode in ("candidate", "release"):
                with self.subTest(mode=mode), self.assertRaisesRegex(
                    RuntimeError, "private material"
                ):
                    build_public_archive.build_archive(
                        base / "release.zip",
                        base / "SOURCE_MANIFEST.sha256",
                        root=source,
                        authority_mode=mode,
                        private_policy_path=policy,
                    )
            self.assertFalse((base / "release.zip").exists())
            self.assertFalse((base / "SOURCE_MANIFEST.sha256").exists())

    def test_history_bundle_is_not_a_public_source_member(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir)
            (source / "history.bundle").write_bytes(b"synthetic-git-history")
            with self.assertRaisesRegex(RuntimeError, "private or generated"):
                build_public_archive.included_files(source)

    def test_private_output_names_are_rejected_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"public source\n")
            self._commit(source)
            policy = base / "private-policy.json"
            canary = "synthetic-private-artifact-name"
            policy.write_bytes(_release_evidence.canonical_json_bytes({
                "schema": _release_evidence.PRIVATE_POLICY_SCHEMA,
                "forbidden_literals": [canary],
            }))
            with self.assertRaisesRegex(RuntimeError, "private material"):
                build_public_archive.build_archive(
                    base / (canary + ".zip"), base / "SOURCE_MANIFEST.sha256",
                    root=source, private_policy_path=policy,
                )
            self.assertEqual(
                {path.name for path in base.iterdir()}, {"source", "private-policy.json"}
            )

    def test_independent_candidate_verifier_applies_private_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            canary = "synthetic-owner-recording-privacy-canary.flac"
            (source / "README.md").write_text(canary, encoding="utf-8")
            self._commit(source)
            archive = base / "release.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"
            build_public_archive.build_archive(
                archive, manifest, root=source, authority_mode="candidate"
            )
            policy = _release_evidence.PrivateContentPolicy("a" * 64, (canary,))
            with self.assertRaisesRegex(RuntimeError, "private material"):
                build_public_archive.verify_source_archive_commit(
                    base / build_public_archive.MARKER_NAME,
                    archive,
                    manifest,
                    authority_mode="candidate",
                    policy=policy,
                )

    @unittest.skipIf(os.name == "nt", "Control-character filename is POSIX-only")
    def test_control_character_git_member_is_rejected_before_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "bad\nname.md").write_bytes(b"ambiguous\n")
            self._git(source, "init", "--quiet")
            self._git(source, "config", "user.name", "Release Test")
            self._git(source, "config", "user.email", "release-test@example.invalid")
            self._git(source, "add", "--all")
            self._git(source, "commit", "--quiet", "--message", "Release fixture")
            archive = base / "release.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"

            with self.assertRaisesRegex(RuntimeError, "Windows-unsafe character"):
                build_public_archive.build_archive(archive, manifest, root=source)

            self.assertFalse(archive.exists())
            self.assertFalse(manifest.exists())

    def test_refuses_to_replace_an_existing_external_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"hello\n")
            self._commit(source)
            archive = base / "release.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"
            manifest.write_text("preserve me\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                build_public_archive.build_archive(
                    archive,
                    manifest,
                    root=source,
                )

            self.assertFalse(archive.exists())
            self.assertEqual(manifest.read_text(encoding="utf-8"), "preserve me\n")

    def test_worktree_drift_after_audit_is_rejected_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source_root = base / "source"
            source_root.mkdir()
            source = source_root / "README.md"
            original = b"reviewed bytes\n"
            source.write_bytes(original)
            self._commit(source_root)
            archive_path = base / "release.zip"
            manifest_path = base / "SOURCE_MANIFEST.sha256"
            real_read = build_public_archive.read_single_link_file
            swapped = False

            def swap_after_read(path: Path, maximum: int, context: str) -> bytes:
                nonlocal swapped
                payload = real_read(path, maximum, context)
                if path == source and not swapped:
                    source.write_bytes(b"changed after audit\n")
                    swapped = True
                return payload

            with mock.patch.object(
                build_public_archive,
                "read_single_link_file",
                side_effect=swap_after_read,
            ):
                with self.assertRaisesRegex(RuntimeError, "clean Git checkout"):
                    build_public_archive.build_archive(
                        archive_path,
                        manifest_path,
                        root=source_root,
                    )

            self.assertFalse(archive_path.exists())
            self.assertFalse(manifest_path.exists())

    def test_final_member_verification_rejects_corrupt_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"reviewed\n")
            self._commit(source)
            archive_path = base / "release.zip"
            manifest_path = base / "SOURCE_MANIFEST.sha256"
            real_zip_bytes = build_public_archive.zip_bytes

            def corrupt(
                archive: zipfile.ZipFile,
                relative: str,
                payload: bytes,
            ) -> None:
                if relative == "README.md":
                    payload = b"corrupt\n"
                real_zip_bytes(archive, relative, payload)

            with mock.patch.object(
                build_public_archive,
                "zip_bytes",
                side_effect=corrupt,
            ):
                with self.assertRaisesRegex(RuntimeError, "failed verification"):
                    build_public_archive.build_archive(
                        archive_path,
                        manifest_path,
                        root=source,
                    )
            self.assertFalse(archive_path.exists())
            self.assertFalse(manifest_path.exists())

    def test_omitted_tracked_inventory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"reviewed\n")
            (source / "LICENSE").write_bytes(b"license\n")
            self._commit(source)
            real_inventory = build_public_archive.included_files

            def omit_one(root: Path = source) -> list[Path]:
                return [path for path in real_inventory(root) if path.name != "LICENSE"]

            with (
                mock.patch.object(
                    build_public_archive,
                    "included_files",
                    side_effect=omit_one,
                ),
                self.assertRaisesRegex(RuntimeError, "inventory differs"),
            ):
                build_public_archive.build_archive(
                    base / "release.zip",
                    base / "SOURCE_MANIFEST.sha256",
                    root=source,
                )
            self.assertFalse((base / "release.zip").exists())
            self.assertFalse((base / "SOURCE_MANIFEST.sha256").exists())

    def test_ignored_browser_results_do_not_contaminate_git_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"reviewed\n")
            (source / ".gitignore").write_bytes(b".groove-serpent/\n")
            self._commit(source)
            browser_results = source / ".groove-serpent" / "browser-test-results"
            browser_results.mkdir(parents=True)
            (browser_results / ".last-run.json").write_bytes(b'{"status":"passed"}\n')
            archive = base / "release.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"

            build_public_archive.build_archive(
                archive,
                manifest,
                root=source,
            )

            with zipfile.ZipFile(archive) as bundle:
                self.assertFalse(
                    any(".groove-serpent" in name for name in bundle.namelist())
                )

    def test_tracked_generated_path_is_rejected_instead_of_silently_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            generated = source / "src" / "public"
            generated.mkdir(parents=True)
            (source / "README.md").write_bytes(b"reviewed\n")
            (generated / "required.py").write_bytes(b"required = True\n")
            self._commit(source)
            archive = base / "release.zip"
            manifest = base / "SOURCE_MANIFEST.sha256"

            with self.assertRaisesRegex(RuntimeError, "excluded generated path"):
                build_public_archive.build_archive(
                    archive,
                    manifest,
                    root=source,
                )
            self.assertFalse(archive.exists())
            self.assertFalse(manifest.exists())

    def test_concurrent_zip_winner_preserves_committed_manifest_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"reviewed\n")
            self._commit(source)
            archive_path = base / "release.zip"
            manifest_path = base / "SOURCE_MANIFEST.sha256"
            winner = b"independent ZIP winner"
            real_publish = _release_evidence.rename_no_replace

            def win_archive(source_path: Path, destination: Path) -> None:
                if destination == archive_path:
                    destination.write_bytes(winner)
                real_publish(source_path, destination)

            with (
                mock.patch.object(
                    _release_evidence,
                    "rename_no_replace",
                    side_effect=win_archive,
                ),
                self.assertRaises(FileExistsError),
            ):
                build_public_archive.build_archive(
                    archive_path,
                    manifest_path,
                    root=source,
                )

            self.assertEqual(archive_path.read_bytes(), winner)
            self.assertTrue(manifest_path.is_file())
            self.assertFalse((base / build_public_archive.MARKER_NAME).exists())

    def test_head_change_before_marker_preserves_uncommitted_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"reviewed\n")
            self._commit(source)
            archive_path = base / "release.zip"
            manifest_path = base / "SOURCE_MANIFEST.sha256"
            real_verify = build_public_archive._verify_archive
            changed = False

            def change_head(
                path: Path,
                records: list[tuple[str, bytes]],
            ) -> bytes:
                nonlocal changed
                payload = real_verify(path, records)
                if not changed:
                    changed = True
                    (source / "CHANGELOG.md").write_bytes(b"new commit\n")
                    self._git(source, "add", "--all")
                    self._git(source, "commit", "--quiet", "--message", "Race")
                return payload

            with (
                mock.patch.object(
                    build_public_archive,
                    "_verify_archive",
                    side_effect=change_head,
                ),
                self.assertRaisesRegex(RuntimeError, "HEAD changed"),
            ):
                build_public_archive.build_archive(
                    archive_path,
                    manifest_path,
                    root=source,
                )

            self.assertTrue(archive_path.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertFalse((base / build_public_archive.MARKER_NAME).exists())

    def test_archive_rename_completion_before_interrupt_preserves_ambiguous_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"reviewed\n")
            self._commit(source)
            archive_path = base / "release.zip"
            manifest_path = base / "SOURCE_MANIFEST.sha256"
            real_publish = _release_evidence.rename_no_replace

            def publish_then_interrupt(source_path: Path, destination: Path) -> None:
                real_publish(source_path, destination)
                if destination == archive_path:
                    raise KeyboardInterrupt

            with (
                mock.patch.object(
                    _release_evidence,
                    "rename_no_replace",
                    side_effect=publish_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                build_public_archive.build_archive(
                    archive_path,
                    manifest_path,
                    root=source,
                )

            self.assertTrue(archive_path.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertFalse((base / build_public_archive.MARKER_NAME).exists())

    def test_final_verification_rejects_unprofiled_zip_trailer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "source"
            source.mkdir()
            (source / "README.md").write_bytes(b"reviewed\n")
            self._commit(source)
            archive_path = base / "release.zip"
            manifest_path = base / "SOURCE_MANIFEST.sha256"
            real_verify = build_public_archive._verify_archive
            calls = 0

            def append_on_final(
                path: Path,
                records: list[tuple[str, bytes]],
            ) -> bytes:
                nonlocal calls
                calls += 1
                if calls == 2:
                    with path.open("ab") as destination:
                        destination.write(b"UNPROFILED-TRAILER")
                return real_verify(path, records)

            with (
                mock.patch.object(
                    build_public_archive,
                    "_verify_archive",
                    side_effect=append_on_final,
                ),
                self.assertRaisesRegex(RuntimeError, "container layout") as caught,
            ):
                build_public_archive.build_archive(
                    archive_path,
                    manifest_path,
                    root=source,
                )

            self.assertFalse(archive_path.exists())
            self.assertTrue(manifest_path.is_file())
            self.assertFalse((base / build_public_archive.MARKER_NAME).exists())
            stages = list(base.glob(".release.zip.source-archive.*.stage"))
            self.assertEqual(len(stages), 1)
            self.assertTrue(stages[0].read_bytes().endswith(b"UNPROFILED-TRAILER"))
            self.assertEqual(getattr(caught.exception, "__notes__", ()), ())

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_non_symlink_reparse_tree_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            outside = root / "outside"
            junction = source / "linked"
            source.mkdir()
            outside.mkdir()
            (source / "README.md").write_text("reviewed\n", encoding="utf-8")
            (outside / "secret.txt").write_text("private\n", encoding="utf-8")
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode != 0:
                self.skipTest("Directory junction creation is unavailable")
            try:
                with self.assertRaisesRegex(RuntimeError, "reparse point"):
                    build_public_archive.included_files(source)
            finally:
                os.rmdir(junction)

    @unittest.skipUnless(os.name == "nt", "Windows current-directory search regression")
    def test_git_discovery_never_selects_current_directory_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            current = Path(temp_dir)
            fake = current / "git.exe"
            system_binary = Path(os.environ["SystemRoot"]) / "System32" / "hostname.exe"
            shutil.copyfile(system_binary, fake)
            real_git = Path(build_public_archive._git_executable())
            previous = Path.cwd()
            try:
                os.chdir(current)
                with mock.patch.dict(
                    os.environ,
                    {"PATH": str(real_git.parent), "PATHEXT": ".EXE"},
                    clear=False,
                ):
                    observed = Path(build_public_archive._git_executable())
            finally:
                os.chdir(previous)
            self.assertNotEqual(observed, fake)
            self.assertEqual(observed, real_git)


if __name__ == "__main__":
    unittest.main()
