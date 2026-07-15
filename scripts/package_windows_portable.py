from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, NoReturn, Sequence, TypedDict, cast

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import verify_windows_portable as portable_verifier  # noqa: E402
from scripts._release_fs import (  # noqa: E402
    capture_descriptor_identity,
    capture_identity,
    ensure_plain_directory_path,
    rename_no_replace,
    require_stable_creation_identity,
    unlink_owned_file_candidates,
    zip_layout_is_exact,
)


PACKAGE_SCHEMA = "groove-serpent.windows-portable-package/2"
ARCHIVE_PREFIX_RE = re.compile(r"Groove-Serpent-([0-9][0-9A-Za-z._+-]{0,127})-windows-x64\Z")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ZIP_MODE = stat.S_IFREG | 0o644
CHUNK_BYTES = 1024 * 1024
REPARSE_POINT_ATTRIBUTE = 0x400
MAX_ARCHIVE_OVERHEAD_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_BYTES = (
    portable_verifier.MAX_TOTAL_BYTES
    + portable_verifier.MAX_MANIFEST_BYTES
    + MAX_ARCHIVE_OVERHEAD_BYTES
)


class PackageError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ManifestMember(TypedDict):
    path: str
    sha256: str
    size: int


class PackageResult(TypedDict):
    schema: str
    ok: bool
    authenticity: str
    archive_filename: str
    archive_sha256: str
    archive_size: int
    manifest_sha256: str
    app_version: str
    platform: str
    member_count: int
    code_signing: str
    corresponding_source_path: str
    corresponding_source_sha256: str
    corresponding_source_size: int


class ArchiveVerification(TypedDict):
    archive_sha256: str
    archive_size: int
    manifest_sha256: str
    app_version: str
    prefix: str
    member_count: int


class StrictArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise PackageError("usage", message)


def _fail(code: str, message: str) -> NoReturn:
    raise PackageError(code, message)


def _is_reparse(result: os.stat_result) -> bool:
    attributes = getattr(result, "st_file_attributes", 0)
    return bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _regular_handle(path: Path, context: str, maximum: int) -> BinaryIO:
    try:
        before = path.lstat()
    except OSError as exc:
        raise PackageError("read", f"{context} cannot be inspected.") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        _fail("unsafe_file", f"{context} is not a single-link regular file.")
    if before.st_size < 0 or before.st_size > maximum:
        _fail("size", f"{context} exceeds its bounded size.")
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise PackageError("read", f"{context} cannot be opened.") from exc
    try:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            _fail("race", f"{context} changed identity while it was opened.")
        return handle
    except BaseException:
        handle.close()
        raise


def _read_regular(path: Path, context: str, maximum: int) -> bytes:
    with _regular_handle(path, context, maximum) as opened:
        payload = opened.read(maximum + 1)
        if len(payload) > maximum or opened.read(1):
            _fail("size", f"{context} exceeded its bounded size while reading.")
        return payload


def _hash_regular(path: Path, context: str, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with _regular_handle(path, context, maximum) as opened:
        while True:
            chunk = opened.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                _fail("size", f"{context} exceeded its bounded size while hashing.")
            digest.update(chunk)
    return digest.hexdigest(), total


def _manifest_details(
    payload: bytes,
) -> tuple[str, list[ManifestMember], int]:
    decoded = portable_verifier._parse_manifest(payload)
    (
        _app_name,
        app_version,
        members,
        _by_path,
        _inputs,
        _singleton_inputs,
    ) = portable_verifier._strict_manifest(decoded)
    return app_version, members, len(members)


def _zip_info(name: str, size: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.comment = b""
    info.extra = b""
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.external_attr = ZIP_MODE << 16
    info.internal_attr = 0
    info.file_size = size
    return info


def _write_member(
    archive: zipfile.ZipFile,
    *,
    archive_name: str,
    source: Path,
    expected_sha256: str,
    expected_size: int,
    maximum: int,
) -> None:
    digest = hashlib.sha256()
    total = 0
    with _regular_handle(source, "portable payload member", maximum) as opened:
        with archive.open(_zip_info(archive_name, expected_size), "w") as output:
            while True:
                chunk = opened.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    _fail("size", "Portable payload member exceeded its bounded size.")
                digest.update(chunk)
                output.write(chunk)
    if total != expected_size:
        _fail("source_drift", "Portable payload member size changed during packaging.")
    if digest.hexdigest() != expected_sha256:
        _fail("source_drift", "Portable payload member hash changed during packaging.")


def _write_archive(
    raw: BinaryIO,
    *,
    source: Path,
    prefix: str,
    manifest_payload: bytes,
    manifest_sha256: str,
    members: list[ManifestMember],
) -> None:
    with zipfile.ZipFile(
        raw,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
        strict_timestamps=True,
    ) as archive:
        archive.comment = b""
        manifest_name = f"{prefix}/{portable_verifier.MANIFEST_NAME}"
        info = _zip_info(manifest_name, len(manifest_payload))
        with archive.open(info, "w") as output:
            output.write(manifest_payload)
        if hashlib.sha256(manifest_payload).hexdigest() != manifest_sha256:
            _fail("manifest_hash", "Portable manifest changed before archive creation.")
        for member in members:
            relative = member["path"]
            _write_member(
                archive,
                archive_name=f"{prefix}/{relative}",
                source=source.joinpath(*PurePosixPath(relative).parts),
                expected_sha256=member["sha256"],
                expected_size=member["size"],
                maximum=portable_verifier.MAX_MEMBER_BYTES,
            )


def _safe_archive_name(name: str, expected_prefix: str) -> str:
    if "\\" in name or name.startswith("/") or "\x00" in name:
        _fail("unsafe_archive", "Archive contains a non-canonical member path.")
    path = PurePosixPath(name)
    if path.as_posix() != name:
        _fail("unsafe_archive", "Archive contains a non-canonical member path.")
    if len(path.parts) < 2 or path.parts[0] != expected_prefix:
        _fail("unsafe_archive", "Archive member is outside its exact versioned directory.")
    relative = PurePosixPath(*path.parts[1:]).as_posix()
    try:
        portable_verifier._safe_relative(relative, "archive member")
    except portable_verifier.VerificationError as exc:
        raise PackageError("unsafe_archive", exc.message) from exc
    return relative


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    expected_sha256: str,
    expected_size: int,
    maximum: int,
) -> None:
    if info.file_size != expected_size or info.compress_size != expected_size:
        _fail("archive_size", "Archive member size does not match its manifest record.")
    if info.file_size < 0 or info.file_size > maximum:
        _fail("archive_size", "Archive member exceeds its bounded size.")
    digest = hashlib.sha256()
    total = 0
    try:
        with archive.open(info, "r") as opened:
            while True:
                chunk = opened.read(CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    _fail("archive_size", "Archive member exceeded its bounded size.")
                digest.update(chunk)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise PackageError("archive_read", "Archive member cannot be read safely.") from exc
    if total != expected_size or digest.hexdigest() != expected_sha256:
        _fail("archive_hash", "Reopened archive member does not match the manifest.")


def _assert_zip_metadata(info: zipfile.ZipInfo) -> None:
    if (
        info.is_dir()
        or info.date_time != ZIP_TIMESTAMP
        or info.compress_type != zipfile.ZIP_STORED
        or info.comment != b""
        or info.extra != b""
        or info.create_system != 3
        or info.create_version != 20
        or info.extract_version != 20
        or info.external_attr != ZIP_MODE << 16
        or info.internal_attr != 0
        or bool(info.flag_bits & 0x1)
    ):
        _fail("archive_metadata", "Archive member metadata is not the deterministic profile.")


def verify_zip_archive(
    archive_path: Path,
    *,
    expected_manifest_sha256: str,
) -> ArchiveVerification:
    expected = expected_manifest_sha256.casefold()
    if portable_verifier.SHA256_RE.fullmatch(expected) is None:
        _fail("usage", "Expected manifest SHA-256 is invalid.")
    digest_builder = hashlib.sha256()
    archive_size = 0
    app_version = ""
    prefix = ""
    member_count = 0
    try:
        with _regular_handle(
            archive_path,
            "portable ZIP archive",
            MAX_ARCHIVE_BYTES,
        ) as opened:
            identity = capture_descriptor_identity(opened.fileno(), bind_file=True)
            while True:
                chunk = opened.read(CHUNK_BYTES)
                if not chunk:
                    break
                archive_size += len(chunk)
                if archive_size > MAX_ARCHIVE_BYTES:
                    _fail("size", "Portable ZIP exceeded its bound while hashing.")
                digest_builder.update(chunk)
            opened.seek(0)
            with zipfile.ZipFile(opened, mode="r", allowZip64=True) as archive:
                if archive.comment != b"":
                    _fail("archive_metadata", "Archive comment is not empty.")
                infos = archive.infolist()
                if not infos or len(infos) > portable_verifier.MAX_MEMBERS + 1:
                    _fail("archive_inventory", "Archive member count is outside its bound.")
                if not zip_layout_is_exact(opened, archive_size, infos):
                    _fail("archive_metadata", "Archive container layout is not exact.")
                first = infos[0]
                first_path = PurePosixPath(first.filename)
                if (
                    len(first_path.parts) != 2
                    or first_path.parts[1] != portable_verifier.MANIFEST_NAME
                ):
                    _fail("archive_inventory", "Archive manifest is not the first exact member.")
                prefix = first_path.parts[0]
                match = ARCHIVE_PREFIX_RE.fullmatch(prefix)
                if match is None:
                    _fail("archive_inventory", "Archive directory is not exactly versioned.")
                relative_names = [_safe_archive_name(info.filename, prefix) for info in infos]
                if len(set(relative_names)) != len(relative_names):
                    _fail("archive_inventory", "Archive contains duplicate member names.")
                portable_keys = [name.casefold() for name in relative_names]
                if len(set(portable_keys)) != len(portable_keys):
                    _fail("archive_inventory", "Archive contains portable-equivalent names.")
                for info in infos:
                    _assert_zip_metadata(info)
                if (
                    first.file_size > portable_verifier.MAX_MANIFEST_BYTES
                    or first.compress_size != first.file_size
                ):
                    _fail("archive_size", "Archive manifest exceeds its bounded size.")
                try:
                    manifest_payload = archive.read(first)
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise PackageError(
                        "archive_read",
                        "Archive manifest cannot be read.",
                    ) from exc
                manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
                if manifest_sha256 != expected:
                    _fail("manifest_hash", "Archive manifest does not match the trust anchor.")
                app_version, members, member_count = _manifest_details(manifest_payload)
                if match.group(1) != app_version:
                    _fail(
                        "archive_inventory",
                        "Archive directory version does not match manifest.",
                    )
                expected_names = [portable_verifier.MANIFEST_NAME]
                expected_names.extend(member["path"] for member in members)
                if relative_names != expected_names:
                    _fail("archive_inventory", "Archive has missing, extra, or reordered members.")
                if first.file_size != len(manifest_payload):
                    _fail("archive_size", "Archive manifest size changed while reading.")
                for info, member in zip(infos[1:], members, strict=True):
                    _read_zip_member(
                        archive,
                        info,
                        expected_sha256=member["sha256"],
                        expected_size=member["size"],
                        maximum=portable_verifier.MAX_MEMBER_BYTES,
                    )
            descriptor_after = os.fstat(opened.fileno())
            try:
                path_after = archive_path.lstat()
            except OSError as exc:
                raise PackageError(
                    "race",
                    "Portable ZIP path changed during verification.",
                ) from exc
            if (
                archive_size != int(descriptor_after.st_size)
                or not identity.matches_descriptor(opened.fileno(), descriptor_after)
                or not identity.matches_path(archive_path, path_after)
            ):
                _fail("race", "Portable ZIP changed during verification.")
    except PackageError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise PackageError("archive_read", "Portable ZIP archive cannot be reopened.") from exc
    return {
        "archive_sha256": digest_builder.hexdigest(),
        "archive_size": archive_size,
        "manifest_sha256": expected,
        "app_version": app_version,
        "prefix": prefix,
        "member_count": member_count + 1,
    }


def _validated_output_directory(path: Path, source: Path) -> Path:
    try:
        supplied = ensure_plain_directory_path(
            path,
            "Portable ZIP output directory",
            create=False,
        )
    except RuntimeError as exc:
        raise PackageError(
            "output",
            "Output directory must be an existing plain directory with plain ancestry.",
        ) from exc
    # Resolution is safe only after every supplied lexical component passed
    # the no-link/no-reparse audit above.
    resolved = supplied.resolve(strict=True)
    if resolved == source or source in resolved.parents:
        _fail("output", "Output directory must not be inside the portable directory.")
    return resolved


def _publish_new(staged: Path, output: Path) -> None:
    if os.path.lexists(output):
        _fail("exists", "Versioned portable ZIP already exists; replacement is refused.")
    try:
        rename_no_replace(staged, output)
    except FileExistsError as exc:
        raise PackageError(
            "exists",
            "Versioned portable ZIP already exists; replacement is refused.",
        ) from exc
    except OSError as exc:
        raise PackageError("publish", "Portable ZIP could not be published atomically.") from exc


def package_portable_directory(
    source: Path,
    output_directory: Path,
    *,
    expected_manifest_sha256: str,
) -> tuple[Path, PackageResult]:
    try:
        verified = portable_verifier.verify_portable_directory(
            source,
            expected_manifest_sha256=expected_manifest_sha256,
        )
    except portable_verifier.VerificationError as exc:
        raise PackageError(exc.code, exc.message) from exc
    resolved_source = source.expanduser().absolute().resolve(strict=True)
    output_root = _validated_output_directory(output_directory, resolved_source)
    try:
        require_stable_creation_identity(
            output_root,
            "Portable ZIP output",
        )
    except RuntimeError as exc:
        raise PackageError("output", str(exc)) from exc
    app_version = verified["app_version"]
    prefix = f"Groove-Serpent-{app_version}-windows-x64"
    archive_filename = f"{prefix}.zip"
    output_path = output_root / archive_filename
    if output_path.exists():
        _fail("exists", "Versioned portable ZIP already exists; replacement is refused.")
    manifest_path = resolved_source / portable_verifier.MANIFEST_NAME
    manifest_payload = _read_regular(
        manifest_path,
        "portable manifest",
        portable_verifier.MAX_MANIFEST_BYTES,
    )
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
    if manifest_sha256 != verified["manifest_sha256"]:
        _fail("source_drift", "Portable manifest changed after verification.")
    parsed_version, members, _member_count = _manifest_details(manifest_payload)
    if parsed_version != app_version:
        _fail("source_drift", "Portable manifest version changed after verification.")
    source_member = next(
        (
            member
            for member in members
            if member["path"] == portable_verifier.WINDOWS_MEDIA_SOURCE_PATH
        ),
        None,
    )
    if source_member is None:
        _fail("source_missing", "Portable manifest omits corresponding source.")
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{archive_filename}.",
        suffix=".tmp",
        dir=output_root,
    )
    staged = Path(staged_name)
    staged_identity = capture_descriptor_identity(descriptor)
    completed = False
    try:
        raw = os.fdopen(descriptor, "w+b")
        # The file object now exclusively owns the descriptor.  Clearing the
        # numeric slot prevents finally from closing a different descriptor if
        # another thread reuses the number after the file object closes it.
        descriptor = -1
        with raw:
            _write_archive(
                raw,
                source=resolved_source,
                prefix=prefix,
                manifest_payload=manifest_payload,
                manifest_sha256=manifest_sha256,
                members=members,
            )
            raw.flush()
            os.fsync(raw.fileno())
        staged_sha256, staged_size = _hash_regular(
            staged,
            "staging portable ZIP",
            MAX_ARCHIVE_BYTES,
        )
        staged_details = staged.lstat()
        if (
            not staged_identity.matches_path(staged, staged_details)
            or int(staged_details.st_size) != staged_size
        ):
            _fail("race", "Staging portable ZIP changed identity after writing.")
        staged_identity = capture_identity(
            staged,
            bind_file=True,
            content_sha256=staged_sha256,
        )
        staged_check = verify_zip_archive(
            staged,
            expected_manifest_sha256=manifest_sha256,
        )
        if (
            staged_check["archive_sha256"] != staged_sha256
            or staged_check["archive_size"] != staged_size
            or not staged_identity.matches_path(staged)
        ):
            _fail("race", "Staging portable ZIP changed during verification.")
        try:
            portable_verifier.verify_portable_directory(
                resolved_source,
                expected_manifest_sha256=manifest_sha256,
            )
        except portable_verifier.VerificationError as exc:
            raise PackageError("source_drift", exc.message) from exc
        _publish_new(staged, output_path)
        final_check = verify_zip_archive(
            output_path,
            expected_manifest_sha256=manifest_sha256,
        )
        if final_check != staged_check:
            _fail(
                "publish_drift",
                "Published ZIP bytes differ from the verified staging ZIP.",
            )
        completed = True
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not completed:
            if not unlink_owned_file_candidates((staged, output_path), staged_identity):
                message = "Portable ZIP cleanup lost ownership; unknown paths were preserved."
                active = sys.exception()
                if active is None:
                    raise PackageError("cleanup", message)
                active.add_note(message)
    result: PackageResult = {
        "schema": PACKAGE_SCHEMA,
        "ok": True,
        "authenticity": "anchored-to-expected-manifest-sha256",
        "archive_filename": archive_filename,
        "archive_sha256": final_check["archive_sha256"],
        "archive_size": final_check["archive_size"],
        "manifest_sha256": manifest_sha256,
        "app_version": app_version,
        "platform": portable_verifier.PLATFORM,
        "member_count": final_check["member_count"],
        "code_signing": "unsigned",
        "corresponding_source_path": portable_verifier.WINDOWS_MEDIA_SOURCE_PATH,
        "corresponding_source_sha256": source_member["sha256"],
        "corresponding_source_size": source_member["size"],
    }
    return output_path, result


def _parser() -> argparse.ArgumentParser:
    parser = StrictArgumentParser(add_help=False)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--help", action="store_true")
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.help:
            _emit(
                {
                    "schema": PACKAGE_SCHEMA,
                    "ok": True,
                    "usage": (
                        "package_windows_portable.py --directory DIR "
                        "--output-directory DIR --expected-manifest-sha256 SHA256"
                    ),
                }
            )
            return 0
        if (
            args.directory is None
            or args.output_directory is None
            or args.expected_manifest_sha256 is None
        ):
            _fail(
                "usage",
                "--directory, --output-directory, and --expected-manifest-sha256 are required.",
            )
        _path, result = package_portable_directory(
            args.directory,
            args.output_directory,
            expected_manifest_sha256=args.expected_manifest_sha256,
        )
        _emit(cast(dict[str, object], result))
        return 0
    except PackageError as exc:
        _emit(
            {
                "schema": PACKAGE_SCHEMA,
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            }
        )
        return 2 if exc.code == "usage" else 1
    except Exception:
        _emit(
            {
                "schema": PACKAGE_SCHEMA,
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "Portable ZIP packaging failed unexpectedly.",
                },
            }
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
