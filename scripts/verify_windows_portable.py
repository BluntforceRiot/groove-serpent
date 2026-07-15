from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import NoReturn, Sequence, TypedDict, cast


MANIFEST_SCHEMA = "groove-serpent.windows-portable-manifest/2"
VERIFICATION_SCHEMA = "groove-serpent.windows-portable-verification/2"
PLATFORM = "windows-x64"
MANIFEST_NAME = "PORTABLE-MANIFEST.json"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBERS = 50_000
MAX_RELATIVE_PATH_LENGTH = 240
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
VERSION_RE = re.compile(r"[0-9][0-9A-Za-z._+-]{0,127}\Z")
PYTHON_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.\d+(?:[A-Za-z0-9._+-]*)\Z")
WINDOWS_FORBIDDEN = frozenset('<>:"\\|?*')
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
REPARSE_POINT_ATTRIBUTE = 0x400
REQUIRED_WEB_ASSETS = (
    "album.css",
    "album.html",
    "album.js",
    "app.js",
    "index.html",
    "styles.css",
)
REQUIRED_SKILL_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "references/authority-contract.json",
)
WINDOWS_MEDIA_SOURCE_FILENAME = "groove-serpent-windows-media-8.1.2-corresponding-source.zip"
WINDOWS_MEDIA_SOURCE_PATH = f"CORRESPONDING-SOURCE/{WINDOWS_MEDIA_SOURCE_FILENAME}"
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
TOP_LEVEL_KEYS = frozenset(
    {
        "app",
        "build_epoch",
        "builder",
        "code_signing",
        "inputs",
        "owner_data",
        "payload",
        "platform",
        "publication",
        "schema",
        "smoke",
    }
)
SMOKE_KEYS = frozenset(
    {
        "app_version",
        "app_fingerprint_parity",
        "architecture",
        "chromaprint",
        "doctor_ready",
        "ffmpeg_version",
        "ffprobe_version",
        "libsoxr",
        "media_capability_smoke_sha256",
        "media_runtime_profile",
        "python_version",
        "synthetic_supported_formats",
        "web_resources",
    }
)
SINGLE_INPUT_ROLES = frozenset(
    {
        "groove-serpent-license",
        "groove-serpent-wheel",
        "python-embed",
        "third-party-notices",
        "portable-verifier",
        "windows-media-corresponding-source",
        "windows-media-runtime",
    }
)


class VerificationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Member(TypedDict):
    path: str
    sha256: str
    size: int


class VerifiedPortable(TypedDict):
    schema: str
    ok: bool
    authenticity: str
    manifest_sha256: str
    app_name: str
    app_version: str
    platform: str
    member_count: int
    total_bytes: int
    launcher: str
    verifier_launcher: str
    python_runtime: str
    ffmpeg: str
    ffprobe: str
    fingerprint_backend: str
    corresponding_source: str


class StrictArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise VerificationError("usage", message)


def expected_app_launcher() -> bytes:
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


def expected_verifier_launcher() -> bytes:
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


def _fail(code: str, message: str) -> NoReturn:
    raise VerificationError(code, message)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _valid_sha256(value: object, context: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _fail("schema", f"{context} is not a lowercase SHA-256.")
    return value


def _text(value: object, context: str, *, maximum: int = 4_096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        _fail("schema", f"{context} is not bounded non-empty text.")
    return value


def _integer(value: object, context: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        _fail("schema", f"{context} is not a bounded non-negative integer.")
    return value


def _object(value: object, context: str, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail("schema", f"{context} is not an object.")
    result = cast(dict[str, object], value)
    if set(result) != keys:
        _fail("schema", f"{context} fields are not the exact supported schema.")
    return result


def _array(value: object, context: str, *, maximum: int) -> list[object]:
    if not isinstance(value, list) or len(value) > maximum:
        _fail("schema", f"{context} is not a bounded array.")
    return cast(list[object], value)


def _portable_component(value: str, context: str) -> str:
    if not value or value in {".", ".."}:
        _fail("unsafe_path", f"{context} contains traversal or an empty component.")
    if value != unicodedata.normalize("NFC", value):
        _fail("unsafe_path", f"{context} is not canonical NFC text.")
    if value[-1] in {" ", "."}:
        _fail("unsafe_path", f"{context} has a trailing space or period.")
    if any(ord(character) < 32 or character in WINDOWS_FORBIDDEN for character in value):
        _fail("unsafe_path", f"{context} contains a Windows-unsafe character.")
    if value.split(".", 1)[0].casefold() in WINDOWS_RESERVED:
        _fail("unsafe_path", f"{context} uses a reserved Windows device name.")
    if len(value.encode("utf-16-le")) // 2 > 255:
        _fail("unsafe_path", f"{context} exceeds the Windows component limit.")
    return value


def _safe_relative(value: object, context: str) -> PurePosixPath:
    text = _text(value, context, maximum=MAX_RELATIVE_PATH_LENGTH)
    if "\\" in text:
        _fail("unsafe_path", f"{context} is not a canonical forward-slash path.")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or path.as_posix() != text:
        _fail("unsafe_path", f"{context} is not a relative path.")
    for component in path.parts:
        _portable_component(component, context)
    return path


def _portable_key(path: PurePosixPath) -> str:
    return unicodedata.normalize("NFC", path.as_posix()).casefold()


def _is_reparse(result: os.stat_result) -> bool:
    return bool(getattr(result, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE)


def _regular_bytes(path: Path, context: str, maximum: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise VerificationError("missing", f"{context} cannot be inspected.") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        _fail("unsafe_file", f"{context} is not a single-link regular file.")
    if before.st_size < 0 or before.st_size > maximum:
        _fail("size", f"{context} exceeds the supported size.")
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not os.path.samestat(before, opened):
                _fail("race", f"{context} changed identity while opening.")
            payload = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise VerificationError("read", f"{context} could not be read.") from exc
    if not os.path.samestat(opened, after) or len(payload) != opened.st_size:
        _fail("race", f"{context} changed while reading.")
    if len(payload) > maximum:
        _fail("size", f"{context} exceeds the supported size.")
    return payload


def _hash_regular_file(path: Path, context: str, expected_size: int) -> str:
    try:
        before = path.lstat()
    except OSError as exc:
        raise VerificationError("missing", f"{context} is missing.") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        _fail("unsafe_file", f"{context} is not a single-link regular file.")
    if before.st_size != expected_size:
        _fail("size_mismatch", f"{context} size does not match the manifest.")
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if not os.path.samestat(before, opened):
                _fail("race", f"{context} changed identity while opening.")
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > expected_size:
                    _fail("size_mismatch", f"{context} grew while reading.")
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise VerificationError("read", f"{context} could not be read.") from exc
    if total != expected_size or not os.path.samestat(opened, after):
        _fail("race", f"{context} changed while reading.")
    return digest.hexdigest()


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_json_key", "Manifest JSON contains a duplicate object key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    _fail("nonfinite_json", f"Manifest JSON contains non-finite value {value}.")


def _parse_manifest(payload: bytes) -> dict[str, object]:
    try:
        decoded = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_constant,
        )
    except VerificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("manifest_json", "Manifest is not strict UTF-8 JSON.") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        _fail("schema", "Manifest root is not an object.")
    return cast(dict[str, object], decoded)


def _validate_inputs(value: object) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    raw = _array(value, "inputs", maximum=1_000)
    inputs: list[dict[str, object]] = []
    by_single_role: dict[str, dict[str, object]] = {}
    seen_records: set[tuple[str, str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            _fail("schema", f"inputs[{index}] is not an object.")
        record = cast(dict[str, object], item)
        role = _text(record.get("role"), f"inputs[{index}].role", maximum=64)
        common = {"role", "filename", "sha256"}
        if role in {"groove-serpent-wheel", "dependency-wheel"}:
            expected = common | {"distribution", "version"}
        elif role == "python-embed":
            expected = common | {"version"}
        elif role in {"skill-file", "windows-media-corresponding-source"}:
            expected = common | {"destination"}
        elif role in SINGLE_INPUT_ROLES:
            expected = common
        else:
            _fail("schema", f"inputs[{index}] has an unsupported role.")
        if set(record) != expected:
            _fail("schema", f"inputs[{index}] fields do not match its role.")
        filename = _portable_component(
            _text(record["filename"], f"inputs[{index}].filename", maximum=255),
            f"inputs[{index}].filename",
        )
        digest = _valid_sha256(record["sha256"], f"inputs[{index}].sha256")
        unique = (role, filename.casefold(), digest)
        if unique in seen_records:
            _fail("schema", "Manifest inputs contain a duplicate record.")
        seen_records.add(unique)
        if "distribution" in record:
            _text(record["distribution"], f"inputs[{index}].distribution", maximum=128)
        if "version" in record:
            version = _text(record["version"], f"inputs[{index}].version", maximum=128)
            if VERSION_RE.fullmatch(version) is None:
                _fail("schema", f"inputs[{index}].version is invalid.")
        if "destination" in record:
            destination = _safe_relative(
                record["destination"],
                f"inputs[{index}].destination",
            ).as_posix()
            if (
                role == "windows-media-corresponding-source"
                and destination != WINDOWS_MEDIA_SOURCE_PATH
            ):
                _fail("schema", "Corresponding-source destination is unsupported.")
        if role in SINGLE_INPUT_ROLES:
            if role in by_single_role:
                _fail("schema", f"Manifest repeats singleton input role {role}.")
            by_single_role[role] = record
        inputs.append(record)
    if set(by_single_role) != SINGLE_INPUT_ROLES:
        _fail("schema", "Manifest does not contain every singleton build input exactly once.")
    if not any(record.get("role") == "dependency-wheel" for record in inputs):
        _fail("schema", "Manifest contains no dependency wheel.")
    skill_destinations = {
        cast(str, record["destination"]) for record in inputs if record.get("role") == "skill-file"
    }
    if skill_destinations != set(REQUIRED_SKILL_FILES):
        _fail("schema", "Manifest skill inputs are not the exact required skill files.")
    ordering = [(cast(str, record["role"]), cast(str, record["filename"])) for record in inputs]
    if ordering != sorted(ordering):
        _fail("schema", "Manifest inputs are not in deterministic order.")
    return inputs, by_single_role


def _validate_members(value: object) -> tuple[list[Member], dict[str, Member]]:
    raw = _array(value, "payload.members", maximum=MAX_MEMBERS)
    members: list[Member] = []
    by_path: dict[str, Member] = {}
    portable_paths: dict[str, str] = {}
    for index, item in enumerate(raw):
        record = _object(
            item,
            f"payload.members[{index}]",
            frozenset({"path", "sha256", "size"}),
        )
        relative = _safe_relative(record["path"], f"payload.members[{index}].path")
        path = relative.as_posix()
        if path == MANIFEST_NAME:
            _fail("schema", "Manifest must not list itself as a payload member.")
        key = _portable_key(relative)
        previous = portable_paths.get(key)
        if previous is not None:
            _fail("portable_collision", "Manifest payload paths are portable-equivalent.")
        portable_paths[key] = path
        if path in by_path:
            _fail("schema", "Manifest payload repeats an exact path.")
        member: Member = {
            "path": path,
            "sha256": _valid_sha256(record["sha256"], f"payload.members[{index}].sha256"),
            "size": _integer(
                record["size"],
                f"payload.members[{index}].size",
                maximum=MAX_MEMBER_BYTES,
            ),
        }
        members.append(member)
        by_path[path] = member
    expected_order = sorted((item["path"] for item in members), key=str.casefold)
    if [item["path"] for item in members] != expected_order:
        _fail("schema", "Manifest payload members are not in deterministic order.")
    return members, by_path


def _strict_manifest(
    value: dict[str, object],
) -> tuple[
    str,
    str,
    list[Member],
    dict[str, Member],
    list[dict[str, object]],
    dict[str, dict[str, object]],
]:
    manifest = _object(value, "manifest", TOP_LEVEL_KEYS)
    if manifest["schema"] != MANIFEST_SCHEMA:
        _fail("schema", "Manifest schema is unsupported.")
    app = _object(manifest["app"], "app", frozenset({"name", "version"}))
    app_name = _text(app["name"], "app.name", maximum=64)
    if app_name != "groove-serpent":
        _fail("claim", "Manifest app name is not Groove Serpent.")
    app_version = _text(app["version"], "app.version", maximum=128)
    if VERSION_RE.fullmatch(app_version) is None:
        _fail("schema", "Manifest app version is invalid.")
    if manifest["platform"] != PLATFORM:
        _fail("claim", "Manifest platform is not windows-x64.")
    _integer(manifest["build_epoch"], "build_epoch", maximum=4_102_444_800)
    builder = _object(manifest["builder"], "builder", frozenset({"name", "sha256"}))
    if builder["name"] != "scripts/build_windows_portable.py":
        _fail("claim", "Manifest builder name is unsupported.")
    _valid_sha256(builder["sha256"], "builder.sha256")
    inputs, singleton_inputs = _validate_inputs(manifest["inputs"])
    payload = _object(
        manifest["payload"],
        "payload",
        frozenset({"member_count", "members", "total_bytes"}),
    )
    members, by_path = _validate_members(payload["members"])
    member_count = _integer(payload["member_count"], "payload.member_count", maximum=MAX_MEMBERS)
    total_bytes = _integer(payload["total_bytes"], "payload.total_bytes", maximum=MAX_TOTAL_BYTES)
    if member_count != len(members):
        _fail("count_mismatch", "Manifest member count does not match its member array.")
    if total_bytes != sum(member["size"] for member in members):
        _fail("size_mismatch", "Manifest byte count does not match its member array.")
    smoke = _object(manifest["smoke"], "smoke", SMOKE_KEYS)
    if smoke["app_version"] != app_version:
        _fail("claim", "Smoke app version does not match manifest app version.")
    python_version = _text(smoke["python_version"], "smoke.python_version", maximum=128)
    if PYTHON_VERSION_RE.fullmatch(python_version) is None:
        _fail("claim", "Smoke Python version is invalid.")
    if smoke["architecture"] != "64-bit":
        _fail("claim", "Smoke architecture is not 64-bit.")
    if smoke["doctor_ready"] is not True:
        _fail("claim", "Smoke does not record doctor readiness.")
    if smoke["libsoxr"] != "exercised-ready":
        _fail("claim", "Smoke does not record an exercised libsoxr path.")
    if smoke["chromaprint"] != "ffmpeg-muxer-exercised-ready":
        _fail("claim", "Smoke does not record an exercised FFmpeg Chromaprint path.")
    if smoke["web_resources"] != "six-required-assets-readable":
        _fail("claim", "Smoke does not record all required web resources.")
    if smoke["app_fingerprint_parity"] != "exact-match":
        _fail("claim", "Smoke does not bind app and direct FFmpeg fingerprints.")
    if smoke["synthetic_supported_formats"] != ("fresh-capability-smoke-exact-match"):
        _fail("claim", "Smoke does not record the fresh media capability suite.")
    if smoke["media_runtime_profile"] != "groove-serpent-minimal-audio-shared-v1":
        _fail("claim", "Smoke records an unsupported media runtime profile.")
    _valid_sha256(
        smoke["media_capability_smoke_sha256"],
        "smoke.media_capability_smoke_sha256",
    )
    ffmpeg_version = _text(smoke["ffmpeg_version"], "smoke.ffmpeg_version")
    ffprobe_version = _text(smoke["ffprobe_version"], "smoke.ffprobe_version")
    if not ffmpeg_version.startswith("ffmpeg version "):
        _fail("claim", "Smoke FFmpeg identity is invalid.")
    if not ffprobe_version.startswith("ffprobe version "):
        _fail("claim", "Smoke ffprobe identity is invalid.")
    publication = _object(
        manifest["publication"],
        "publication",
        frozenset({"mode", "replacement", "rollback"}),
    )
    if publication != {
        "mode": "new-side-by-side-directory",
        "replacement": "refused",
        "rollback": "run-an-older-intact-version-directory",
    }:
        _fail("claim", "Manifest publication claims are unsupported.")
    signing = _object(
        manifest["code_signing"],
        "code_signing",
        frozenset({"claim", "status"}),
    )
    if signing != {
        "status": "unsigned",
        "claim": "No Authenticode or other code signing was performed.",
    }:
        _fail("claim", "Manifest signing claim is not the supported unsigned claim.")
    if manifest["owner_data"] != (
        "not accepted or discovered; only explicit exact build inputs were copied"
    ):
        _fail("claim", "Manifest owner-data claim is unsupported.")
    return (
        app_name,
        app_version,
        members,
        by_path,
        inputs,
        singleton_inputs,
    )


def _required_paths(app_version: str, python_version: str) -> set[str]:
    match = PYTHON_VERSION_RE.fullmatch(python_version)
    if match is None:
        _fail("claim", "Smoke Python version is invalid.")
    major, minor = match.groups()
    dist = f"app/groove_serpent-{app_version}.dist-info"
    required = {
        WINDOWS_MEDIA_SOURCE_PATH,
        "LICENSES/GROOVE-SERPENT-LICENSE.txt",
        "README-PORTABLE.txt",
        "THIRD-PARTY-NOTICES.txt",
        "app/groove_serpent/__init__.py",
        f"{dist}/METADATA",
        "groove-serpent.cmd",
        "runtime/python.exe",
        f"runtime/python{major}{minor}._pth",
        f"runtime/python{major}{minor}.zip",
        "tools/ffmpeg.exe",
        "tools/ffprobe.exe",
        "tools/BUILD-MANIFEST.json",
        "tools/CAPABILITY-SMOKE.json",
        "tools/FFMPEG-CONFIGURE.txt",
        "tools/SHA256SUMS",
        "tools/libchromaprint.dll",
        "tools/libsoxr.dll",
        "verify-portable.cmd",
        "verify-portable.py",
    }
    required.update(f"tools/{name}" for name in WINDOWS_MEDIA_BINARIES)
    required.update(f"app/groove_serpent/web/{name}" for name in REQUIRED_WEB_ASSETS)
    required.update(f"skills/groove-serpent/{name}" for name in REQUIRED_SKILL_FILES)
    return required


def _walk_bundle(root: Path) -> tuple[dict[str, Path], set[str]]:
    files: dict[str, Path] = {}
    directories: set[str] = set()
    portable_entries: dict[str, str] = {}
    stack: list[tuple[Path, PurePosixPath | None]] = [(root, None)]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise VerificationError("read", "Bundle directory cannot be enumerated.") from exc
        for entry in entries:
            relative = (
                PurePosixPath(entry.name)
                if prefix is None
                else PurePosixPath(*prefix.parts, entry.name)
            )
            _safe_relative(relative.as_posix(), "bundle path")
            key = _portable_key(relative)
            previous = portable_entries.get(key)
            if previous is not None:
                _fail("portable_collision", "Bundle contains portable-equivalent paths.")
            portable_entries[key] = relative.as_posix()
            entry_path = Path(entry.path)
            try:
                # DirEntry.stat() can return a synthetic st_nlink of zero on
                # Windows network volumes. Path.lstat() performs the full
                # filesystem query needed for the hard-link/reparse checks.
                result = entry_path.lstat()
            except OSError as exc:
                raise VerificationError("read", "Bundle entry cannot be inspected.") from exc
            if entry.is_symlink() or stat.S_ISLNK(result.st_mode) or _is_reparse(result):
                _fail("unsafe_file", "Bundle contains a symlink or reparse point.")
            if stat.S_ISDIR(result.st_mode):
                directories.add(relative.as_posix())
                stack.append((entry_path, relative))
            elif stat.S_ISREG(result.st_mode):
                if result.st_nlink != 1:
                    _fail("unsafe_file", "Bundle contains a multiply-linked file.")
                files[relative.as_posix()] = entry_path
            else:
                _fail("unsafe_file", "Bundle contains a special file.")
    return files, directories


def _expected_directories(paths: set[str]) -> set[str]:
    result: set[str] = set()
    for value in paths:
        parent = PurePosixPath(value).parent
        while parent.parts:
            result.add(parent.as_posix())
            parent = parent.parent
    return result


def _media_sums(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise VerificationError("identity", "Media SHA256SUMS is not UTF-8.") from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\r\n]+)", line)
        if match is None:
            _fail("identity", "Media SHA256SUMS has a malformed record.")
        path = _safe_relative(match.group(2), "media SHA256SUMS path").as_posix()
        if path in result:
            _fail("identity", "Media SHA256SUMS repeats a path.")
        result[path] = match.group(1)
    if not result:
        _fail("identity", "Media SHA256SUMS is empty.")
    return result


def _assert_media_runtime_identity(
    root: Path,
    by_path: dict[str, Member],
    singleton_inputs: dict[str, dict[str, object]],
    smoke: dict[str, object],
) -> None:
    source_input = singleton_inputs["windows-media-corresponding-source"]
    if (
        source_input.get("filename") != WINDOWS_MEDIA_SOURCE_FILENAME
        or source_input.get("destination") != WINDOWS_MEDIA_SOURCE_PATH
        or source_input["sha256"] != by_path[WINDOWS_MEDIA_SOURCE_PATH]["sha256"]
    ):
        _fail("identity", "Corresponding source is not bound to its exact carried archive.")
    runtime_input = singleton_inputs["windows-media-runtime"]
    if runtime_input.get("filename") != "groove-serpent-windows-media-8.1.2-x86_64.zip":
        _fail("identity", "Windows media runtime input name is unsupported.")

    sums_payload = _regular_bytes(
        root / "tools" / "SHA256SUMS",
        "media SHA256SUMS",
        1024 * 1024,
    )
    sums = _media_sums(sums_payload)
    tool_members = {
        path.removeprefix("tools/"): member
        for path, member in by_path.items()
        if path.startswith("tools/") and path != "tools/SHA256SUMS"
    }
    if set(sums) != set(tool_members):
        _fail("identity", "Media SHA256SUMS does not cover the exact tools payload.")
    for path, digest in sums.items():
        if tool_members[path]["sha256"] != digest:
            _fail("identity", "Media SHA256SUMS disagrees with the portable manifest.")
    binaries = {path for path in sums if PurePosixPath(path).suffix.casefold() in {".dll", ".exe"}}
    if binaries != WINDOWS_MEDIA_BINARIES:
        _fail("identity", "Media runtime binary inventory is unsupported.")

    build_manifest_payload = _regular_bytes(
        root / "tools" / "BUILD-MANIFEST.json",
        "media build manifest",
        MAX_MANIFEST_BYTES,
    )
    build_manifest = _parse_manifest(build_manifest_payload)
    if build_manifest.get("schema") != "groove-serpent.windows-media-runtime-manifest/1":
        _fail("identity", "Media runtime manifest schema is unsupported.")
    artifact = build_manifest.get("artifact")
    if not isinstance(artifact, dict) or (
        artifact.get("architecture") != "x86_64-w64-mingw32"
        or artifact.get("ffmpeg_version") != "8.1.2"
        or artifact.get("profile") != "groove-serpent-minimal-audio-shared-v1"
    ):
        _fail("identity", "Media runtime artifact identity is unsupported.")
    runtime_files = build_manifest.get("runtime_files")
    if not isinstance(runtime_files, list):
        _fail("identity", "Media runtime file inventory is absent.")
    manifest_files: dict[str, str] = {}
    for item in runtime_files:
        if not isinstance(item, dict):
            _fail("identity", "Media runtime file record is invalid.")
        raw_path = item.get("path")
        raw_digest = item.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(raw_digest, str):
            _fail("identity", "Media runtime file identity is incomplete.")
        relative = _safe_relative(raw_path, "media runtime file").as_posix()
        if relative in manifest_files:
            _fail("identity", "Media runtime manifest repeats a file.")
        manifest_files[relative] = _valid_sha256(
            raw_digest,
            "media runtime file SHA-256",
        )
    if set(manifest_files) != set(sums) - {"BUILD-MANIFEST.json"}:
        _fail("identity", "Media runtime manifest inventory is not exact.")
    if any(sums[path] != digest for path, digest in manifest_files.items()):
        _fail("identity", "Media runtime manifest disagrees with SHA256SUMS.")
    capability_sha256 = sums.get("CAPABILITY-SMOKE.json")
    if (
        capability_sha256 is None
        or build_manifest.get("capability_smoke_sha256") != capability_sha256
        or smoke["media_capability_smoke_sha256"] != capability_sha256
    ):
        _fail("identity", "Media capability proof is not consistently bound.")


def _assert_static_identity(
    root: Path,
    app_version: str,
    by_path: dict[str, Member],
    inputs: list[dict[str, object]],
    singleton_inputs: dict[str, dict[str, object]],
    python_version: str,
    smoke: dict[str, object],
) -> None:
    required = _required_paths(app_version, python_version)
    missing = required - set(by_path)
    if missing:
        _fail("missing", "Manifest omits a required portable runtime path.")
    mappings = {
        "groove-serpent-license": "LICENSES/GROOVE-SERPENT-LICENSE.txt",
        "third-party-notices": "THIRD-PARTY-NOTICES.txt",
        "portable-verifier": "verify-portable.py",
        "windows-media-corresponding-source": WINDOWS_MEDIA_SOURCE_PATH,
    }
    for role, path in mappings.items():
        if singleton_inputs[role]["sha256"] != by_path[path]["sha256"]:
            _fail("identity", "A packaged exact input does not match its runtime path.")
    app_wheel = singleton_inputs["groove-serpent-wheel"]
    if app_wheel.get("distribution") != "groove-serpent" or app_wheel.get("version") != app_version:
        _fail("identity", "Application wheel input does not match the manifest app.")
    if singleton_inputs["python-embed"].get("version") != python_version:
        _fail("identity", "Python runtime input does not match the smoke version.")
    numpy_inputs = [
        record
        for record in inputs
        if record.get("role") == "dependency-wheel" and record.get("distribution") == "numpy"
    ]
    if len(numpy_inputs) != 1:
        _fail("identity", "Manifest does not bind exactly one NumPy wheel.")
    for record in inputs:
        if record.get("role") != "skill-file":
            continue
        destination = cast(str, record["destination"])
        path = f"skills/groove-serpent/{destination}"
        if by_path[path]["sha256"] != record["sha256"]:
            _fail("identity", "A packaged skill file does not match its exact input.")
    launcher = _regular_bytes(root / "groove-serpent.cmd", "app launcher", 16_384)
    if launcher != expected_app_launcher():
        _fail("identity", "Application launcher bytes are unsupported.")
    verifier_launcher = _regular_bytes(root / "verify-portable.cmd", "verifier launcher", 16_384)
    if verifier_launcher != expected_verifier_launcher():
        _fail("identity", "Verifier launcher bytes are unsupported.")
    match = PYTHON_VERSION_RE.fullmatch(python_version)
    if match is None:
        _fail("claim", "Smoke Python version is invalid.")
    major, minor = match.groups()
    pth = _regular_bytes(
        root / "runtime" / f"python{major}{minor}._pth",
        "embedded Python path file",
        16_384,
    )
    expected_pth = f"python{major}{minor}.zip\n.\n..\\app\nimport site\n".encode("utf-8")
    if pth != expected_pth:
        _fail("identity", "Embedded Python path isolation file is unsupported.")
    initializer = _regular_bytes(
        root / "app" / "groove_serpent" / "__init__.py",
        "application version source",
        64 * 1024,
    )
    version_pattern = re.compile(
        rb'^__version__\s*=\s*["\']' + re.escape(app_version.encode("ascii")) + rb'["\']\s*$',
        re.MULTILINE,
    )
    if version_pattern.search(initializer) is None:
        _fail("identity", "Static application version does not match the manifest.")
    metadata_path = root / "app" / f"groove_serpent-{app_version}.dist-info" / "METADATA"
    metadata = _regular_bytes(metadata_path, "application wheel metadata", 16 * 1024 * 1024)
    try:
        metadata_text = metadata.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise VerificationError("identity", "Application metadata is not UTF-8.") from exc
    metadata_lines = metadata_text.splitlines()
    if metadata_lines.count("Name: groove-serpent") != 1 or (
        metadata_lines.count(f"Version: {app_version}") != 1
    ):
        _fail("identity", "Application wheel metadata does not match the manifest.")
    _assert_media_runtime_identity(
        root,
        by_path,
        singleton_inputs,
        smoke,
    )


def verify_portable_directory(
    root: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> VerifiedPortable:
    try:
        supplied = root.expanduser().absolute()
        root_stat = supplied.lstat()
    except OSError as exc:
        raise VerificationError("missing", "Portable root does not exist.") from exc
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or _is_reparse(root_stat)
        or not stat.S_ISDIR(root_stat.st_mode)
    ):
        _fail("unsafe_root", "Portable root is not a regular directory.")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise VerificationError("missing", "Portable root cannot be resolved.") from exc
    manifest_payload = _regular_bytes(
        resolved / MANIFEST_NAME,
        "portable manifest",
        MAX_MANIFEST_BYTES,
    )
    manifest_sha256 = _sha256_bytes(manifest_payload)
    if expected_manifest_sha256 is not None:
        expected = expected_manifest_sha256.casefold()
        if SHA256_RE.fullmatch(expected) is None:
            _fail("usage", "Expected manifest SHA-256 is invalid.")
        if manifest_sha256 != expected:
            _fail("manifest_hash", "Portable manifest does not match the external trust anchor.")
        authenticity = "anchored-to-expected-manifest-sha256"
    else:
        authenticity = "consistency-only-no-external-trust-anchor"
    decoded = _parse_manifest(manifest_payload)
    (
        app_name,
        app_version,
        members,
        by_path,
        inputs,
        singleton_inputs,
    ) = _strict_manifest(decoded)
    smoke = cast(dict[str, object], decoded["smoke"])
    python_version = cast(str, smoke["python_version"])
    files, directories = _walk_bundle(resolved)
    expected_files = set(by_path) | {MANIFEST_NAME}
    if set(files) != expected_files:
        _fail("inventory", "Bundle has missing or extra files.")
    if directories != _expected_directories(expected_files):
        _fail("inventory", "Bundle has missing or extra directories.")
    total = 0
    for member in members:
        digest = _hash_regular_file(
            files[member["path"]],
            "payload member",
            member["size"],
        )
        if digest != member["sha256"]:
            _fail("member_hash", "A payload member does not match the manifest.")
        total += member["size"]
    payload_object = cast(dict[str, object], decoded["payload"])
    if total != payload_object["total_bytes"]:
        _fail("size_mismatch", "Reopened payload bytes do not match the manifest total.")
    _assert_static_identity(
        resolved,
        app_version,
        by_path,
        inputs,
        singleton_inputs,
        python_version,
        smoke,
    )
    return {
        "schema": VERIFICATION_SCHEMA,
        "ok": True,
        "authenticity": authenticity,
        "manifest_sha256": manifest_sha256,
        "app_name": app_name,
        "app_version": app_version,
        "platform": PLATFORM,
        "member_count": len(members),
        "total_bytes": total,
        "launcher": "groove-serpent.cmd",
        "verifier_launcher": "verify-portable.cmd",
        "python_runtime": "runtime/python.exe",
        "ffmpeg": "tools/ffmpeg.exe",
        "ffprobe": "tools/ffprobe.exe",
        "fingerprint_backend": "ffmpeg-chromaprint",
        "corresponding_source": WINDOWS_MEDIA_SOURCE_PATH,
    }


def _parser() -> argparse.ArgumentParser:
    parser = StrictArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path)
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
                    "schema": VERIFICATION_SCHEMA,
                    "ok": True,
                    "usage": ("verify-portable.cmd [--expected-manifest-sha256 64_HEX_CHARACTERS]"),
                }
            )
            return 0
        if args.root is None:
            _fail("usage", "--root is required.")
        result = verify_portable_directory(
            args.root,
            expected_manifest_sha256=args.expected_manifest_sha256,
        )
        _emit(cast(dict[str, object], result))
        return 0
    except VerificationError as exc:
        _emit(
            {
                "schema": VERIFICATION_SCHEMA,
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            }
        )
        return 2 if exc.code == "usage" else 1
    except Exception:
        _emit(
            {
                "schema": VERIFICATION_SCHEMA,
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "Portable verification failed unexpectedly.",
                },
            }
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
