from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Sequence, TypedDict, cast


ROOT = Path(__file__).resolve().parent.parent
if not __package__:
    sys.path.insert(0, str(ROOT))

from scripts._release_fs import (  # noqa: E402
    PathIdentity,
    capture_descriptor_identity,
    capture_identity,
    ensure_plain_directory_path,
    inspect_plain_directory,
    inspect_single_link_file,
    read_single_link_file,
    remove_owned_tree,
    remove_owned_tree_candidates,
    rename_no_replace,
    require_stable_creation_identity,
    unlink_if_owned_file,
)


PORTABLE_SCHEMA = "groove-serpent.windows-portable-manifest/2"
PORTABLE_PLATFORM = "windows-x64"
DEFAULT_EPOCH = 315_532_800  # 1980-01-01T00:00:00Z, the ZIP epoch.
MAX_ARCHIVE_MEMBERS = 50_000
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 1024 * 1024
MAX_RELATIVE_PATH_LENGTH = 240
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
CHROMAPRINT_FINGERPRINT_RE = re.compile(rb"[A-Za-z0-9_-]{16,16384}={0,2}\Z")
WINDOWS_MEDIA_RUNTIME_SCHEMA = "groove-serpent.windows-media-runtime-manifest/1"
WINDOWS_MEDIA_SMOKE_SCHEMA = "groove-serpent.windows-media-capability-smoke/1"
WINDOWS_MEDIA_RUNTIME_FILENAME = "groove-serpent-windows-media-8.1.2-x86_64.zip"
WINDOWS_MEDIA_SOURCE_FILENAME = "groove-serpent-windows-media-8.1.2-corresponding-source.zip"
WINDOWS_MEDIA_SOURCE_DESTINATION = f"CORRESPONDING-SOURCE/{WINDOWS_MEDIA_SOURCE_FILENAME}"
WINDOWS_MEDIA_BINARIES = frozenset(
    {
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
    }
)
WINDOWS_MEDIA_SOURCE_RECIPE = frozenset(
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
DIST_NORMALIZER_RE = re.compile(r"[-_.]+")
WINDOWS_FORBIDDEN = frozenset('<>:"\\|?*')
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
FORBIDDEN_PAYLOAD_SUFFIXES = frozenset(
    {
        ".aif",
        ".aiff",
        ".flac",
        ".m4a",
        ".mp3",
        ".p12",
        ".pem",
        ".pfx",
        ".wav",
    }
)
FORBIDDEN_PRIVATE_PATTERNS = (
    re.compile(rb"[A-Za-z]:\\HomelabForge(?:\\|/)", re.IGNORECASE),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"sk-" rb"proj-[A-Za-z0-9_-]+"),
)
REPARSE_POINT_ATTRIBUTE = 0x400


class PortableBuildError(RuntimeError):
    """A fail-closed portable-delivery validation or build error."""


class InventoryItem(TypedDict):
    path: str
    sha256: str
    size: int


class PayloadManifest(TypedDict):
    member_count: int
    total_bytes: int
    members: list[InventoryItem]


class PortableManifest(TypedDict):
    schema: str
    payload: PayloadManifest


@dataclass(frozen=True, slots=True)
class ExactInput:
    path: Path
    sha256: str
    label: str


@dataclass(frozen=True, slots=True)
class WheelInput:
    exact: ExactInput
    distribution: str
    version: str


@dataclass(frozen=True, slots=True)
class ResourceInput:
    exact: ExactInput
    destination: PurePosixPath


@dataclass(frozen=True, slots=True)
class PortableInputs:
    app_wheel: WheelInput
    dependency_wheels: tuple[WheelInput, ...]
    python_embed: ExactInput
    python_version: str
    windows_media_runtime: ExactInput
    windows_media_corresponding_source: ExactInput
    groove_license: ExactInput
    third_party_notices: ExactInput
    portable_verifier: ExactInput
    skill_files: tuple[ResourceInput, ...]


@dataclass(frozen=True, slots=True)
class WindowsMediaEvidence:
    ffmpeg_sha256: str
    ffprobe_sha256: str
    build_manifest_sha256: str
    capability_smoke_sha256: str
    profile: str
    ffmpeg_version: str
    source_input_count: int


def _normalized_distribution(value: str) -> str:
    return DIST_NORMALIZER_RE.sub("-", value).casefold()


def _validated_sha256(value: str, context: str) -> str:
    if SHA256_RE.fullmatch(value) is None:
        raise PortableBuildError(f"{context} must be an exact 64-character SHA-256.")
    return value.casefold()


def _portable_component(value: str, context: str) -> str:
    if not value or value in {".", ".."}:
        raise PortableBuildError(f"{context} contains an empty or traversal component.")
    if value != unicodedata.normalize("NFC", value):
        raise PortableBuildError(f"{context} is not canonical NFC text: {value!r}")
    if value[-1] in {" ", "."}:
        raise PortableBuildError(f"{context} has a trailing space or period: {value!r}")
    if any(ord(character) < 32 or character in WINDOWS_FORBIDDEN for character in value):
        raise PortableBuildError(f"{context} contains a Windows-unsafe character: {value!r}")
    if value.split(".", 1)[0].casefold() in WINDOWS_RESERVED:
        raise PortableBuildError(f"{context} uses a reserved Windows device name: {value!r}")
    if len(value.encode("utf-16-le")) // 2 > 255:
        raise PortableBuildError(f"{context} exceeds the Windows component limit: {value!r}")
    return value


def _safe_relative_path(value: str, context: str) -> PurePosixPath:
    if "\x00" in value or "\\" in value:
        raise PortableBuildError(f"{context} is not a canonical forward-slash path.")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or path.as_posix() != value:
        raise PortableBuildError(f"{context} must be a non-empty relative path: {value!r}")
    for component in path.parts:
        _portable_component(component, context)
    canonical = path.as_posix()
    if len(canonical) > MAX_RELATIVE_PATH_LENGTH:
        raise PortableBuildError(f"{context} exceeds {MAX_RELATIVE_PATH_LENGTH} characters.")
    return path


def _portable_key(path: PurePosixPath) -> str:
    return unicodedata.normalize("NFC", path.as_posix()).casefold()


def _archive_member_relative(
    member: zipfile.ZipInfo,
    context: str,
) -> PurePosixPath:
    supplied = member.filename
    value = supplied[:-1] if member.is_dir() and supplied.endswith("/") else supplied
    relative = _safe_relative_path(value, f"{context} member")
    expected = f"{relative.as_posix()}/" if member.is_dir() else relative.as_posix()
    if supplied != expected:
        raise PortableBuildError(f"{context} member is not an exact canonical path: {supplied!r}")
    return relative


def _private_patterns() -> tuple[re.Pattern[bytes], ...]:
    paths = {str(Path.home().resolve()), str(Path.cwd().resolve())}
    local = tuple(
        re.compile(re.escape(path.encode("utf-8")), re.IGNORECASE) for path in paths if path
    )
    return (*FORBIDDEN_PRIVATE_PATTERNS, *local)


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _regular_input_handle(exact: ExactInput) -> BinaryIO:
    source = exact.path.expanduser()
    try:
        before = source.lstat()
    except OSError as exc:
        raise PortableBuildError(f"Cannot inspect {exact.label}: {exc}") from exc
    if stat.S_ISLNK(before.st_mode) or _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise PortableBuildError(f"{exact.label} must be a regular non-link file: {source}")
    if before.st_size > MAX_INPUT_BYTES:
        raise PortableBuildError(f"{exact.label} exceeds the bounded input size: {source}")
    try:
        handle = source.open("rb")
    except OSError as exc:
        raise PortableBuildError(f"Cannot open {exact.label}: {exc}") from exc
    try:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise PortableBuildError(f"{exact.label} changed identity while it was opened.")
        return handle
    except BaseException:
        handle.close()
        raise


def _copy_exact_input(exact: ExactInput, destination: Path, epoch: int) -> None:
    expected = _validated_sha256(exact.sha256, f"{exact.label} SHA-256")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    output_identity: PathIdentity | None = None
    with _regular_input_handle(exact) as source, destination.open("xb") as output:
        output_identity = capture_descriptor_identity(output.fileno())
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_INPUT_BYTES:
                raise PortableBuildError(f"{exact.label} exceeded the bounded input size.")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    actual = digest.hexdigest()
    if actual != expected:
        try:
            output_details = destination.lstat()
        except OSError:
            output_details = None
        if (
            output_identity is not None
            and output_details is not None
            and output_identity.matches_path(destination, output_details)
        ):
            output_identity = capture_identity(
                destination,
                bind_file=True,
                content_sha256=actual,
            )
        removed = output_identity is not None and unlink_if_owned_file(
            destination,
            output_identity,
        )
        cleanup = "" if removed else " The unowned output was preserved."
        raise PortableBuildError(
            f"{exact.label} SHA-256 mismatch: expected {expected}, observed {actual}.{cleanup}"
        )
    os.utime(destination, (epoch, epoch))


def _snapshot_input(exact: ExactInput, input_directory: Path, epoch: int) -> Path:
    digest = _validated_sha256(exact.sha256, f"{exact.label} SHA-256")
    snapshot = input_directory / f"{digest}.input"
    if snapshot.exists():
        if _sha256_file(snapshot) != digest:
            raise PortableBuildError("Two explicit inputs reused a digest with different bytes.")
        return snapshot
    _copy_exact_input(exact, snapshot, epoch)
    return snapshot


def _archive_members(archive: zipfile.ZipFile, context: str) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if not members or len(members) > MAX_ARCHIVE_MEMBERS:
        raise PortableBuildError(f"{context} has an invalid member count: {len(members)}")
    total = 0
    portable_names: dict[str, str] = {}
    for member in members:
        if member.flag_bits & 0x1:
            raise PortableBuildError(f"{context} contains an encrypted member: {member.filename}")
        relative = _archive_member_relative(member, context)
        key = _portable_key(relative)
        previous = portable_names.get(key)
        if previous is not None:
            raise PortableBuildError(
                f"{context} has duplicate portable paths: {previous!r} and {member.filename!r}"
            )
        portable_names[key] = member.filename
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type and not member.is_dir() and file_type != stat.S_IFREG:
            raise PortableBuildError(f"{context} contains a non-regular member: {member.filename}")
        if member.file_size < 0 or member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise PortableBuildError(f"{context} member is too large: {member.filename}")
        total += member.file_size
        if total > MAX_ARCHIVE_EXPANDED_BYTES:
            raise PortableBuildError(f"{context} expands beyond the bounded size.")
        if member.compress_size == 0 and member.file_size:
            raise PortableBuildError(f"{context} has an invalid compressed size: {member.filename}")
        if member.compress_size and member.file_size > member.compress_size * 1_000:
            raise PortableBuildError(
                f"{context} has an excessive compression ratio: {member.filename}"
            )
    return members


def _read_zip_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    with archive.open(member, "r") as handle:
        payload = handle.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
    if len(payload) != member.file_size or len(payload) > MAX_ARCHIVE_MEMBER_BYTES:
        raise PortableBuildError(f"Archive member size changed while reading: {member.filename}")
    return payload


def _strict_json_object(payload: bytes, context: str) -> dict[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PortableBuildError(f"{context} repeats JSON key {key!r}.")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise PortableBuildError(f"{context} contains non-finite JSON value {value!r}.")

    try:
        decoded = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableBuildError(f"{context} is not strict UTF-8 JSON.") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise PortableBuildError(f"{context} must contain a JSON object.")
    return cast(dict[str, object], decoded)


def _parse_archive_sums(payload: bytes, context: str) -> dict[str, str]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PortableBuildError(f"{context} SHA256SUMS is not UTF-8 text.") from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            raise PortableBuildError(f"{context} has a malformed SHA256SUMS line.")
        relative = _safe_relative_path(match.group(2), f"{context} SHA256SUMS path")
        name = relative.as_posix()
        if name in result:
            raise PortableBuildError(f"{context} SHA256SUMS repeats {name!r}.")
        result[name] = match.group(1)
    if not result:
        raise PortableBuildError(f"{context} SHA256SUMS is empty.")
    return result


def _verify_zip_sums(
    archive: zipfile.ZipFile,
    members: Sequence[zipfile.ZipInfo],
    context: str,
) -> tuple[dict[str, str], dict[str, zipfile.ZipInfo]]:
    by_name = {member.filename: member for member in members if not member.is_dir()}
    sums_member = by_name.get("SHA256SUMS")
    if sums_member is None:
        raise PortableBuildError(f"{context} is missing SHA256SUMS.")
    sums = _parse_archive_sums(_read_zip_member(archive, sums_member), context)
    if set(sums) != set(by_name) - {"SHA256SUMS"}:
        raise PortableBuildError(f"{context} SHA256SUMS inventory is not exact.")
    for name, expected in sums.items():
        actual = hashlib.sha256(_read_zip_member(archive, by_name[name])).hexdigest()
        if actual != expected:
            raise PortableBuildError(f"{context} SHA-256 failed for {name!r}.")
    return sums, by_name


def _runtime_file_inventory(value: object) -> dict[str, tuple[str, int]]:
    if not isinstance(value, list):
        raise PortableBuildError("Windows media runtime_files is not an array.")
    inventory: dict[str, tuple[str, int]] = {}
    for item in value:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise PortableBuildError("Windows media runtime file is not an object.")
        path_value = item.get("path")
        digest_value = item.get("sha256")
        size_value = item.get("size_bytes")
        if not isinstance(path_value, str):
            raise PortableBuildError("Windows media runtime file path is missing.")
        path = _safe_relative_path(path_value, "Windows media runtime file").as_posix()
        if (
            not isinstance(digest_value, str)
            or SHA256_RE.fullmatch(digest_value) is None
            or type(size_value) is not int
            or size_value < 0
            or size_value > MAX_ARCHIVE_MEMBER_BYTES
        ):
            raise PortableBuildError("Windows media runtime file identity is incomplete.")
        if path in inventory:
            raise PortableBuildError(f"Windows media runtime repeats {path!r}.")
        inventory[path] = (digest_value, size_value)
    return inventory


def _source_input_inventory(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        raise PortableBuildError("Windows media source_inputs is not an array.")
    inventory: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise PortableBuildError("Windows media source input is not an object.")
        name = item.get("name")
        digest = item.get("sha256")
        if (
            not isinstance(name, str)
            or _portable_component(name, "Windows media source input") != name
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
        ):
            raise PortableBuildError("Windows media source input identity is incomplete.")
        if name in inventory:
            raise PortableBuildError(f"Windows media source input repeats {name!r}.")
        inventory[name] = digest
    if not inventory:
        raise PortableBuildError("Windows media source input inventory is empty.")
    return inventory


def _verify_windows_media_pair(
    runtime_snapshot: Path,
    source_snapshot: Path,
    destination: Path,
    epoch: int,
) -> tuple[WindowsMediaEvidence, bytes]:
    try:
        runtime_archive = zipfile.ZipFile(runtime_snapshot, "r")
        runtime_members = _archive_members(runtime_archive, "Windows media runtime")
        source_archive = zipfile.ZipFile(source_snapshot, "r")
        source_members = _archive_members(
            source_archive,
            "Windows media corresponding source",
        )
    except (OSError, zipfile.BadZipFile, PortableBuildError):
        try:
            runtime_archive.close()
        except UnboundLocalError:
            pass
        try:
            source_archive.close()
        except UnboundLocalError:
            pass
        raise
    try:
        runtime_sums, runtime_by_name = _verify_zip_sums(
            runtime_archive,
            runtime_members,
            "Windows media runtime",
        )
        manifest_member = runtime_by_name.get("BUILD-MANIFEST.json")
        smoke_member = runtime_by_name.get("CAPABILITY-SMOKE.json")
        if manifest_member is None or smoke_member is None:
            raise PortableBuildError("Windows media runtime proof files are incomplete.")
        manifest = _strict_json_object(
            _read_zip_member(runtime_archive, manifest_member),
            "Windows media BUILD-MANIFEST.json",
        )
        smoke_payload = _read_zip_member(runtime_archive, smoke_member)
        smoke = _strict_json_object(
            smoke_payload,
            "Windows media CAPABILITY-SMOKE.json",
        )
        if manifest.get("schema") != WINDOWS_MEDIA_RUNTIME_SCHEMA:
            raise PortableBuildError("Windows media runtime manifest schema is unsupported.")
        if smoke.get("schema") != WINDOWS_MEDIA_SMOKE_SCHEMA or smoke.get("result") != "passed":
            raise PortableBuildError("Windows media embedded capability smoke did not pass.")
        artifact = manifest.get("artifact")
        if not isinstance(artifact, dict) or artifact != {
            "architecture": "x86_64-w64-mingw32",
            "ffmpeg_version": "8.1.2",
            "profile": "groove-serpent-minimal-audio-shared-v1",
            "source_date_epoch": 1_781_664_539,
        }:
            raise PortableBuildError("Windows media runtime artifact identity is unsupported.")
        license_evidence = manifest.get("license_evidence")
        if (
            not isinstance(license_evidence, dict)
            or license_evidence.get("ffmpeg_gpl_flag") is not False
            or license_evidence.get("ffmpeg_nonfree_flag") is not False
            or license_evidence.get("ffmpeg_version3_flag") is not False
            or license_evidence.get("linking")
            != "shared FFmpeg, Chromaprint, and libsoxr libraries"
        ):
            raise PortableBuildError("Windows media runtime license evidence is unsupported.")
        binaries = {
            name for name in runtime_sums if Path(name).suffix.casefold() in {".dll", ".exe"}
        }
        if binaries != WINDOWS_MEDIA_BINARIES:
            raise PortableBuildError("Windows media binary inventory is not exact.")
        manifest_inventory = _runtime_file_inventory(manifest.get("runtime_files"))
        expected_manifest_inventory = set(runtime_sums) - {"BUILD-MANIFEST.json"}
        if set(manifest_inventory) != expected_manifest_inventory:
            raise PortableBuildError("Windows media manifest file inventory is not exact.")
        for name, (digest, size) in manifest_inventory.items():
            if runtime_sums[name] != digest or runtime_by_name[name].file_size != size:
                raise PortableBuildError(f"Windows media manifest identity disagrees for {name!r}.")
        if manifest.get("capability_smoke_sha256") != runtime_sums["CAPABILITY-SMOKE.json"]:
            raise PortableBuildError("Windows media capability-smoke binding is invalid.")
        smoke_runtime = smoke.get("runtime")
        if (
            not isinstance(smoke_runtime, dict)
            or not str(smoke_runtime.get("ffmpeg", "")).startswith("ffmpeg version 8.1.2 ")
            or not str(smoke_runtime.get("ffprobe", "")).startswith("ffprobe version 8.1.2 ")
            or smoke_runtime.get("network_protocols_absent")
            != ["http", "https", "tcp", "tls", "udp"]
        ):
            raise PortableBuildError("Windows media runtime capability identity is invalid.")
        source_decode = smoke.get("source_decode")
        cover_art = smoke.get("cover_art_stream_copy")
        speed = smoke.get("speed_correction")
        chromaprint = smoke.get("chromaprint")
        if (
            not isinstance(source_decode, list)
            or {item.get("container") for item in source_decode if isinstance(item, dict)}
            != {"wav", "aiff"}
            or not isinstance(cover_art, dict)
            or set(cover_art) != {"jpg-flac", "jpg-m4a", "png-flac", "png-m4a"}
            or not isinstance(speed, dict)
            or "libsoxr" not in str(speed.get("filter", ""))
            or not isinstance(chromaprint, dict)
            or chromaprint.get("repeat_equal") is not True
            or chromaprint.get("backend") != "FFmpeg chromaprint muxer + Chromaprint 1.6.0 kissfft"
        ):
            raise PortableBuildError("Windows media synthetic capability coverage is incomplete.")

        source_sums, source_by_name = _verify_zip_sums(
            source_archive,
            source_members,
            "Windows media corresponding source",
        )
        if not WINDOWS_MEDIA_SOURCE_RECIPE.issubset(source_sums):
            raise PortableBuildError("Windows media corresponding-source recipe is incomplete.")
        runtime_sources = _source_input_inventory(manifest.get("source_inputs"))
        for name, digest in runtime_sources.items():
            source_name = f"inputs/{name}"
            if source_sums.get(source_name) != digest:
                raise PortableBuildError(
                    f"Corresponding source does not carry runtime input {name!r}."
                )
        capability_script = _read_zip_member(
            source_archive,
            source_by_name["recipe/capability_smoke.py"],
        )
        _extract_members(
            runtime_archive,
            runtime_members,
            destination,
            epoch,
            {},
            "Windows media runtime",
        )
        evidence = WindowsMediaEvidence(
            ffmpeg_sha256=runtime_sums["ffmpeg.exe"],
            ffprobe_sha256=runtime_sums["ffprobe.exe"],
            build_manifest_sha256=runtime_sums["BUILD-MANIFEST.json"],
            capability_smoke_sha256=runtime_sums["CAPABILITY-SMOKE.json"],
            profile="groove-serpent-minimal-audio-shared-v1",
            ffmpeg_version="8.1.2",
            source_input_count=len(runtime_sources),
        )
        return evidence, capability_script
    finally:
        runtime_archive.close()
        source_archive.close()


def _verify_wheel_record(
    archive: zipfile.ZipFile,
    members: Sequence[zipfile.ZipInfo],
    record_member: zipfile.ZipInfo,
    context: str,
) -> None:
    member_by_name = {member.filename: member for member in members if not member.is_dir()}
    record_payload = _read_zip_member(archive, record_member)
    try:
        rows = list(csv.reader(io.StringIO(record_payload.decode("utf-8"), newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise PortableBuildError(f"{context} has an unreadable RECORD file.") from exc
    recorded: set[str] = set()
    for row in rows:
        if len(row) != 3:
            raise PortableBuildError(f"{context} has a malformed RECORD row.")
        name, digest_field, size_field = row
        if name in recorded:
            raise PortableBuildError(f"{context} RECORD repeats {name!r}.")
        recorded.add(name)
        member = member_by_name.get(name)
        if member is None:
            raise PortableBuildError(f"{context} RECORD names a missing member: {name!r}")
        if name == record_member.filename:
            if digest_field or size_field:
                raise PortableBuildError(f"{context} RECORD must not hash itself.")
            continue
        if not digest_field.startswith("sha256=") or not size_field.isdecimal():
            raise PortableBuildError(f"{context} RECORD lacks a SHA-256 or size for {name!r}.")
        payload = _read_zip_member(archive, member)
        encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        if digest_field[7:].encode("ascii", errors="strict") != encoded:
            raise PortableBuildError(f"{context} RECORD hash failed for {name!r}.")
        if int(size_field) != len(payload):
            raise PortableBuildError(f"{context} RECORD size failed for {name!r}.")
    missing = set(member_by_name) - recorded
    if missing:
        sample = sorted(missing)[0]
        raise PortableBuildError(f"{context} has an unrecorded wheel member: {sample!r}")


def _verify_wheel(
    snapshot: Path,
    wheel: WheelInput,
) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo]]:
    try:
        archive = zipfile.ZipFile(snapshot, "r")
        members = _archive_members(archive, wheel.exact.label)
    except (OSError, zipfile.BadZipFile, PortableBuildError):
        try:
            archive.close()
        except UnboundLocalError:
            pass
        raise
    dist_name = _normalized_distribution(wheel.distribution).replace("-", "_")
    dist_prefix = f"{dist_name}-{wheel.version}.dist-info/"
    metadata_name = dist_prefix + "METADATA"
    record_name = dist_prefix + "RECORD"
    member_by_name = {member.filename: member for member in members}
    metadata_member = member_by_name.get(metadata_name)
    record_member = member_by_name.get(record_name)
    if metadata_member is None or record_member is None:
        archive.close()
        raise PortableBuildError(
            f"{wheel.exact.label} is missing the exact {dist_prefix!r} metadata directory."
        )
    metadata = BytesParser().parsebytes(_read_zip_member(archive, metadata_member))
    observed_name = metadata.get("Name", "")
    observed_version = metadata.get("Version", "")
    if _normalized_distribution(observed_name) != _normalized_distribution(wheel.distribution):
        archive.close()
        raise PortableBuildError(
            f"{wheel.exact.label} distribution mismatch: observed {observed_name!r}."
        )
    if observed_version != wheel.version:
        archive.close()
        raise PortableBuildError(
            f"{wheel.exact.label} version mismatch: observed {observed_version!r}."
        )
    _verify_wheel_record(archive, members, record_member, wheel.exact.label)
    return archive, members


def _extract_members(
    archive: zipfile.ZipFile,
    members: Sequence[zipfile.ZipInfo],
    destination: Path,
    epoch: int,
    occupied: dict[str, str],
    context: str,
) -> None:
    for member in members:
        relative = _archive_member_relative(member, context)
        key = _portable_key(relative)
        previous = occupied.get(key)
        if previous is not None:
            raise PortableBuildError(
                f"Portable output collision between {previous!r} and {relative.as_posix()!r}."
            )
        occupied[key] = relative.as_posix()
        target = destination.joinpath(*relative.parts)
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=False)
            os.utime(target, (epoch, epoch))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member, "r") as source, target.open("xb") as output:
            remaining = member.file_size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise PortableBuildError(f"Truncated archive member: {member.filename}")
                output.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise PortableBuildError(f"Oversized archive member: {member.filename}")
        os.utime(target, (epoch, epoch))


def _extract_wheel(
    snapshot: Path,
    wheel: WheelInput,
    destination: Path,
    epoch: int,
    occupied: dict[str, str],
) -> None:
    archive, members = _verify_wheel(snapshot, wheel)
    try:
        _extract_members(
            archive,
            members,
            destination,
            epoch,
            occupied,
            wheel.exact.label,
        )
    finally:
        archive.close()


def _extract_runtime(snapshot: Path, destination: Path, epoch: int) -> Path:
    try:
        with zipfile.ZipFile(snapshot, "r") as archive:
            members = _archive_members(archive, "Python embedded runtime")
            _extract_members(
                archive,
                members,
                destination,
                epoch,
                {},
                "Python embedded runtime",
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise PortableBuildError(f"Python embedded runtime is not a readable ZIP: {exc}") from exc
    python_executable = destination / "python.exe"
    pth_files = sorted(destination.glob("python*._pth"))
    if not python_executable.is_file() or len(pth_files) != 1:
        raise PortableBuildError(
            "Python embedded runtime must contain python.exe and exactly one python*._pth file."
        )
    pth = pth_files[0]
    library_entries = []
    for line in pth.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and stripped != "import site":
            if stripped not in library_entries:
                library_entries.append(stripped)
    if not any(
        entry.casefold().startswith("python") and entry.endswith(".zip")
        for entry in library_entries
    ):
        raise PortableBuildError("Python embedded runtime ._pth omits its standard-library ZIP.")
    lines = [*library_entries, r"..\app", "import site"]
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.utime(pth, (epoch, epoch))
    return python_executable


def _write_bytes(path: Path, payload: bytes, epoch: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(path, (epoch, epoch))


def _portable_readme(version: str) -> bytes:
    text = f"""Groove Serpent {version} - Windows x64 portable directory

RUN
  Double-click groove-serpent.cmd for command-line help, or run:
    groove-serpent.cmd doctor --json

SELF-CONTAINED BOUNDARY
  This directory includes its own Python runtime, Python packages, ffmpeg.exe,
  ffprobe.exe, shared FFmpeg libraries, libsoxr, and Chromaprint. The launcher
  prepends only this directory's tools folder.
  It does not require or invoke a separately installed Python.
  The exact bundled FFmpeg Chromaprint muxer is exercised during the build, so
  local fingerprint generation does not require a separate fpcalc executable.

UPDATE AND ROLLBACK
  Put each release in its own versioned directory. To update, close Groove
  Serpent and start the newer directory. To roll back, close it and start the
  older directory. Never merge or overwrite two release directories.

UNINSTALL / REMOVE
  Close Groove Serpent, then delete only this versioned portable directory.
  The bundle creates no installer registration, service, account, telemetry,
  or automatic updater. Audio captures, project files, exports, and caches kept
  outside this directory are not removed. Inspect before deleting anything.

INTEGRITY AND SIGNING
  PORTABLE-MANIFEST.json records exact payload hashes and exact build inputs.
  Run verify-portable.cmd before use. When a trusted release receipt provides
  the manifest hash, pass:
    verify-portable.cmd --expected-manifest-sha256 64_HEX_CHARACTERS
  Without that external hash, verification proves consistency, not origin or
  authenticity. The verifier does not execute the app, NumPy, FFmpeg, or
  ffprobe while it hashes them; its bundled Python bootstrap is still part of
  the externally anchored payload trust boundary.
  This private foundation does not claim Authenticode or other code signing.

LICENSES
  See LICENSES, tools/LICENSES, THIRD-PARTY-NOTICES.txt, runtime/LICENSE.txt,
  and the license directories carried by installed wheels under app. The exact
  media-component corresponding-source archive is carried under
  CORRESPONDING-SOURCE and is bound by PORTABLE-MANIFEST.json. This pairing is
  evidence for review, not a legal-compliance certification.
"""
    return text.encode("utf-8")


def _launcher() -> bytes:
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        'set "GROOVE_SERPENT_HOME=%~dp0"\r\n'
        'set "NoDefaultCurrentDirectoryInExePath=1"\r\n'
        'set "PATH=%~dp0tools;%PATH%"\r\n'
        'set "PYTHONDONTWRITEBYTECODE=1"\r\n'
        'set "PYTHONNOUSERSITE=1"\r\n'
        'set "PYTHONUTF8=1"\r\n'
        '"%~dp0runtime\\python.exe" -B -m groove_serpent %*\r\n'
        "exit /b %errorlevel%\r\n"
    ).encode("ascii")


def _verifier_launcher() -> bytes:
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        'set "PYTHONDONTWRITEBYTECODE=1"\r\n'
        'set "PYTHONNOUSERSITE=1"\r\n'
        'set "PYTHONUTF8=1"\r\n'
        '"%~dp0runtime\\python.exe" -B "%~dp0verify-portable.py" '
        '--root "%~dp0." %*\r\n'
        "exit /b %errorlevel%\r\n"
    ).encode("ascii")


def _run_bounded(
    command: Sequence[str],
    *,
    environment: dict[str, str],
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortableBuildError(f"Portable smoke command could not complete: {exc}") from exc
    if len(completed.stdout) > MAX_DIAGNOSTIC_BYTES or len(completed.stderr) > MAX_DIAGNOSTIC_BYTES:
        raise PortableBuildError("Portable smoke command exceeded the diagnostic output limit.")
    if completed.returncode != 0:
        diagnostic = (completed.stderr or completed.stdout).decode("utf-8", errors="replace")
        raise PortableBuildError(
            f"Portable smoke command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{diagnostic[:4_096]}"
        )
    return completed


def _smoke_windows_media_runtime(
    root: Path,
    capability_script: Path,
    expected_sha256: str,
) -> dict[str, object]:
    report = root / ".windows-media-capability-smoke.json"
    work = root / "groove-serpent-windows-media-smoke-portable"
    report_identity: PathIdentity | None = None
    work_identity: PathIdentity | None = None
    try:
        _run_bounded(
            [
                sys.executable,
                "-B",
                str(capability_script),
                "--runtime-dir",
                str(root / "tools"),
                "--work-dir",
                str(work),
                "--report",
                str(report),
            ],
            environment={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONUTF8": "1",
            },
            timeout=240.0,
        )
        inspect_single_link_file(
            report,
            "Windows media capability report",
        )
        report_identity = capture_identity(report)
        if os.path.lexists(work):
            inspect_plain_directory(work, "Windows media capability work directory")
            work_identity = capture_identity(work)
        fresh = read_single_link_file(
            report,
            MAX_DIAGNOSTIC_BYTES,
            "Windows media capability report",
        )
        embedded = read_single_link_file(
            root / "tools" / "CAPABILITY-SMOKE.json",
            MAX_DIAGNOSTIC_BYTES,
            "Embedded Windows media capability report",
        )
        fresh_sha256 = hashlib.sha256(fresh).hexdigest()
        current_report = report.lstat()
        if not report_identity.matches_path(report, current_report):
            raise PortableBuildError(
                "Windows media capability report changed identity after reading."
            )
        report_identity = capture_identity(
            report,
            bind_file=True,
            content_sha256=fresh_sha256,
        )
        if fresh_sha256 != expected_sha256 or fresh != embedded:
            raise PortableBuildError(
                "Fresh Windows media capability smoke differs from the embedded proof."
            )
        return {
            "synthetic_supported_formats": "fresh-capability-smoke-exact-match",
            "media_capability_smoke_sha256": fresh_sha256,
        }
    finally:
        report_removed = (
            not os.path.lexists(report)
            if report_identity is None
            else unlink_if_owned_file(report, report_identity)
        )
        work_removed = (
            not os.path.lexists(work)
            if work_identity is None
            else remove_owned_tree(work, work_identity)
        )
        if (
            os.path.lexists(report)
            or os.path.lexists(work)
            or not (report_removed and work_removed)
        ):
            raise PortableBuildError(
                "Windows media smoke cleanup lost ownership; unknown paths were preserved."
            )


def _app_fingerprint_parity(
    root: Path,
    python_executable: Path,
    environment: dict[str, str],
) -> str:
    ffmpeg = root / "tools" / "ffmpeg.exe"
    with tempfile.TemporaryDirectory(prefix="groove-serpent-portable-fingerprint-") as value:
        temp = Path(value)
        source = temp / "synthetic.wav"
        generator = (
            "import array,sys,wave;"
            "p=sys.argv[1];rate=44100;frames=rate*20;"
            "w=wave.open(p,'wb');w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);"
            "chunk=4410;"
            "[(lambda a:w.writeframes(a.tobytes()))(array.array('h',"
            "[((i%200)-100)*220 for i in range(base,min(base+chunk,frames))]))"
            " for base in range(0,frames,chunk)];w.close()"
        )
        _run_bounded(
            [str(python_executable), "-B", "-c", generator, str(source)],
            environment=environment,
            timeout=30.0,
        )
        app_script = (
            "import json,sys;from pathlib import Path;"
            "from groove_serpent.recognition import "
            "AcoustIDRecognitionProvider,_discover_fingerprint_runtime;"
            "status,runtime=_discover_fingerprint_runtime();assert status.ready and runtime;"
            "p=AcoustIDRecognitionProvider(api_key='local-only',enabled=True);"
            "print(json.dumps(p._fingerprint(Path(sys.argv[1]),0,882000,44100,runtime=runtime),"
            "sort_keys=True))"
        )
        app_result = _run_bounded(
            [str(python_executable), "-B", "-c", app_script, str(source)],
            environment=environment,
            timeout=60.0,
        )
        try:
            app_payload = json.loads(app_result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortableBuildError(
                "Portable app fingerprint parity emitted invalid JSON."
            ) from exc
        if (
            not isinstance(app_payload, dict)
            or set(app_payload) != {"duration", "fingerprint"}
            or app_payload.get("duration") != 20
            or not isinstance(app_payload.get("fingerprint"), str)
        ):
            raise PortableBuildError("Portable app fingerprint parity payload is invalid.")
        direct_result = _run_bounded(
            [
                str(ffmpeg),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-af",
                "atrim=start_sample=0:end_sample=882000,asetpts=PTS-STARTPTS",
                "-ac",
                "1",
                "-ar",
                "11025",
                "-c:a",
                "pcm_s16le",
                "-algorithm",
                "1",
                "-fp_format",
                "base64",
                "-f",
                "chromaprint",
                "pipe:1",
            ],
            environment=environment,
            timeout=60.0,
        )
        direct = direct_result.stdout.strip()
        app_fingerprint = cast(str, app_payload["fingerprint"]).encode("ascii")
        if CHROMAPRINT_FINGERPRINT_RE.fullmatch(direct) is None or direct != app_fingerprint:
            raise PortableBuildError(
                "Groove Serpent fingerprint output differs from direct bundled FFmpeg."
            )
    return "exact-match"


def _smoke_bundle(
    root: Path,
    inputs: PortableInputs,
    media_evidence: WindowsMediaEvidence,
    media_smoke: dict[str, object],
) -> dict[str, object]:
    python_executable = root / "runtime" / "python.exe"
    tools = root / "tools"
    environment = {
        "PATH": str(tools),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "TEMP": tempfile.gettempdir(),
        "TMP": tempfile.gettempdir(),
    }

    version_result = _run_bounded(
        [str(python_executable), "-B", "-m", "groove_serpent", "--version"],
        environment=environment,
    )
    version_output = version_result.stdout.decode("utf-8", errors="strict").strip()
    if inputs.app_wheel.version not in version_output:
        raise PortableBuildError(f"Portable app version smoke returned {version_output!r}.")
    dependency_assertions = ";".join(
        (
            f"import {wheel.distribution.replace('-', '_')};"
            f"assert {wheel.distribution.replace('-', '_')}.__version__ == {wheel.version!r}"
        )
        for wheel in inputs.dependency_wheels
        if wheel.distribution.casefold() == "numpy"
    )
    resource_script = (
        "import importlib.resources,platform,struct,sys;"
        f"assert platform.python_version()=={inputs.python_version!r};"
        "assert struct.calcsize('P')*8==64;"
        f"assert sys.executable=={str(python_executable)!r};"
        "r=importlib.resources.files('groove_serpent').joinpath('web');"
        "assert all(r.joinpath(n).read_bytes() for n in "
        "('index.html','app.js','styles.css','album.html','album.js','album.css'));"
        + dependency_assertions
    )
    _run_bounded(
        [str(python_executable), "-B", "-c", resource_script],
        environment=environment,
    )
    doctor_result = _run_bounded(
        [str(python_executable), "-B", "-m", "groove_serpent", "doctor", "--json"],
        environment=environment,
    )
    try:
        doctor = json.loads(doctor_result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableBuildError("Portable doctor did not emit strict JSON.") from exc
    if (
        doctor.get("ready") is not True
        or doctor.get("groove_serpent_version") != inputs.app_wheel.version
    ):
        raise PortableBuildError("Portable doctor did not report the exact app as ready.")
    checks = {item.get("capability"): item for item in doctor.get("checks", [])}
    for name in ("ffmpeg", "ffprobe", "ffmpeg-libsoxr"):
        if checks.get(name, {}).get("status") != "ready":
            raise PortableBuildError(f"Portable doctor did not prove required capability {name}.")
    fingerprinting = checks.get("acoustic-fingerprinting", {})
    if (
        fingerprinting.get("status") != "ready"
        or fingerprinting.get("backend") != "ffmpeg-chromaprint"
    ):
        raise PortableBuildError(
            "Portable doctor did not prove the bundled FFmpeg Chromaprint backend."
        )
    expected_tools = {
        "ffmpeg": (tools / "ffmpeg.exe", media_evidence.ffmpeg_sha256),
        "ffprobe": (tools / "ffprobe.exe", media_evidence.ffprobe_sha256),
    }
    for name, (expected_path, expected_hash) in expected_tools.items():
        observed = Path(str(checks[name].get("executable", ""))).resolve()
        if observed != expected_path.resolve() or _sha256_file(observed) != expected_hash:
            raise PortableBuildError(
                f"Portable doctor escaped the exact bundled {name} executable."
            )
    fingerprint_executable = Path(str(fingerprinting.get("executable", ""))).resolve()
    if fingerprint_executable != expected_tools["ffmpeg"][0].resolve():
        raise PortableBuildError("Portable fingerprint readiness escaped the exact bundled FFmpeg.")
    fingerprint_result = _run_bounded(
        [
            str(fingerprint_executable),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono:d=15",
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            "11025",
            "-c:a",
            "pcm_s16le",
            "-algorithm",
            "1",
            "-fp_format",
            "base64",
            "-f",
            "chromaprint",
            "pipe:1",
        ],
        environment=environment,
        timeout=30.0,
    )
    fingerprint = fingerprint_result.stdout.strip()
    if CHROMAPRINT_FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise PortableBuildError(
            "Bundled FFmpeg returned an invalid Chromaprint smoke fingerprint."
        )
    ffmpeg_version = str(checks["ffmpeg"].get("version", ""))
    ffprobe_version = str(checks["ffprobe"].get("version", ""))
    if not ffmpeg_version.startswith("ffmpeg version "):
        raise PortableBuildError("Bundled ffmpeg identity smoke returned an unexpected version.")
    if not ffprobe_version.startswith("ffprobe version "):
        raise PortableBuildError("Bundled ffprobe identity smoke returned an unexpected version.")
    fingerprint_parity = _app_fingerprint_parity(
        root,
        python_executable,
        environment,
    )
    return {
        "app_version": inputs.app_wheel.version,
        "python_version": inputs.python_version,
        "architecture": "64-bit",
        "ffmpeg_version": ffmpeg_version,
        "ffprobe_version": ffprobe_version,
        "libsoxr": "exercised-ready",
        "chromaprint": "ffmpeg-muxer-exercised-ready",
        "app_fingerprint_parity": fingerprint_parity,
        "media_runtime_profile": media_evidence.profile,
        **media_smoke,
        "web_resources": "six-required-assets-readable",
        "doctor_ready": True,
    }


def _smoke_packaged_verifier(
    root: Path,
    expected_manifest_sha256: str,
) -> dict[str, object]:
    environment = {
        "PATH": str(root / "tools"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "TEMP": tempfile.gettempdir(),
        "TMP": tempfile.gettempdir(),
    }
    completed = _run_bounded(
        [
            str(root / "runtime" / "python.exe"),
            "-B",
            str(root / "verify-portable.py"),
            "--root",
            str(root),
            "--expected-manifest-sha256",
            expected_manifest_sha256,
        ],
        environment=environment,
        timeout=120.0,
    )
    try:
        decoded = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableBuildError("Packaged verifier did not emit strict JSON.") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise PortableBuildError("Packaged verifier did not emit a JSON object.")
    result = cast(dict[str, object], decoded)
    if (
        result.get("ok") is not True
        or result.get("manifest_sha256") != expected_manifest_sha256
        or result.get("authenticity") != "anchored-to-expected-manifest-sha256"
    ):
        raise PortableBuildError("Packaged verifier did not validate the exact staged bundle.")
    return result


def _validate_notices(
    root: Path,
    inputs: PortableInputs,
    smoke: dict[str, object],
) -> None:
    paths = (
        root / "THIRD-PARTY-NOTICES.txt",
        root / "tools" / "FFMPEG-CONFIGURE.txt",
        root / "tools" / "BUILD-MANIFEST.json",
    )
    try:
        combined = "\n".join(path.read_text(encoding="utf-8", errors="strict") for path in paths)
    except (OSError, UnicodeDecodeError) as exc:
        raise PortableBuildError("Portable third-party notices are not strict UTF-8 text.") from exc
    required_tokens = {
        inputs.python_version,
        inputs.app_wheel.version,
        *(wheel.version for wheel in inputs.dependency_wheels),
        str(smoke["ffmpeg_version"]),
        str(smoke["ffprobe_version"]),
        inputs.windows_media_runtime.sha256.casefold(),
        inputs.windows_media_corresponding_source.sha256.casefold(),
        WINDOWS_MEDIA_SOURCE_DESTINATION,
    }
    missing = sorted(token for token in required_tokens if token not in combined)
    if missing:
        raise PortableBuildError(
            "Portable notices do not identify every exact shipped component: " + ", ".join(missing)
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(root: Path, *, exclude: Iterable[str] = ()) -> list[InventoryItem]:
    excluded = {_portable_key(PurePosixPath(value)) for value in exclude}
    inventory: list[InventoryItem] = []
    portable_names: dict[str, str] = {}
    private_patterns = _private_patterns()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        key = _portable_key(relative)
        if key in excluded:
            continue
        result = path.lstat()
        if stat.S_ISLNK(result.st_mode) or _is_reparse(result):
            raise PortableBuildError(f"Portable output contains a link: {relative}")
        if path.is_dir():
            continue
        if not stat.S_ISREG(result.st_mode):
            raise PortableBuildError(f"Portable output contains a special file: {relative}")
        previous = portable_names.get(key)
        if previous is not None:
            raise PortableBuildError(
                "Portable output contains equivalent names: "
                f"{previous!r} and {relative.as_posix()!r}"
            )
        portable_names[key] = relative.as_posix()
        lowered = relative.name.casefold()
        if Path(lowered).suffix in FORBIDDEN_PAYLOAD_SUFFIXES or lowered.startswith(".env"):
            raise PortableBuildError(
                f"Portable output contains forbidden owner-data shape: {relative}"
            )
        if lowered.endswith(
            (
                ".album.json",
                ".click-scan.json",
                ".groove.json",
                ".restoration-recipe.json",
                ".tracklist.json",
            )
        ):
            raise PortableBuildError(f"Portable output contains a private project file: {relative}")
        if result.st_size <= 16 * 1024 * 1024:
            payload = path.read_bytes()
            for pattern in private_patterns:
                if pattern.search(payload):
                    raise PortableBuildError(
                        f"Portable output contains private material: {relative}"
                    )
        inventory.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256_file(path),
                "size": result.st_size,
            }
        )
    return inventory


def _input_manifest(inputs: PortableInputs) -> list[dict[str, object]]:
    values: list[tuple[str, ExactInput, dict[str, str]]] = [
        (
            "groove-serpent-wheel",
            inputs.app_wheel.exact,
            {"distribution": inputs.app_wheel.distribution, "version": inputs.app_wheel.version},
        ),
        ("python-embed", inputs.python_embed, {"version": inputs.python_version}),
        ("windows-media-runtime", inputs.windows_media_runtime, {}),
        (
            "windows-media-corresponding-source",
            inputs.windows_media_corresponding_source,
            {"destination": WINDOWS_MEDIA_SOURCE_DESTINATION},
        ),
        ("groove-serpent-license", inputs.groove_license, {}),
        ("third-party-notices", inputs.third_party_notices, {}),
        ("portable-verifier", inputs.portable_verifier, {}),
    ]
    for wheel in inputs.dependency_wheels:
        values.append(
            (
                "dependency-wheel",
                wheel.exact,
                {"distribution": wheel.distribution, "version": wheel.version},
            )
        )
    for resource in inputs.skill_files:
        values.append(
            (
                "skill-file",
                resource.exact,
                {"destination": resource.destination.as_posix()},
            )
        )
    result: list[dict[str, object]] = []
    for role, exact, extra in values:
        result.append(
            {
                "role": role,
                "filename": exact.path.name,
                "sha256": exact.sha256.casefold(),
                **extra,
            }
        )
    return sorted(
        result,
        key=lambda item: (str(item["role"]), str(item.get("filename", ""))),
    )


def _write_manifest(
    root: Path,
    inputs: PortableInputs,
    smoke: dict[str, object],
    epoch: int,
    builder_sha256: str,
) -> None:
    manifest_path = root / "PORTABLE-MANIFEST.json"
    inventory = _inventory(root, exclude=(manifest_path.name,))
    payload = {
        "schema": PORTABLE_SCHEMA,
        "app": {"name": "groove-serpent", "version": inputs.app_wheel.version},
        "platform": PORTABLE_PLATFORM,
        "build_epoch": epoch,
        "builder": {
            "name": "scripts/build_windows_portable.py",
            "sha256": builder_sha256,
        },
        "inputs": _input_manifest(inputs),
        "payload": {
            "member_count": len(inventory),
            "total_bytes": sum(item["size"] for item in inventory),
            "members": inventory,
        },
        "smoke": smoke,
        "publication": {
            "mode": "new-side-by-side-directory",
            "replacement": "refused",
            "rollback": "run-an-older-intact-version-directory",
        },
        "code_signing": {
            "status": "unsigned",
            "claim": "No Authenticode or other code signing was performed.",
        },
        "owner_data": "not accepted or discovered; only explicit exact build inputs were copied",
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    _write_bytes(manifest_path, encoded, epoch)


def _verify_manifest(root: Path) -> PortableManifest:
    manifest_path = root / "PORTABLE-MANIFEST.json"
    try:
        decoded = json.loads(manifest_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableBuildError("Portable manifest cannot be reopened.") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise PortableBuildError("Portable manifest root is not an object.")
    manifest = cast(dict[str, object], decoded)
    if manifest.get("schema") != PORTABLE_SCHEMA:
        raise PortableBuildError("Portable manifest schema changed after writing.")
    payload = manifest.get("payload")
    if not isinstance(payload, dict):
        raise PortableBuildError("Portable manifest payload is not an object.")
    recorded = payload.get("members")
    if not isinstance(recorded, list):
        raise PortableBuildError("Portable manifest members are not an array.")
    actual = _inventory(root, exclude=(manifest_path.name,))
    if recorded != actual:
        raise PortableBuildError("Portable manifest does not exactly match reopened payload bytes.")
    if payload.get("member_count") != len(actual):
        raise PortableBuildError("Portable manifest member count is inconsistent.")
    if payload.get("total_bytes") != sum(item["size"] for item in actual):
        raise PortableBuildError("Portable manifest byte count is inconsistent.")
    return cast(PortableManifest, manifest)


def _validate_epoch(epoch: int) -> int:
    if epoch < DEFAULT_EPOCH or epoch > 4_102_444_800:
        raise PortableBuildError("Build epoch must be between 1980 and 2100.")
    return epoch


def _validate_destination(output_root: Path, version: str) -> tuple[Path, Path]:
    _portable_component(version, "Version")
    try:
        # Audit the caller's lexical path before any resolution can erase a
        # symlink or Windows-junction component.
        root = ensure_plain_directory_path(
            output_root,
            "Portable output root",
            create=False,
        )
    except RuntimeError as exc:
        raise PortableBuildError(
            "Portable output root must have plain, existing directory ancestry."
        ) from exc
    require_stable_creation_identity(root, "Portable output")
    final = root / f"Groove-Serpent-{version}-{PORTABLE_PLATFORM}"
    stage = root / f".{final.name}.building-{os.getpid()}"
    if os.path.lexists(final) or os.path.lexists(stage):
        raise FileExistsError(f"Refusing to replace portable output or stage: {final}")
    return final, stage


def _publish_new_directory(stage: Path, final: Path) -> None:
    if os.name != "nt":
        raise PortableBuildError(
            "Windows portable publication is supported only by a native Windows builder."
        )
    try:
        rename_no_replace(stage, final)
    except FileExistsError:
        raise
    except OSError as exc:
        raise PortableBuildError(f"Atomic new-directory publication failed: {exc}") from exc


def build_portable_directory(
    inputs: PortableInputs,
    output_root: Path,
    *,
    epoch: int = DEFAULT_EPOCH,
) -> Path:
    """Build, smoke, and atomically publish a new self-contained Windows directory."""

    epoch = _validate_epoch(epoch)
    builder_path = Path(__file__).resolve()
    builder_sha256 = _sha256_file(builder_path)
    version = inputs.app_wheel.version
    if _normalized_distribution(inputs.app_wheel.distribution) != "groove-serpent":
        raise PortableBuildError("The application wheel must be the groove-serpent distribution.")
    if not inputs.dependency_wheels:
        raise PortableBuildError("At least one exact dependency wheel is required.")
    if not any(
        _normalized_distribution(wheel.distribution) == "numpy"
        for wheel in inputs.dependency_wheels
    ):
        raise PortableBuildError("The exact NumPy dependency wheel is required.")
    final, stage = _validate_destination(output_root, version)
    stage.mkdir()
    inspect_plain_directory(stage, "Portable staging directory")
    stage_identity = capture_identity(stage)
    completed = False
    try:
        input_directory = stage / ".verified-inputs"
        input_directory.mkdir()
        inspect_plain_directory(
            input_directory,
            "Portable verified-input staging directory",
        )
        input_directory_identity = capture_identity(input_directory)
        runtime_snapshot = _snapshot_input(inputs.python_embed, input_directory, epoch)
        runtime = stage / "runtime"
        runtime.mkdir()
        _extract_runtime(runtime_snapshot, runtime, epoch)

        app = stage / "app"
        app.mkdir()
        occupied: dict[str, str] = {}
        wheel_inputs = (inputs.app_wheel, *inputs.dependency_wheels)
        for wheel in wheel_inputs:
            snapshot = _snapshot_input(wheel.exact, input_directory, epoch)
            _extract_wheel(snapshot, wheel, app, epoch, occupied)

        media_runtime_snapshot = _snapshot_input(
            inputs.windows_media_runtime,
            input_directory,
            epoch,
        )
        media_source_snapshot = _snapshot_input(
            inputs.windows_media_corresponding_source,
            input_directory,
            epoch,
        )
        tools = stage / "tools"
        tools.mkdir()
        media_evidence, capability_script_payload = _verify_windows_media_pair(
            media_runtime_snapshot,
            media_source_snapshot,
            tools,
            epoch,
        )
        capability_script = input_directory / "windows-media-capability-smoke.py"
        _write_bytes(capability_script, capability_script_payload, epoch)
        _copy_exact_input(
            inputs.windows_media_corresponding_source,
            stage.joinpath(*PurePosixPath(WINDOWS_MEDIA_SOURCE_DESTINATION).parts),
            epoch,
        )
        _copy_exact_input(
            inputs.groove_license,
            stage / "LICENSES" / "GROOVE-SERPENT-LICENSE.txt",
            epoch,
        )
        _copy_exact_input(
            inputs.third_party_notices,
            stage / "THIRD-PARTY-NOTICES.txt",
            epoch,
        )
        _copy_exact_input(
            inputs.portable_verifier,
            stage / "verify-portable.py",
            epoch,
        )
        seen_skill_destinations: set[str] = set()
        for resource in inputs.skill_files:
            destination = PurePosixPath("skills", "groove-serpent", *resource.destination.parts)
            key = _portable_key(destination)
            if key in seen_skill_destinations:
                raise PortableBuildError(f"Repeated skill destination: {destination}")
            seen_skill_destinations.add(key)
            _copy_exact_input(resource.exact, stage.joinpath(*destination.parts), epoch)
        required_skill = {
            "skills/groove-serpent/SKILL.md",
            "skills/groove-serpent/agents/openai.yaml",
            "skills/groove-serpent/references/authority-contract.json",
        }
        if {
            f"skills/groove-serpent/{resource.destination.as_posix()}"
            for resource in inputs.skill_files
        } != required_skill:
            raise PortableBuildError(
                "The exact complete Groove Serpent skill contract is required."
            )

        _write_bytes(stage / "groove-serpent.cmd", _launcher(), epoch)
        _write_bytes(stage / "verify-portable.cmd", _verifier_launcher(), epoch)
        _write_bytes(stage / "README-PORTABLE.txt", _portable_readme(version), epoch)
        media_smoke = _smoke_windows_media_runtime(
            stage,
            capability_script,
            media_evidence.capability_smoke_sha256,
        )
        if not remove_owned_tree(input_directory, input_directory_identity):
            raise PortableBuildError(
                "Verified-input cleanup lost ownership; unknown paths were preserved."
            )
        smoke = _smoke_bundle(stage, inputs, media_evidence, media_smoke)
        _validate_notices(stage, inputs, smoke)
        if _sha256_file(builder_path) != builder_sha256:
            raise PortableBuildError("Portable builder changed during assembly.")
        _write_manifest(stage, inputs, smoke, epoch, builder_sha256)
        _verify_manifest(stage)
        manifest_sha256 = _sha256_file(stage / "PORTABLE-MANIFEST.json")
        _smoke_packaged_verifier(stage, manifest_sha256)
        _publish_new_directory(stage, final)
        inspect_plain_directory(final, "Published portable directory")
        if capture_identity(final) != stage_identity:
            raise PortableBuildError("Published portable directory changed identity.")
        _verify_manifest(final)
        _smoke_packaged_verifier(final, manifest_sha256)
        completed = True
        return final
    finally:
        if not completed:
            if not remove_owned_tree_candidates((stage, final), stage_identity):
                message = "Portable cleanup lost ownership; unknown paths were preserved."
                active = sys.exception()
                if active is None:
                    raise PortableBuildError(message)
                active.add_note(message)


def _exact(path: str, digest: str, label: str) -> ExactInput:
    return ExactInput(Path(path), _validated_sha256(digest, f"{label} SHA-256"), label)


def _parse_dependency(values: Sequence[Sequence[str]]) -> tuple[WheelInput, ...]:
    result = []
    for distribution, version, path, digest in values:
        result.append(
            WheelInput(
                _exact(path, digest, f"{distribution} dependency wheel"),
                distribution,
                version,
            )
        )
    return tuple(result)


def _parse_skill(values: Sequence[Sequence[str]]) -> tuple[ResourceInput, ...]:
    result = []
    for destination, path, digest in values:
        result.append(
            ResourceInput(
                _exact(path, digest, f"Skill file {destination}"),
                _safe_relative_path(destination, "Skill destination"),
            )
        )
    return tuple(result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a self-contained, exact-input, unsigned Windows x64 portable directory."
        )
    )
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--dependency-wheel",
        action="append",
        nargs=4,
        metavar=("DIST", "VERSION", "PATH", "SHA256"),
        default=[],
    )
    parser.add_argument("--python-embed", required=True)
    parser.add_argument("--python-embed-sha256", required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--windows-media-runtime", required=True)
    parser.add_argument("--windows-media-runtime-sha256", required=True)
    parser.add_argument("--windows-media-corresponding-source", required=True)
    parser.add_argument("--windows-media-corresponding-source-sha256", required=True)
    parser.add_argument("--groove-license", required=True)
    parser.add_argument("--groove-license-sha256", required=True)
    parser.add_argument("--third-party-notices", required=True)
    parser.add_argument("--third-party-notices-sha256", required=True)
    parser.add_argument("--portable-verifier", required=True)
    parser.add_argument("--portable-verifier-sha256", required=True)
    parser.add_argument(
        "--skill-file",
        action="append",
        nargs=3,
        metavar=("DEST", "PATH", "SHA256"),
        default=[],
    )
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--epoch",
        type=int,
        default=int(os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_EPOCH)),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    inputs = PortableInputs(
        app_wheel=WheelInput(
            _exact(args.wheel, args.wheel_sha256, "Groove Serpent wheel"),
            "groove-serpent",
            args.version,
        ),
        dependency_wheels=_parse_dependency(args.dependency_wheel),
        python_embed=_exact(
            args.python_embed,
            args.python_embed_sha256,
            "Python embedded runtime",
        ),
        python_version=args.python_version,
        windows_media_runtime=_exact(
            args.windows_media_runtime,
            args.windows_media_runtime_sha256,
            "Windows media runtime",
        ),
        windows_media_corresponding_source=_exact(
            args.windows_media_corresponding_source,
            args.windows_media_corresponding_source_sha256,
            "Windows media corresponding source",
        ),
        groove_license=_exact(
            args.groove_license,
            args.groove_license_sha256,
            "Groove Serpent license",
        ),
        third_party_notices=_exact(
            args.third_party_notices,
            args.third_party_notices_sha256,
            "Third-party notices",
        ),
        portable_verifier=_exact(
            args.portable_verifier,
            args.portable_verifier_sha256,
            "Portable verifier",
        ),
        skill_files=_parse_skill(args.skill_file),
    )
    output = build_portable_directory(inputs, args.output_root, epoch=args.epoch)
    manifest = _verify_manifest(output)
    print(
        json.dumps(
            {
                "output": str(output),
                "manifest_sha256": _sha256_file(output / "PORTABLE-MANIFEST.json"),
                "member_count": manifest["payload"]["member_count"],
                "total_bytes": manifest["payload"]["total_bytes"],
                "unsigned": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
