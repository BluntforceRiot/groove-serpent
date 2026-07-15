#!/usr/bin/env python3
"""Verify Windows media runtime/source archives before Groove Serpent uses them."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


MAX_ENTRIES = 512
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
GPG_PROVIDER = Path("/usr/bin/gpg")
RUNTIME_ARCHIVE_NAME = "groove-serpent-windows-media-8.1.2-x86_64.zip"
SOURCE_ARCHIVE_NAME = "groove-serpent-windows-media-8.1.2-corresponding-source.zip"
PUBLICATION_FILENAMES = (
    RUNTIME_ARCHIVE_NAME,
    SOURCE_ARCHIVE_NAME,
    "SHA256SUMS",
)
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
EXPECTED_INPUTS = {
    "inputs/chromaprint-1.6.0.tar.gz": (
        "9d33482e56a1389a37a0d6742c376139fa43e3b8a63d29003222b93db2cb40da"
    ),
    "inputs/ffmpeg-8.1.2.tar.xz": (
        "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
    ),
    "inputs/ffmpeg-8.1.2.tar.xz.asc": (
        "0a0963fccd70597838073f3e31b20f4a4d8cc2b5e577472c9a5a1f22624246f8"
    ),
    "inputs/soxr-0.1.3-Source.tar.xz": (
        "b111c15fdc8c029989330ff559184198c161100a59312f5dc19ddeb9b5a15889"
    ),
    "inputs/zlib-1.3.2.tar.xz": (
        "d7a0654783a4da529d1bb793b7ad9c3318020af77667bcae35f95d0e42a792f3"
    ),
    "inputs/zlib-1.3.2.tar.xz.asc": (
        "03ce710347e2f84fa7ed0a6ae6a93467b08031a3022fc296da40220a83b96667"
    ),
    "recipe/keys/ffmpeg-release-signing-key.asc": (
        "397b3becedcd5a98769967ff1ff8501ddc89f8368b8f766e4701377d7dbaabe5"
    ),
    "recipe/keys/zlib-mark-adler.asc": (
        "27f818fd93326e4531c6b094f0edc4c331a1c77ec6449675a3929ae3274d85ac"
    ),
}
REQUIRED_RECIPE = frozenset(
    {
        "recipe/README.md",
        "recipe/bootstrap-ubuntu-24.04.sh",
        "recipe/build.py",
        "recipe/build.sh",
        "recipe/capability_smoke.py",
        "recipe/make_manifest.py",
        "recipe/ubuntu-24.04-packages.txt",
        "recipe/verify_artifact.py",
        "recipe/verify_build_host.sh",
    }
)
EXPECTED_RUNTIME_FILES = frozenset(
    {
        "avcodec-62.dll",
        "avdevice-62.dll",
        "avfilter-11.dll",
        "avformat-62.dll",
        "avutil-60.dll",
        "BUILD-ENVIRONMENT.txt",
        "BUILD-MANIFEST.json",
        "CAPABILITY-SMOKE.json",
        "FFMPEG-CONFIGURE.txt",
        "FFMPEG-SIGNATURE-VERIFICATION.txt",
        "ffmpeg.exe",
        "ffprobe.exe",
        "libchromaprint.dll",
        "libsoxr.dll",
        "LICENSES/Chromaprint-LICENSE.md",
        "LICENSES/FFmpeg-COPYING.LGPLv2.1",
        "LICENSES/FFmpeg-LICENSE.md",
        "LICENSES/GCC-MinGW-RUNTIME-NOTICE.txt",
        "LICENSES/GPL-3.0.txt",
        "LICENSES/KissFFT-BSD-3-Clause.txt",
        "LICENSES/KissFFT-COPYING",
        "LICENSES/libsoxr-COPYING.LGPL",
        "LICENSES/libsoxr-LICENCE",
        "LICENSES/MinGW-w64-NOTICE.txt",
        "LICENSES/zlib-LICENSE",
        "README.txt",
        "swresample-6.dll",
        "ZLIB-SIGNATURE-VERIFICATION.txt",
    }
)
TOOLCHAIN_AUTHORITY_FILES = frozenset(
    {name.removeprefix("recipe/") for name in REQUIRED_RECIPE}
    | {
        "keys/ffmpeg-release-signing-key.asc",
        "keys/zlib-mark-adler.asc",
    }
)


class VerificationFailure(RuntimeError):
    """An archive failed a structural, hash, provenance, or runtime check."""


class PublicationFailure(RuntimeError):
    """A staged artifact set could not be published without replacement."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


DirectoryIdentity = tuple[int, int, int]
FileIdentity = tuple[int, int, int, int, int, int]
FileEvidence = tuple[str, FileIdentity, str]
PublicationSnapshot = tuple[DirectoryIdentity, tuple[FileEvidence, ...]]
PreparedPublication = tuple[Path, Path, PublicationSnapshot]
RecipeFileIdentity = tuple[int, int, int, int, int, int, int]
RecipeDirectoryIdentity = tuple[int, int, int, int, int]


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _assert_build_paths_disjoint(destination: Path, work_root: Path) -> None:
    """Reject an output directory equal to or lexically below the work root."""

    destination_key = os.path.normcase(os.fspath(_absolute_path(destination)))
    work_root_key = os.path.normcase(os.fspath(_absolute_path(work_root)))
    try:
        common = os.path.commonpath((destination_key, work_root_key))
    except ValueError:
        return
    if common == work_root_key:
        raise PublicationFailure(
            "DIST_DIR must be disjoint from the deterministic build work root."
        )


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _plain_directory_identity(path: Path) -> DirectoryIdentity:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PublicationFailure(f"Could not inspect publication directory: {path}") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
    ):
        raise PublicationFailure(f"Publication path is not a plain directory: {path}")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
    )


def _assert_plain_directory_ancestry(path: Path) -> Path:
    absolute = _absolute_path(path)
    for candidate in reversed((absolute, *absolute.parents)):
        _plain_directory_identity(candidate)
    return absolute


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _bound_file_evidence(path: Path, *, capture: bool = False) -> tuple[FileIdentity, str, bytes]:
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PublicationFailure(f"Could not open staged publication file: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or _is_reparse_point(before) or before.st_nlink != 1:
            raise PublicationFailure(
                f"Staged publication member is not a plain single-link file: {path}"
            )
        digest = hashlib.sha256()
        captured = bytearray()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if capture:
                captured.extend(chunk)
                if len(captured) > 4096:
                    raise PublicationFailure(f"Publication receipt is unexpectedly large: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
    )
    observed_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
    )
    try:
        path_metadata = path.lstat()
    except OSError as exc:
        raise PublicationFailure(f"Publication member changed while reading: {path}") from exc
    observed_path = (
        path_metadata.st_dev,
        path_metadata.st_ino,
        path_metadata.st_mode,
        path_metadata.st_nlink,
        path_metadata.st_size,
        path_metadata.st_mtime_ns,
    )
    if identity != observed_after or identity != observed_path:
        raise PublicationFailure(f"Publication member changed while reading: {path}")
    return identity, digest.hexdigest(), bytes(captured)


def _recipe_file_identity(metadata: os.stat_result) -> RecipeFileIdentity:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _recipe_directory_identity(path: Path) -> RecipeDirectoryIdentity:
    _plain_directory_identity(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PublicationFailure(f"Could not inspect recipe directory: {path}") from exc
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_bound_recipe_file(path: Path) -> tuple[RecipeFileIdentity, str, bytes]:
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PublicationFailure(f"Could not open recipe authority file: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _is_reparse_point(before)
            or before.st_nlink != 1
        ):
            raise PublicationFailure(
                f"Recipe authority member is not a plain single-link file: {path}"
            )
        content = bytearray()
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            content.extend(chunk)
            digest.update(chunk)
            if len(content) > 4 * 1024 * 1024:
                raise PublicationFailure(f"Recipe authority member is unexpectedly large: {path}")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        path_metadata = path.lstat()
    except OSError as exc:
        raise PublicationFailure(f"Recipe authority member changed while reading: {path}") from exc
    identity = _recipe_file_identity(before)
    if (
        identity != _recipe_file_identity(after)
        or identity[:-1] != _recipe_file_identity(path_metadata)[:-1]
    ):
        raise PublicationFailure(f"Recipe authority member changed while reading: {path}")
    return identity, digest.hexdigest(), bytes(content)


def _recipe_authority(root: Path, *, exact_inventory: bool = True) -> dict[str, Any]:
    root = _assert_plain_directory_ancestry(root)
    expected_directories = {"keys"}
    watched_directories = {".": root, "keys": root / "keys"}
    before_directories = {
        name: _recipe_directory_identity(path) for name, path in watched_directories.items()
    }
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    if exact_inventory:
        try:
            candidates = tuple(root.rglob("*"))
        except OSError as exc:
            raise PublicationFailure(f"Could not inventory recipe authority: {root}") from exc
        for candidate in candidates:
            relative = candidate.relative_to(root).as_posix()
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                raise PublicationFailure(
                    f"Could not inspect recipe authority member: {candidate}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
                raise PublicationFailure(f"Recipe authority links are forbidden: {candidate}")
            if stat.S_ISDIR(metadata.st_mode):
                actual_directories.add(relative)
            elif stat.S_ISREG(metadata.st_mode):
                actual_files.add(relative)
            else:
                raise PublicationFailure(f"Unsupported recipe authority member: {candidate}")
    else:
        actual_files.update(TOOLCHAIN_AUTHORITY_FILES)
        actual_directories.update(expected_directories)
    inventory_valid = (
        actual_files == TOOLCHAIN_AUTHORITY_FILES and actual_directories == expected_directories
        if exact_inventory
        else TOOLCHAIN_AUTHORITY_FILES <= actual_files
        and expected_directories <= actual_directories
    )
    if not inventory_valid:
        raise PublicationFailure(
            "Recipe authority inventory is not exact: "
            f"files={sorted(actual_files)}, directories={sorted(actual_directories)}."
        )
    files: list[dict[str, Any]] = []
    for name in sorted(TOOLCHAIN_AUTHORITY_FILES):
        identity, digest, _content = _read_bound_recipe_file(root / name)
        files.append(
            {
                "identity": list(identity),
                "path": name,
                "sha256": digest,
            }
        )
    after_directories = {
        name: _recipe_directory_identity(path) for name, path in watched_directories.items()
    }
    if before_directories != after_directories:
        raise PublicationFailure("Recipe authority directories changed while being inspected.")
    return {
        "directories": [
            {"identity": list(before_directories[name]), "path": name}
            for name in sorted(before_directories)
        ],
        "files": files,
        "schema": "groove-serpent.windows-media-recipe-authority/1",
    }


def _canonical_recipe_authority(authority: dict[str, Any]) -> str:
    return json.dumps(authority, sort_keys=True, separators=(",", ":"))


def _content_authority_sha256(digests: dict[str, str]) -> str:
    encoded = json.dumps(
        {
            "files": [
                {"path": name, "sha256": digests[name]}
                for name in sorted(TOOLCHAIN_AUTHORITY_FILES)
            ],
            "schema": "groove-serpent.windows-media-content-authority/1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_exclusive_snapshot_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0))
    descriptor = os.open(path, flags, 0o400)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError(errno.EIO, "recipe snapshot write made no progress")
            view = view[written:]
        if os.name != "nt":
            fchmod: Any = getattr(os, "fchmod")
            fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_recipe_snapshot(
    source: Path,
    destination: Path,
    expected_authority_sha256: str,
) -> str:
    source = _assert_plain_directory_ancestry(source)
    destination = _absolute_path(destination)
    parent = _assert_plain_directory_ancestry(destination.parent)
    if destination.parent != parent or destination.name in {"", ".", ".."}:
        raise PublicationFailure("Recipe snapshot destination is invalid.")
    source_authority = _recipe_authority(source, exact_inventory=False)
    source_digests = {str(item["path"]): str(item["sha256"]) for item in source_authority["files"]}
    if re.fullmatch(r"[0-9a-f]{64}", expected_authority_sha256) is None or (
        _content_authority_sha256(source_digests) != expected_authority_sha256
    ):
        raise PublicationFailure(
            "Recipe snapshot contents do not match the launcher-bound authority digest."
        )
    os.mkdir(destination, mode=0o700)
    os.mkdir(destination / "keys", mode=0o700)
    if os.name != "nt":
        destination.chmod(0o700)
        (destination / "keys").chmod(0o700)
    for name in sorted(TOOLCHAIN_AUTHORITY_FILES):
        _identity, digest, content = _read_bound_recipe_file(source / name)
        if source_digests.get(name) != digest:
            raise PublicationFailure(f"Recipe authority changed before snapshot copy: {name}")
        _write_exclusive_snapshot_file(destination / name, content)
    if _recipe_authority(source, exact_inventory=False) != source_authority:
        raise PublicationFailure("Recipe authority changed while its private snapshot was created.")
    return _canonical_recipe_authority(_recipe_authority(destination))


def _verify_recipe_snapshot(root: Path, expected_authority: str) -> None:
    observed = _canonical_recipe_authority(_recipe_authority(root))
    if observed != expected_authority:
        raise PublicationFailure("Private recipe snapshot changed after authority binding.")


def _publication_names(path: Path) -> tuple[str, ...]:
    try:
        with os.scandir(path) as entries:
            return tuple(sorted(entry.name for entry in entries))
    except OSError as exc:
        raise PublicationFailure(f"Could not inventory publication directory: {path}") from exc


def _snapshot_publication_directory(path: Path) -> PublicationSnapshot:
    directory_identity = _plain_directory_identity(path)
    try:
        directory_before = path.lstat()
    except OSError as exc:
        raise PublicationFailure(f"Could not inspect publication directory: {path}") from exc
    if os.name != "nt" and stat.S_IMODE(directory_identity[2]) != 0o755:
        raise PublicationFailure("Published directory mode must be exactly 0755.")
    names = _publication_names(path)
    if names != tuple(sorted(PUBLICATION_FILENAMES)):
        raise PublicationFailure(
            "Publication directory must contain exactly the runtime ZIP, source ZIP, "
            "and SHA256SUMS."
        )

    evidence: list[FileEvidence] = []
    receipt = b""
    digests: dict[str, str] = {}
    for name in PUBLICATION_FILENAMES:
        identity, digest, captured = _bound_file_evidence(
            path / name,
            capture=name == "SHA256SUMS",
        )
        if os.name != "nt" and stat.S_IMODE(identity[2]) != 0o644:
            raise PublicationFailure(f"Published member mode must be exactly 0644: {path / name}")
        evidence.append((name, identity, digest))
        digests[name] = digest
        if name == "SHA256SUMS":
            receipt = captured
    expected_receipt = (
        f"{digests[RUNTIME_ARCHIVE_NAME]}  {RUNTIME_ARCHIVE_NAME}\n"
        f"{digests[SOURCE_ARCHIVE_NAME]}  {SOURCE_ARCHIVE_NAME}\n"
    ).encode("ascii")
    if receipt != expected_receipt:
        raise PublicationFailure("SHA256SUMS does not exactly bind the staged archive pair.")
    if _publication_names(path) != names:
        raise PublicationFailure(f"Publication inventory changed while reading: {path}")
    try:
        directory_after = path.lstat()
    except OSError as exc:
        raise PublicationFailure(f"Publication directory changed while reading: {path}") from exc
    if (
        _plain_directory_identity(path) != directory_identity
        or directory_before.st_mtime_ns != directory_after.st_mtime_ns
        or directory_before.st_ctime_ns != directory_after.st_ctime_ns
    ):
        raise PublicationFailure(f"Publication directory changed while reading: {path}")
    return directory_identity, tuple(evidence)


def _rename_directory_no_replace(staged: Path, published: Path) -> None:
    """Use one kernel operation to move a directory only when final is absent."""

    if os.name == "nt":
        os.rename(staged, published)
        return
    if not sys.platform.startswith("linux"):
        raise PublicationFailure(
            "Atomic directory publication is supported only on Linux and Windows."
        )
    library = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2: Any = getattr(library, "renameat2")
    except AttributeError as exc:
        raise PublicationFailure("The host C library does not provide renameat2.") from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(staged),
        _AT_FDCWD,
        os.fsencode(published),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
        raise PublicationFailure(
            "The destination filesystem does not support atomic no-replace directory publication."
        ) from OSError(error, os.strerror(error), os.fspath(published))
    raise OSError(error, os.strerror(error), os.fspath(published))


def _assert_verified_archive_digests(
    snapshot: PublicationSnapshot,
    runtime_sha256: str,
    source_sha256: str,
) -> None:
    expected = {
        RUNTIME_ARCHIVE_NAME: runtime_sha256,
        SOURCE_ARCHIVE_NAME: source_sha256,
    }
    if any(re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in expected.values()):
        raise PublicationFailure("Verified archive digests must be lowercase SHA-256 values.")
    observed = {name: digest for name, _identity, digest in snapshot[1]}
    for name, digest in expected.items():
        if observed.get(name) != digest:
            raise PublicationFailure(
                f"Staged archive no longer matches its verified digest: {name}"
            )


def _prepare_directory_publication(
    staged: Path,
    published: Path,
    expected_archive_digests: tuple[str, str] | None = None,
) -> PreparedPublication:
    staged = _absolute_path(staged)
    published = _absolute_path(published)
    staged_parent = _assert_plain_directory_ancestry(staged.parent)
    published_parent = _assert_plain_directory_ancestry(published.parent)
    if os.path.normcase(os.fspath(staged_parent)) != os.path.normcase(os.fspath(published_parent)):
        raise PublicationFailure("The staged and final publication directories must be siblings.")
    if staged.name in {"", ".", ".."} or published.name in {"", ".", ".."}:
        raise PublicationFailure("Publication directory names are invalid.")
    if _path_lexists(published):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), published)
    snapshot = _snapshot_publication_directory(staged)
    if expected_archive_digests is not None:
        _assert_verified_archive_digests(snapshot, *expected_archive_digests)
    return staged, published, snapshot


def _committed_snapshot_matches(prepared: PreparedPublication) -> bool:
    staged, published, expected = prepared
    if _path_lexists(staged) or not _path_lexists(published):
        return False
    try:
        return _snapshot_publication_directory(published) == expected
    except (OSError, PublicationFailure):
        return False


def _commit_prepared_directory(prepared: PreparedPublication) -> None:
    staged, published, expected = prepared
    try:
        _rename_directory_no_replace(staged, published)
    except BaseException:
        # An asynchronous exception may be delivered immediately after the
        # kernel committed the rename. A complete identity-bound final tree is
        # success; there is nothing to roll back.
        if _committed_snapshot_matches(prepared):
            return
        raise
    if _path_lexists(staged):
        raise PublicationFailure("Staged directory remained after publication.")
    observed = _snapshot_publication_directory(published)
    if observed != expected:
        raise PublicationFailure("Published directory differs from its staged snapshot.")


def _publish_directory_no_replace(staged: Path, published: Path) -> None:
    """Atomically publish one complete artifact directory without replacement."""

    _commit_prepared_directory(_prepare_directory_publication(staged, published))


def _create_publication_stage(published: Path) -> Path:
    """Create and capability-probe a private sibling used as the real stage."""

    published = _absolute_path(published)
    parent = _assert_plain_directory_ancestry(published.parent)
    if _path_lexists(published):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), published)
    prefix = ".groove-serpent-windows-media-stage-"
    initial = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    probed = initial.with_name(f"{initial.name}.ready")
    identity = _plain_directory_identity(initial)
    try:
        _rename_directory_no_replace(initial, probed)
    except BaseException as exc:
        if _path_lexists(probed) and not _path_lexists(initial):
            if _plain_directory_identity(probed) == identity:
                return probed
        if _path_lexists(initial):
            exc.add_note(f"Empty capability-probe stage retained at: {initial}")
        raise
    if _path_lexists(initial) or _plain_directory_identity(probed) != identity:
        raise PublicationFailure("Publication-stage capability probe changed identity.")
    return probed


def _print_failure(prefix: str, exc: BaseException) -> None:
    print(f"{prefix}: {exc}", file=sys.stderr)
    notes = getattr(exc, "__notes__", None)
    if isinstance(notes, list):
        for note in notes:
            if isinstance(note, str):
                print(f"note: {note}", file=sys.stderr)


def _publish_directory_cli(argv: Sequence[str]) -> int:
    if len(argv) != 4:
        print(
            "artifact publication failed: expected STAGED_DIRECTORY FINAL_DIRECTORY "
            "VERIFIED_RUNTIME_SHA256 VERIFIED_SOURCE_SHA256.",
            file=sys.stderr,
        )
        return 2
    previous_handlers: dict[signal.Signals, Any] = {}
    phase = "preparing"
    interrupted_signal: int | None = None

    def interrupted(signum: int, _frame: Any) -> None:
        nonlocal interrupted_signal
        interrupted_signal = signum
        if phase == "preparing":
            raise PublicationFailure(
                f"Artifact publication interrupted before commit by signal {signum}."
            )

    try:
        for signal_name in ("SIGHUP", "SIGINT", "SIGQUIT", "SIGTERM"):
            selected = getattr(signal, signal_name, None)
            if selected is not None:
                previous_handlers[selected] = signal.getsignal(selected)
                signal.signal(selected, interrupted)
        prepared = _prepare_directory_publication(
            Path(argv[0]),
            Path(argv[1]),
            (argv[2], argv[3]),
        )
        phase = "committing"
        _commit_prepared_directory(prepared)
        phase = "committed"
        if interrupted_signal is not None:
            print(
                "artifact publication completed atomically before interruption "
                f"signal {interrupted_signal} was observed.",
                file=sys.stderr,
            )
    except (OSError, PublicationFailure) as exc:
        _print_failure("artifact publication failed", exc)
        return 1
    finally:
        for selected, previous in previous_handlers.items():
            signal.signal(selected, previous)
    return 0


def _create_publication_stage_cli(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print(
            "publication-stage creation failed: expected FINAL_DIRECTORY.",
            file=sys.stderr,
        )
        return 2
    try:
        print(_create_publication_stage(Path(argv[0])))
    except (OSError, PublicationFailure) as exc:
        _print_failure("publication-stage creation failed", exc)
        return 1
    return 0


def _verify_publication_directory_cli(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print(
            "publication verification failed: expected FINAL_DIRECTORY.",
            file=sys.stderr,
        )
        return 2
    try:
        _snapshot_publication_directory(_absolute_path(Path(argv[0])))
    except (OSError, PublicationFailure) as exc:
        print(f"publication verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _create_recipe_snapshot_cli(argv: Sequence[str]) -> int:
    if len(argv) != 3:
        print(
            "recipe snapshot failed: expected SOURCE_DIRECTORY SNAPSHOT_DIRECTORY "
            "LAUNCHER_AUTHORITY_SHA256.",
            file=sys.stderr,
        )
        return 2
    try:
        print(_create_recipe_snapshot(Path(argv[0]), Path(argv[1]), argv[2]))
    except (OSError, PublicationFailure) as exc:
        _print_failure("recipe snapshot failed", exc)
        return 1
    return 0


def _verify_recipe_snapshot_cli(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print(
            "recipe snapshot verification failed: expected SNAPSHOT_DIRECTORY AUTHORITY.",
            file=sys.stderr,
        )
        return 2
    try:
        _verify_recipe_snapshot(Path(argv[0]), argv[1])
    except (OSError, PublicationFailure) as exc:
        _print_failure("recipe snapshot verification failed", exc)
        return 1
    return 0


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationFailure(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                VerificationFailure(f"Non-finite JSON value: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationFailure(f"Invalid JSON in {path.name}.") from exc
    if not isinstance(payload, dict):
        raise VerificationFailure(f"{path.name} does not contain a JSON object.")
    return payload


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if not members or len(members) > MAX_ENTRIES:
        raise VerificationFailure("ZIP member count is empty or exceeds its bound.")
    total = 0
    exact: set[str] = set()
    folded: set[str] = set()
    files: list[zipfile.ZipInfo] = []
    for member in members:
        name = member.filename
        if "\\" in name or "\x00" in name:
            raise VerificationFailure(f"Unsafe ZIP member name: {name!r}")
        path = PurePosixPath(name)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise VerificationFailure(f"Unsafe ZIP member path: {name!r}")
        normalized = path.as_posix().rstrip("/")
        if normalized in exact or normalized.casefold() in folded:
            raise VerificationFailure(f"Duplicate or case-colliding ZIP path: {name!r}")
        exact.add(normalized)
        folded.add(normalized.casefold())
        mode_type = (member.external_attr >> 16) & 0o170000
        if mode_type == stat.S_IFLNK:
            raise VerificationFailure(f"ZIP symlink is forbidden: {name!r}")
        if member.flag_bits & 0x1:
            raise VerificationFailure(f"Encrypted ZIP member is forbidden: {name!r}")
        if member.is_dir():
            raise VerificationFailure(f"Explicit ZIP directory member is forbidden: {name!r}")
        total += member.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise VerificationFailure("ZIP uncompressed size exceeds its bound.")
        files.append(member)
    bad_crc = archive.testzip()
    if bad_crc is not None:
        raise VerificationFailure(f"ZIP CRC failed for {bad_crc!r}.")
    return files


def _extract_checked(zip_path: Path, destination: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as archive:
        members = _safe_members(archive)
        archive.extractall(destination, members=members)
    return sorted(member.filename for member in members)


def _parse_sums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            raise VerificationFailure(f"Malformed SHA256SUMS line: {line!r}")
        name = PurePosixPath(match.group(2)).as_posix()
        if name in result:
            raise VerificationFailure(f"Duplicate SHA256SUMS path: {name!r}")
        result[name] = match.group(1)
    return result


def _verify_sums(root: Path) -> dict[str, str]:
    sums = _parse_sums(root / "SHA256SUMS")
    actual_files = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    if set(sums) != set(actual_files):
        difference = sorted(set(sums) ^ set(actual_files))
        raise VerificationFailure(f"SHA256SUMS inventory mismatch: {difference}")
    for name, expected in sums.items():
        actual = _sha256(actual_files[name])
        if actual != expected:
            raise VerificationFailure(f"SHA-256 mismatch for {name}: {actual}")
    return sums


def _verify_runtime(root: Path) -> dict[str, Any]:
    sums = _verify_sums(root)
    if set(sums) != EXPECTED_RUNTIME_FILES:
        missing = sorted(EXPECTED_RUNTIME_FILES - set(sums))
        extra = sorted(set(sums) - EXPECTED_RUNTIME_FILES)
        raise VerificationFailure(
            f"The runtime payload inventory is not exact: missing={missing}, extra={extra}."
        )
    manifest = _load_json(root / "BUILD-MANIFEST.json")
    smoke = _load_json(root / "CAPABILITY-SMOKE.json")
    if manifest.get("schema") != "groove-serpent.windows-media-runtime-manifest/1":
        raise VerificationFailure("Unexpected runtime-manifest schema.")
    if smoke.get("schema") != "groove-serpent.windows-media-capability-smoke/1":
        raise VerificationFailure("Unexpected capability-smoke schema.")
    if smoke.get("result") != "passed":
        raise VerificationFailure("Embedded capability smoke did not pass.")
    manifest_files = manifest.get("runtime_files")
    if not isinstance(manifest_files, list):
        raise VerificationFailure("Manifest runtime_files is not an array.")
    manifest_inventory: dict[str, str] = {}
    for item in manifest_files:
        if not isinstance(item, dict):
            raise VerificationFailure("Manifest runtime file is not an object.")
        name = item.get("path")
        digest = item.get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            raise VerificationFailure("Manifest runtime-file identity is incomplete.")
        if name in manifest_inventory:
            raise VerificationFailure(f"Duplicate manifest runtime path: {name!r}")
        manifest_inventory[name] = digest
    expected_manifest_files = set(sums) - {"BUILD-MANIFEST.json"}
    if set(manifest_inventory) != expected_manifest_files:
        raise VerificationFailure("Manifest file inventory is not exact.")
    for name, digest in manifest_inventory.items():
        if sums[name] != digest:
            raise VerificationFailure(f"Manifest digest disagrees for {name}.")
    imports = manifest.get("pe_imports")
    if not isinstance(imports, dict) or set(imports) != {
        "avcodec-62.dll",
        "avdevice-62.dll",
        "avfilter-11.dll",
        "avformat-62.dll",
        "avutil-60.dll",
        "ffmpeg.exe",
        "ffprobe.exe",
        "libchromaprint.dll",
        "libsoxr.dll",
        "swresample-6.dll",
    }:
        raise VerificationFailure("PE import inventory is incomplete.")
    return {
        "capability_smoke_sha256": sums["CAPABILITY-SMOKE.json"],
        "manifest_sha256": sums["BUILD-MANIFEST.json"],
        "payload_files": len(sums),
    }


def _verify_source(root: Path) -> dict[str, Any]:
    sums = _verify_sums(root)
    expected_inventory = set(EXPECTED_INPUTS) | REQUIRED_RECIPE
    if set(sums) != expected_inventory:
        missing = sorted(expected_inventory - set(sums))
        extra = sorted(set(sums) - expected_inventory)
        raise VerificationFailure(
            f"The corresponding-source inventory is not exact: missing={missing}, extra={extra}."
        )
    for name, expected in EXPECTED_INPUTS.items():
        if sums.get(name) != expected:
            raise VerificationFailure(f"Pinned source input mismatch: {name}")
    try:
        _recipe_authority(root / "recipe", exact_inventory=True)
    except PublicationFailure as exc:
        raise VerificationFailure("Packaged build recipe authority is not exact.") from exc
    return {"payload_files": len(sums), "pinned_inputs": len(EXPECTED_INPUTS)}


def _verified_gpg_provider() -> Path:
    try:
        metadata = GPG_PROVIDER.lstat()
    except OSError as exc:
        raise VerificationFailure(f"Trusted GPG provider is unavailable: {GPG_PROVIDER}.") from exc
    unsafe_mode = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or unsafe_mode
        or not metadata.st_mode & stat.S_IXUSR
    ):
        raise VerificationFailure(
            "Trusted GPG provider must be a root-owned, root-group, "
            f"non-writable executable regular file: {GPG_PROVIDER}."
        )
    try:
        resolved = GPG_PROVIDER.resolve(strict=True)
    except OSError as exc:
        raise VerificationFailure(
            f"Could not resolve trusted GPG provider: {GPG_PROVIDER}."
        ) from exc
    if resolved != GPG_PROVIDER:
        raise VerificationFailure(f"Trusted GPG provider is not the exact file {GPG_PROVIDER}.")
    return GPG_PROVIDER


def _gpg_validsig(
    root: Path,
    *,
    archive: str,
    signature: str,
    key: str,
    fingerprint: str,
) -> None:
    gpg = _verified_gpg_provider().as_posix()
    with tempfile.TemporaryDirectory(prefix="gs-media-gpg-") as home_value:
        home = Path(home_value)
        if os.name != "nt":
            home.chmod(0o700)
        gpg_environment = {
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }
        imported = subprocess.run(
            [gpg, "--batch", "--homedir", str(home), "--import", str(root / key)],
            check=False,
            capture_output=True,
            env=gpg_environment,
        )
        if imported.returncode != 0:
            raise VerificationFailure(f"Could not import signing key for {archive}.")
        listed = subprocess.run(
            [gpg, "--batch", "--homedir", str(home), "--with-colons", "--fingerprint"],
            check=False,
            capture_output=True,
            env=gpg_environment,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        fingerprints = [
            line.split(":")[9] for line in listed.stdout.splitlines() if line.startswith("fpr:")
        ]
        if listed.returncode != 0 or not fingerprints or fingerprints[0] != fingerprint:
            raise VerificationFailure(f"Signing-key fingerprint mismatch for {archive}.")
        checked = subprocess.run(
            [
                gpg,
                "--batch",
                "--homedir",
                str(home),
                "--status-fd",
                "1",
                "--verify",
                str(root / signature),
                str(root / archive),
            ],
            check=False,
            capture_output=True,
            env=gpg_environment,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        expected = f"[GNUPG:] VALIDSIG {fingerprint} "
        if checked.returncode != 0 or expected not in checked.stdout:
            raise VerificationFailure(f"Detached signature failed for {archive}.")


def _verify_signatures(source_root: Path) -> None:
    _gpg_validsig(
        source_root,
        archive="inputs/ffmpeg-8.1.2.tar.xz",
        signature="inputs/ffmpeg-8.1.2.tar.xz.asc",
        key="recipe/keys/ffmpeg-release-signing-key.asc",
        fingerprint="FCF986EA15E6E293A5644F10B4322F04D67658D8",
    )
    _gpg_validsig(
        source_root,
        archive="inputs/zlib-1.3.2.tar.xz",
        signature="inputs/zlib-1.3.2.tar.xz.asc",
        key="recipe/keys/zlib-mark-adler.asc",
        fingerprint="5ED46A6721D365587791E2AA783FCD8E58BCAFBA",
    )


def _load_smoke_module(script: Path) -> Any:
    spec = importlib.util.spec_from_file_location("gs_windows_media_smoke", script)
    if spec is None or spec.loader is None:
        raise VerificationFailure("Could not load the capability-smoke implementation.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _execute_smoke(runtime_root: Path, script: Path, parent: Path) -> str:
    module = _load_smoke_module(script)
    work = parent / "groove-serpent-windows-media-smoke-verify"
    if os.name != "nt":
        for executable in (runtime_root / "ffmpeg.exe", runtime_root / "ffprobe.exe"):
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    try:
        report = module.run_smoke(runtime_root, work)
    except module.SmokeFailure as exc:
        raise VerificationFailure(f"Fresh capability smoke failed: {exc}") from exc
    encoded = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    embedded = (runtime_root / "CAPABILITY-SMOKE.json").read_bytes()
    if encoded != embedded:
        raise VerificationFailure("Fresh capability smoke differs from the embedded proof.")
    return hashlib.sha256(encoded).hexdigest()


def _archive_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _archive_path_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return _archive_identity(metadata)[:-1]


def _bind_archive_for_validation(
    source: Path,
    destination: Path,
    expected: str | None,
    label: str,
) -> str:
    """Hash and copy one stable descriptor, then validate only the private copy."""

    source = _absolute_path(source)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    try:
        source_descriptor = os.open(source, flags)
    except OSError as exc:
        raise VerificationFailure(f"Could not bind {label.lower()} archive: {source}") from exc
    destination_descriptor = -1
    try:
        before = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _is_reparse_point(before)
            or before.st_size > MAX_ARCHIVE_BYTES
        ):
            raise VerificationFailure(
                f"{label} archive must be a plain regular file no larger than "
                f"{MAX_ARCHIVE_BYTES} bytes."
            )
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0)),
            0o600,
        )
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_ARCHIVE_BYTES:
                raise VerificationFailure(
                    f"{label} archive exceeds the {MAX_ARCHIVE_BYTES}-byte bound."
                )
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "archive validation copy made no progress")
                view = view[written:]
        if os.name != "nt":
            fchmod: Any = getattr(os, "fchmod")
            fchmod(destination_descriptor, 0o600)
        after = os.fstat(source_descriptor)
        copied_metadata = os.fstat(destination_descriptor)
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        os.close(source_descriptor)
    try:
        path_metadata = source.lstat()
    except OSError as exc:
        raise VerificationFailure(
            f"{label} archive changed while it was bound for validation."
        ) from exc
    if (
        _archive_identity(before) != _archive_identity(after)
        # Windows can report sub-microsecond ctime rounding differently through
        # an open descriptor and a pathname for the same inode. The descriptor
        # ctime remains part of the before/after mutation check; path binding
        # uses the other exact identity and content metadata fields.
        or _archive_path_identity(before) != _archive_path_identity(path_metadata)
        or not stat.S_ISREG(copied_metadata.st_mode)
        or copied_metadata.st_size != copied
        or copied != before.st_size
    ):
        raise VerificationFailure(f"{label} archive changed while it was bound for validation.")
    actual = digest.hexdigest()
    if expected is not None and actual.casefold() != expected.casefold():
        raise VerificationFailure(f"{label} archive SHA-256 mismatch: {actual}")
    return actual


def main() -> int:
    if sys.argv[1:2] == ["--create-recipe-snapshot"]:
        return _create_recipe_snapshot_cli(sys.argv[2:])
    if sys.argv[1:2] == ["--verify-recipe-snapshot"]:
        return _verify_recipe_snapshot_cli(sys.argv[2:])
    if sys.argv[1:2] == ["--create-publication-stage"]:
        return _create_publication_stage_cli(sys.argv[2:])
    if sys.argv[1:2] == ["--publish-directory-no-replace"]:
        return _publish_directory_cli(sys.argv[2:])
    if sys.argv[1:2] == ["--verify-publication-directory"]:
        return _verify_publication_directory_cli(sys.argv[2:])
    if sys.argv[1:2] == ["--verify-build-layout"]:
        if len(sys.argv) != 4:
            print(
                "build-layout verification failed: expected DIST_DIR WORK_ROOT.",
                file=sys.stderr,
            )
            return 2
        try:
            _assert_build_paths_disjoint(Path(sys.argv[2]), Path(sys.argv[3]))
        except PublicationFailure as exc:
            print(f"build-layout verification failed: {exc}", file=sys.stderr)
            return 1
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-zip", type=Path, required=True)
    parser.add_argument("--source-zip", type=Path, required=True)
    parser.add_argument("--runtime-sha256")
    parser.add_argument("--source-sha256")
    parser.add_argument("--verify-signatures", action="store_true")
    parser.add_argument("--execute-smoke", action="store_true")
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix="gs-media-verify-") as temp_value:
            temp = Path(temp_value)
            bound = temp / "bound-inputs"
            runtime_root = temp / "runtime"
            source_root = temp / "source"
            bound.mkdir(mode=0o700)
            runtime_root.mkdir()
            source_root.mkdir()
            bound_runtime = bound / RUNTIME_ARCHIVE_NAME
            bound_source = bound / SOURCE_ARCHIVE_NAME
            runtime_hash = _bind_archive_for_validation(
                args.runtime_zip,
                bound_runtime,
                args.runtime_sha256,
                "Runtime",
            )
            source_hash = _bind_archive_for_validation(
                args.source_zip,
                bound_source,
                args.source_sha256,
                "Source",
            )
            _extract_checked(bound_runtime, runtime_root)
            _extract_checked(bound_source, source_root)
            runtime_result = _verify_runtime(runtime_root)
            source_result = _verify_source(source_root)
            if args.verify_signatures:
                _verify_signatures(source_root)
            fresh_smoke = None
            if args.execute_smoke:
                script = Path(__file__).resolve().with_name("capability_smoke.py")
                fresh_smoke = _execute_smoke(runtime_root, script, temp)
        result = {
            "schema": "groove-serpent.windows-media-artifact-verification/1",
            "result": "passed",
            "anchor_semantics": (
                "externally anchored"
                if args.runtime_sha256 and args.source_sha256
                else "self-consistency only; supply both expected hashes for an anchor"
            ),
            "runtime_archive": {"sha256": runtime_hash, **runtime_result},
            "source_archive": {"sha256": source_hash, **source_result},
            "detached_signatures_reverified": args.verify_signatures,
            "fresh_capability_smoke_sha256": fresh_smoke,
        }
        print(json.dumps(result, sort_keys=True, indent=2))
    except (OSError, VerificationFailure, ValueError, zipfile.BadZipFile) as exc:
        print(f"artifact verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
