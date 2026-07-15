"""Run a destructive-but-isolated filesystem acceptance on synthetic audio only.

The default target is a UUID-named child of Groove Serpent's private acceptance
root on ``N:``.  The script never enumerates the acceptance root's siblings and
never opens collection captures or owner project files.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import importlib.metadata as importlib_metadata
import json
import os
import platform
import re
import shutil
import site
import stat
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

import groove_serpent
from groove_serpent import __version__
from groove_serpent.album import (
    AlbumProject,
    AlbumSide,
    pin_album_side,
    save_album_project,
)
from groove_serpent.album_publication_builder import build_album_publication_plan
from groove_serpent.album_publication_durability import (
    inventory_album_publication_orphans,
    recover_album_publication_orphan,
    verify_album_publication,
)
from groove_serpent.album_publication_executor import (
    _capture_execution_lease,
    _directory_identity,
    _estimate_storage,
    _remove_owned_stage,
    execute_album_publication_plan,
    preflight_album_publication_plan,
)
from groove_serpent.atomic_create import probe_atomic_no_replace, rename_no_replace
from groove_serpent.errors import DependencyError, ExportError
from groove_serpent.media import find_tool, probe_audio
from groove_serpent.models import AnalysisSettings, AnalysisSummary, Project, Track
from groove_serpent.portable_names import resolve_portable_path
from groove_serpent.project_io import save_project
from groove_serpent.publication import (
    assert_file_receipt,
    assert_path_receipt,
    capture_file_receipt,
    capture_verified_copy,
)
from groove_serpent.transaction_lock import (
    exclusive_target_write_lease,
    target_lock_path,
)


ACCEPTANCE_SCHEMA = "groove-serpent.n-drive-filesystem-acceptance/2"
ACCEPTANCE_ROOT_ENV = "GROOVE_SERPENT_FILESYSTEM_ACCEPTANCE_ROOT"
_STREAM_CHUNK_BYTES = 1024 * 1024
_DEFAULT_MINIMUM_SOURCE_BYTES = 2 * _STREAM_CHUNK_BYTES
_PROMOTION_DURATION_SECONDS = 16.0
_PROMOTION_MINIMUM_SOURCE_BYTES = _DEFAULT_MINIMUM_SOURCE_BYTES
_PROMOTION_PYTHON_VERSION = "3.13.14"
_PROMOTION_APP_VERSION = "1.0.0"
_ALLOWED_RECORD_HASH_MODES = frozenset({"sha256", "sha384", "sha512"})
_LEASE_CONFLICT_EXIT_CODE = 73
_LEASE_CONFLICT_MARKER = "GROOVE_SERPENT_EXPECTED_WRITE_LEASE_CONFLICT_V1"
_LEASE_CONFLICT_MESSAGE = (
    "Another Groove Serpent process is writing this project; retry after that save finishes."
)
_PYTHON_CHILD_ENVIRONMENT_NAMES = (
    "PYTHONBREAKPOINT",
    "PYTHONDEBUG",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONHASHSEED",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONIOENCODING",
    "PYTHONMALLOC",
    "PYTHONNOUSERSITE",
    "PYTHONOPTIMIZE",
    "PYTHONPATH",
    "PYTHONPROFILEIMPORTTIME",
    "PYTHONPYCACHEPREFIX",
    "PYTHONSAFEPATH",
    "PYTHONSTARTUP",
    "PYTHONTRACEMALLOC",
    "PYTHONUNBUFFERED",
    "PYTHONUSERBASE",
    "PYTHONUTF8",
    "PYTHONWARNDEFAULTENCODING",
    "PYTHONWARNINGS",
)
_DRIVE_TYPES = {
    0: "unknown",
    1: "no-root-directory",
    2: "removable",
    3: "fixed",
    4: "remote",
    5: "optical",
    6: "ramdisk",
}
_VOLUME_FLAGS = {
    "case_sensitive_search": 0x00000001,
    "case_preserved_names": 0x00000002,
    "unicode_on_disk": 0x00000004,
    "persistent_acls": 0x00000008,
    "file_compression": 0x00000010,
    "volume_quotas": 0x00000020,
    "sparse_files": 0x00000040,
    "reparse_points": 0x00000080,
    "object_ids": 0x00010000,
    "encryption": 0x00020000,
    "named_streams": 0x00040000,
    "read_only_volume": 0x00080000,
    "transactions": 0x00200000,
    "hard_links": 0x00400000,
    "extended_attributes": 0x00800000,
    "open_by_file_id": 0x01000000,
    "usn_journal": 0x02000000,
    "integrity_streams": 0x04000000,
    "block_refcounting": 0x08000000,
}


class AcceptanceError(RuntimeError):
    """Raised when a required filesystem acceptance observation fails."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(_absolute(path))))


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_STREAM_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _authority_path(path: Path, repository_root: Path) -> dict[str, str]:
    absolute = path.resolve(strict=True)
    try:
        relative = absolute.relative_to(repository_root).as_posix()
    except ValueError:
        relative = ""
    return {
        "absolute_path": os.fspath(absolute),
        "repository_relative_path": relative,
    }


def _authority_file(path: Path, repository_root: Path) -> dict[str, Any]:
    absolute = path.resolve(strict=True)
    metadata = absolute.lstat()
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if stat.S_ISLNK(metadata.st_mode) or attributes & 0x0400 or not stat.S_ISREG(metadata.st_mode):
        raise AcceptanceError(f"Authority file is not plain and regular: {absolute}")
    return {
        **_authority_path(absolute, repository_root),
        "size_bytes": int(metadata.st_size),
        "modified_ns": int(metadata.st_mtime_ns),
        "sha256": _sha256_path(absolute),
    }


def _authority_tree(root: Path, repository_root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    digest = hashlib.sha256()
    file_count = 0
    total_size_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = path.lstat()
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x0400:
            raise AcceptanceError(f"Authority tree contains a reparse point: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise AcceptanceError(f"Authority tree contains an unsafe entry: {path}")
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        raw = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        file_count += 1
        total_size_bytes += len(raw)
    return {
        **_authority_path(root, repository_root),
        "file_count": file_count,
        "total_size_bytes": total_size_bytes,
        "canonical_tree_sha256": digest.hexdigest(),
    }


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in _PYTHON_CHILD_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUTF8"] = "1"
    return environment


def _uv_check_environment() -> dict[str, str]:
    environment = {
        name: value for name, value in _child_environment().items() if not name.startswith("UV_")
    }
    environment.update(
        {
            "UV_COLOR": "never",
            "UV_NO_PROGRESS": "1",
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    return environment


def _isolated_child_command(code: str, *arguments: str) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-B",
        "-X",
        "utf8",
        "-c",
        code,
        *arguments,
    ]


def _promotion_flags(flags: Any = sys.flags) -> dict[str, int]:
    values = {
        "isolated": int(flags.isolated),
        "dont_write_bytecode": int(flags.dont_write_bytecode),
        "utf8_mode": int(flags.utf8_mode),
    }
    if values != {"isolated": 1, "dont_write_bytecode": 1, "utf8_mode": 1}:
        raise AcceptanceError("Promotion acceptance requires Python flags -I -B -X utf8.")
    return values


def _require_frozen_environment(environment: Mapping[str, str] = os.environ) -> None:
    if environment.get("UV_FROZEN") != "1":
        raise AcceptanceError("Promotion acceptance requires UV_FROZEN=1.")


def _promotion_versions(
    *,
    python_version: str | None = None,
    app_version: str | None = None,
) -> dict[str, str]:
    actual_python = platform.python_version() if python_version is None else python_version
    actual_app = __version__ if app_version is None else app_version
    if actual_python != _PROMOTION_PYTHON_VERSION:
        raise AcceptanceError(
            "Promotion acceptance requires Python "
            f"{_PROMOTION_PYTHON_VERSION}; found {actual_python}."
        )
    if actual_app != _PROMOTION_APP_VERSION:
        raise AcceptanceError(
            "Promotion acceptance requires Groove Serpent "
            f"{_PROMOTION_APP_VERSION}; found {actual_app}."
        )
    return {"python": actual_python, "groove_serpent": actual_app}


def _tool_version(identity: Mapping[str, Any], *, name: str) -> str:
    path = str(identity["absolute_path"])
    command = [path]
    if Path(path).name.casefold().startswith("ffmpeg"):
        command.append("-nostdin")
    command.append("--version" if name.casefold() == "uv" else "-version")
    completed = subprocess.run(
        command,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_child_environment(),
    )
    lines = (completed.stdout or completed.stderr).splitlines()
    return lines[0] if lines else "version unavailable"


def _tool_identity(name: str, repository_root: Path) -> dict[str, Any]:
    try:
        executable = find_tool(name)
    except DependencyError as exc:
        raise AcceptanceError(f"Promotion acceptance requires {name} on PATH.") from exc
    identity = _authority_file(Path(executable), repository_root)
    identity["version"] = _tool_version(identity, name=name)
    return identity


def _safe_installed_record_path(
    environment_root: Path,
    located_path: Path,
    *,
    distribution_name: str,
    record_path: str,
) -> tuple[Path, str]:
    normalized_record = record_path.replace("\\", "/")
    logical = PurePosixPath(normalized_record)
    if not logical.parts or logical.is_absolute() or any(":" in part for part in logical.parts):
        raise AcceptanceError(
            f"Installed distribution {distribution_name} has an unsafe RECORD path."
        )
    environment_root = environment_root.resolve(strict=True)
    candidate = _absolute(located_path)
    try:
        environment_relative = candidate.relative_to(environment_root)
    except ValueError as exc:
        raise AcceptanceError(
            f"Installed distribution {distribution_name} has a RECORD path outside .venv."
        ) from exc
    if not environment_relative.parts:
        raise AcceptanceError(
            f"Installed distribution {distribution_name} RECORD names the .venv root."
        )
    cursor = environment_root
    try:
        for index, part in enumerate(environment_relative.parts):
            cursor /= part
            metadata = cursor.lstat()
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode) or attributes & 0x0400:
                raise AcceptanceError(
                    f"Installed distribution {distribution_name} RECORD crosses a reparse point."
                )
            is_last = index == len(environment_relative.parts) - 1
            if is_last and not stat.S_ISREG(metadata.st_mode):
                raise AcceptanceError(
                    f"Installed distribution {distribution_name} RECORD entry is not regular."
                )
            if not is_last and not stat.S_ISDIR(metadata.st_mode):
                raise AcceptanceError(
                    f"Installed distribution {distribution_name} RECORD parent is not a directory."
                )
    except FileNotFoundError as exc:
        raise AcceptanceError(
            f"Installed distribution {distribution_name} RECORD entry is missing."
        ) from exc
    resolved = candidate.resolve(strict=True)
    if _path_key(resolved) != _path_key(candidate):
        raise AcceptanceError(
            f"Installed distribution {distribution_name} RECORD path changed on resolution."
        )
    return resolved, resolved.relative_to(environment_root).as_posix()


def _hash_installed_record_file(
    path: Path,
    *,
    record_hash_mode: str | None,
    record_hash_value: str | None,
    record_size: int | None,
) -> dict[str, Any]:
    if (record_hash_mode is None) != (record_hash_value is None):
        raise AcceptanceError("Installed RECORD contains an incomplete hash declaration.")
    normalized_mode = record_hash_mode.casefold() if record_hash_mode is not None else None
    if normalized_mode is not None and normalized_mode not in _ALLOWED_RECORD_HASH_MODES:
        raise AcceptanceError(
            f"Installed RECORD uses unsupported hash algorithm {record_hash_mode}."
        )
    sha256 = hashlib.sha256()
    declared_hasher: Any | None = None
    if normalized_mode is not None and normalized_mode != "sha256":
        declared_hasher = hashlib.new(normalized_mode)
    size_bytes = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_STREAM_CHUNK_BYTES), b""):
            size_bytes += len(chunk)
            sha256.update(chunk)
            if declared_hasher is not None:
                declared_hasher.update(chunk)
    sha256_digest = sha256.digest()
    if normalized_mode is not None:
        if normalized_mode == "sha256":
            declared_digest = sha256_digest
        else:
            assert declared_hasher is not None
            declared_digest = declared_hasher.digest()
        actual_record_hash = base64.urlsafe_b64encode(declared_digest).rstrip(b"=").decode("ascii")
        if actual_record_hash != record_hash_value:
            raise AcceptanceError("Installed file bytes do not match their RECORD hash.")
    if record_size is not None and (record_size < 0 or record_size != size_bytes):
        raise AcceptanceError("Installed file size does not match its RECORD declaration.")
    return {
        "size_bytes": size_bytes,
        "sha256": sha256_digest.hex(),
        "record_hash_verified": normalized_mode is not None,
        "record_size_verified": record_size is not None,
    }


def _installed_distribution_inventory(repository_root: Path) -> dict[str, Any]:
    environment_root = Path(sys.prefix).resolve(strict=True)
    search_roots = sorted(
        {Path(path).resolve(strict=True) for path in site.getsitepackages()},
        key=os.fspath,
    )
    for search_root in search_roots:
        try:
            search_root.relative_to(environment_root)
        except ValueError as exc:
            raise AcceptanceError(
                "An installed-distribution search root is outside the canonical .venv."
            ) from exc
    packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_record_file_count = 0
    total_record_size_bytes = 0
    for distribution in importlib_metadata.distributions(
        path=[os.fspath(path) for path in search_roots]
    ):
        raw_name = distribution.metadata["Name"]
        if not raw_name:
            raise AcceptanceError("An installed distribution lacks its canonical name.")
        name = re.sub(r"[-_.]+", "-", raw_name).casefold()
        if name in seen:
            raise AcceptanceError(f"Installed distribution inventory repeats {name}.")
        seen.add(name)
        location = Path(str(distribution.locate_file(""))).resolve(strict=True)
        try:
            location.relative_to(environment_root)
        except ValueError as exc:
            raise AcceptanceError(
                f"Installed distribution {name} resolves outside the canonical .venv."
            ) from exc
        record_files = distribution.files
        if not record_files:
            raise AcceptanceError(f"Installed distribution {name} has no RECORD inventory.")
        files_digest = hashlib.sha256()
        seen_record_paths: set[str] = set()
        record_entry_present = False
        record_total_size_bytes = 0
        record_hashes_verified = 0
        record_sizes_verified = 0
        for record_file in sorted(record_files, key=str):
            raw_record_path = str(record_file)
            installed_path, environment_relative = _safe_installed_record_path(
                environment_root,
                Path(str(distribution.locate_file(record_file))),
                distribution_name=name,
                record_path=raw_record_path,
            )
            record_key = os.path.normcase(environment_relative)
            if record_key in seen_record_paths:
                raise AcceptanceError(f"Installed distribution {name} repeats a RECORD path.")
            seen_record_paths.add(record_key)
            declared_hash = record_file.hash
            hashed = _hash_installed_record_file(
                installed_path,
                record_hash_mode=(declared_hash.mode if declared_hash is not None else None),
                record_hash_value=(declared_hash.value if declared_hash is not None else None),
                record_size=record_file.size,
            )
            relative_bytes = environment_relative.encode("utf-8")
            files_digest.update(len(relative_bytes).to_bytes(8, "big"))
            files_digest.update(relative_bytes)
            files_digest.update(int(hashed["size_bytes"]).to_bytes(8, "big"))
            files_digest.update(bytes.fromhex(str(hashed["sha256"])))
            record_total_size_bytes += int(hashed["size_bytes"])
            record_hashes_verified += int(bool(hashed["record_hash_verified"]))
            record_sizes_verified += int(bool(hashed["record_size_verified"]))
            if raw_record_path.replace("\\", "/").casefold().endswith(".dist-info/record"):
                record_entry_present = True
        if not record_entry_present:
            raise AcceptanceError(f"Installed distribution {name} does not inventory RECORD.")
        record_file_count = len(seen_record_paths)
        total_record_file_count += record_file_count
        total_record_size_bytes += record_total_size_bytes
        packages.append(
            {
                "name": name,
                "version": distribution.version,
                "location": _authority_path(location, repository_root),
                "record_file_count": record_file_count,
                "record_total_size_bytes": record_total_size_bytes,
                "record_files_sha256": files_digest.hexdigest(),
                "record_hashes_verified": record_hashes_verified,
                "record_hashes_omitted": record_file_count - record_hashes_verified,
                "record_sizes_verified": record_sizes_verified,
                "record_sizes_omitted": record_file_count - record_sizes_verified,
                "record_entry_present": True,
            }
        )
    packages.sort(key=lambda item: str(item["name"]))
    canonical = json.dumps(
        packages,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "environment": _authority_path(environment_root, repository_root),
        "search_roots": [_authority_path(path, repository_root) for path in search_roots],
        "package_count": len(packages),
        "record_file_count": total_record_file_count,
        "record_total_size_bytes": total_record_size_bytes,
        "record_bytes_bound": True,
        "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        "packages": packages,
    }


def _uv_sync_check(
    identity: Mapping[str, Any],
    repository_root: Path,
    interpreter: Path,
    environment_root: Path,
) -> dict[str, Any]:
    arguments = [
        "--no-config",
        "sync",
        "--check",
        "--locked",
        "--python",
        os.fspath(interpreter),
        "--offline",
        "--no-progress",
        "--color",
        "never",
    ]
    completed = subprocess.run(
        [str(identity["absolute_path"]), *arguments],
        cwd=repository_root,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=_uv_check_environment(),
        timeout=120,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    resolved = re.search(r"(?m)^Resolved (\d+) packages?\b", output)
    checked = re.search(r"(?m)^Checked (\d+) packages?\b", output)
    reported_environment = re.search(
        r"(?m)^Would use project environment at: (.+?)\s*$",
        output,
    )
    if (
        completed.returncode != 0
        or resolved is None
        or checked is None
        or reported_environment is None
        or "Would make no changes" not in output.splitlines()
    ):
        diagnostic = output.strip()[-2000:]
        raise AcceptanceError(
            f"The canonical .venv is not synchronized with the locked project: {diagnostic}"
        )
    reported_path = Path(reported_environment.group(1).strip())
    if not reported_path.is_absolute():
        reported_path = repository_root / reported_path
    try:
        reported_path = reported_path.resolve(strict=True)
    except OSError as exc:
        raise AcceptanceError("uv reported an environment path that does not exist.") from exc
    if reported_path != environment_root.resolve(strict=True):
        raise AcceptanceError("uv checked an environment other than the canonical .venv.")
    return {
        "passed": True,
        "arguments": arguments,
        "repository_cwd": _authority_path(repository_root, repository_root),
        "reported_environment": _authority_path(reported_path, repository_root),
        "environment_synchronized": True,
        "would_make_no_changes": True,
        "resolved_package_count": int(resolved.group(1)),
        "checked_package_count": int(checked.group(1)),
        "uv_environment_sanitized": True,
    }


def _crosscheck_uv_distribution_counts(
    installed_distributions: Mapping[str, Any],
    uv_sync_check: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        package_count = int(installed_distributions["package_count"])
        resolved_count = int(uv_sync_check["resolved_package_count"])
        checked_count = int(uv_sync_check["checked_package_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AcceptanceError("Distribution count evidence is incomplete.") from exc
    if package_count <= 0 or resolved_count != package_count or checked_count != package_count:
        raise AcceptanceError(
            "uv package counts do not match the byte-bound installed-distribution inventory."
        )
    bound = dict(uv_sync_check)
    bound["installed_distribution_count_matches"] = True
    return bound


def _isolated_child_authority(
    repository_root: Path,
    expected_package_file: Path,
) -> dict[str, Any]:
    code = (
        "import json,sys,groove_serpent;"
        "print(json.dumps({'executable':sys.executable,"
        "'package_file':groove_serpent.__file__,"
        "'version':groove_serpent.__version__,"
        "'flags':{'isolated':sys.flags.isolated,"
        "'dont_write_bytecode':sys.flags.dont_write_bytecode,"
        "'utf8_mode':sys.flags.utf8_mode}}))"
    )
    completed = subprocess.run(
        _isolated_child_command(code),
        cwd=repository_root,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=_child_environment(),
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise AcceptanceError("Isolated child authority probe was not a JSON object.")
    child_executable = Path(str(value.get("executable"))).resolve()
    child_package = Path(str(value.get("package_file"))).resolve()
    if child_executable != Path(sys.executable).resolve():
        raise AcceptanceError("Isolated child used a different interpreter.")
    if child_package != expected_package_file or value.get("version") != __version__:
        raise AcceptanceError("Isolated child imported a non-canonical package.")
    if value.get("flags") != {
        "isolated": 1,
        "dont_write_bytecode": 1,
        "utf8_mode": 1,
    }:
        raise AcceptanceError("Isolated child did not retain required Python flags.")
    return {
        "interpreter": _authority_path(child_executable, repository_root),
        "package": _authority_path(child_package, repository_root),
        "version": str(value["version"]),
        "flags": value["flags"],
        "python_environment_sanitized": True,
    }


def _capture_promotion_authority() -> dict[str, Any]:
    versions = _promotion_versions()
    flags = _promotion_flags()
    _require_frozen_environment()
    generator = Path(__file__).resolve(strict=True)
    repository_root = generator.parents[1]
    expected_generator = repository_root / "scripts" / "accept_n_drive_filesystem.py"
    if generator != expected_generator:
        raise AcceptanceError("Promotion generator is outside the repository scripts folder.")
    if Path.cwd().resolve() != repository_root:
        raise AcceptanceError("Promotion acceptance must launch from the source root.")
    if not sys.argv or Path(sys.argv[0]).resolve() != generator:
        raise AcceptanceError("Promotion argv does not name the canonical generator.")

    package_file_value = groove_serpent.__file__
    if package_file_value is None:
        raise AcceptanceError("Canonical Groove Serpent package has no source path.")
    package_file = Path(package_file_value).resolve(strict=True)
    package_root = package_file.parent
    expected_package_file = repository_root / "src" / "groove_serpent" / "__init__.py"
    if package_file != expected_package_file:
        raise AcceptanceError("Promotion acceptance imported a non-canonical package.")

    interpreter = Path(sys.executable).resolve(strict=True)
    environment_root = (repository_root / ".venv").resolve(strict=True)
    if Path(sys.prefix).resolve() != environment_root:
        raise AcceptanceError("Promotion interpreter prefix is not the canonical .venv.")
    try:
        interpreter.relative_to(environment_root)
    except ValueError as exc:
        raise AcceptanceError("Promotion interpreter is outside the canonical .venv.") from exc
    base_executable = Path(str(getattr(sys, "_base_executable", sys.executable))).resolve(
        strict=True
    )
    repository_identity = _directory_identity(
        repository_root,
        label="Promotion repository root",
    )
    uv_identity = _tool_identity("uv", repository_root)
    installed_distributions = _installed_distribution_inventory(repository_root)
    uv_sync_check = _crosscheck_uv_distribution_counts(
        installed_distributions,
        _uv_sync_check(
            uv_identity,
            repository_root,
            interpreter,
            environment_root,
        ),
    )
    return {
        "schema": "groove-serpent.n-drive-filesystem-authority/1",
        "promotion_enforced": True,
        "repository": {
            **_authority_path(repository_root, repository_root),
            "identity": asdict(repository_identity),
        },
        "cwd": _authority_path(Path.cwd(), repository_root),
        "argv": {
            "entrypoint": _authority_path(generator, repository_root),
            "arguments": list(sys.argv[1:]),
        },
        "flags": flags,
        "versions": versions,
        "uv_frozen_environment_marker": True,
        "generator": _authority_file(generator, repository_root),
        "pyproject": _authority_file(repository_root / "pyproject.toml", repository_root),
        "uv_lock": _authority_file(repository_root / "uv.lock", repository_root),
        "interpreter": _authority_file(interpreter, repository_root),
        "base_executable": _authority_file(base_executable, repository_root),
        "pyvenv_config": _authority_file(
            environment_root / "pyvenv.cfg",
            repository_root,
        ),
        "python_version": versions["python"],
        "package_entrypoint": _authority_file(package_file, repository_root),
        "runtime_package_tree": _authority_tree(package_root, repository_root),
        "installed_distributions": installed_distributions,
        "uv_sync_check": uv_sync_check,
        "isolated_child": _isolated_child_authority(
            repository_root,
            expected_package_file,
        ),
        "tools": {
            "ffmpeg": _tool_identity("ffmpeg", repository_root),
            "ffprobe": _tool_identity("ffprobe", repository_root),
            "uv": uv_identity,
        },
    }


def _finalize_promotion_authority(
    expected: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if expected is None:
        return {
            "schema": "groove-serpent.n-drive-filesystem-authority/1",
            "promotion_enforced": False,
            "rechecked_at_end": False,
        }
    current = _capture_promotion_authority()
    if current != expected:
        raise AcceptanceError("Promotion execution authority changed during acceptance.")
    raw_receipt: object = json.loads(json.dumps(expected, ensure_ascii=True, allow_nan=False))
    if not isinstance(raw_receipt, dict):
        raise AcceptanceError("Promotion authority could not be copied as a JSON object.")
    receipt: dict[str, Any] = {}
    for key, value in raw_receipt.items():
        if not isinstance(key, str):
            raise AcceptanceError("Promotion authority contains a non-string key.")
        receipt[key] = value
    receipt["rechecked_at_end"] = True
    return receipt


def _promotion_tool_path(authority: Mapping[str, Any], name: str) -> str:
    tools = authority.get("tools")
    if not isinstance(tools, Mapping):
        raise AcceptanceError("Promotion authority lacks its tool identities.")
    identity = tools.get(name)
    if not isinstance(identity, Mapping):
        raise AcceptanceError(f"Promotion authority lacks the {name} identity.")
    raw_path = identity.get("absolute_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise AcceptanceError(f"Promotion authority lacks the {name} executable path.")
    path = Path(raw_path).resolve(strict=True)
    if not path.is_file():
        raise AcceptanceError(f"Promotion {name} executable is not a regular file.")
    return os.fspath(path)


def validate_standard_acceptance_root(path: Path) -> Path:
    """Require the exact private root configured by the caller's environment."""

    absolute = _absolute(path)
    expected = configured_acceptance_root()
    if _path_key(absolute) != _path_key(expected):
        raise AcceptanceError(
            f"The candidate acceptance root must be exactly {expected}; refusing {absolute}."
        )
    if os.name != "nt" or absolute.drive.casefold() != "n:":
        raise AcceptanceError("The standard promotion acceptance root must be on N:.")
    if _path_key(absolute) == _path_key(Path(absolute.anchor)):
        raise AcceptanceError("The standard promotion acceptance root cannot be the N: root.")
    return absolute


def configured_acceptance_root() -> Path:
    """Load the private target without embedding an owner-specific path in source."""

    raw = os.environ.get(ACCEPTANCE_ROOT_ENV)
    if raw is None or not raw.strip():
        raise AcceptanceError(f"Set {ACCEPTANCE_ROOT_ENV} to the isolated acceptance root.")
    return _absolute(Path(raw))


def _validate_promotion_workload(
    target_root: Path,
    *,
    duration_seconds: float,
    minimum_source_bytes: int,
    enforce_standard_root: bool,
    keep_workdir: bool,
) -> Path:
    if not enforce_standard_root:
        raise AcceptanceError("Promotion acceptance cannot allow a nonstandard root.")
    if keep_workdir:
        raise AcceptanceError("Promotion acceptance must clean its owned work directory.")
    if duration_seconds != _PROMOTION_DURATION_SECONDS:
        raise AcceptanceError(
            "Promotion acceptance requires exactly "
            f"{_PROMOTION_DURATION_SECONDS} seconds per synthetic side."
        )
    if minimum_source_bytes != _PROMOTION_MINIMUM_SOURCE_BYTES:
        raise AcceptanceError(
            "Promotion acceptance requires the exact minimum synthetic source size of "
            f"{_PROMOTION_MINIMUM_SOURCE_BYTES} bytes."
        )
    return validate_standard_acceptance_root(target_root)


def _prepare_root(path: Path) -> Path:
    resolution = resolve_portable_path(path / ".root-sentinel", create_parents=True)
    root = resolution.path.parent
    if not root.is_dir():
        raise AcceptanceError("The acceptance root could not be created as a directory.")
    return root


def _new_run_directory(root: Path) -> Path:
    name = f"n-drive-{uuid.uuid4().hex}"
    resolution = resolve_portable_path(root / name / ".run-sentinel", create_parents=True)
    run = resolution.path.parent
    if not run.is_dir() or not run.parent.samefile(root):
        raise AcceptanceError("The isolated acceptance directory escaped its approved root.")
    return run


def _file_receipt_payload(path: Path, *, label: str) -> dict[str, Any]:
    return asdict(capture_file_receipt(path, label=label))


def _tree_receipt(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    total_size = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        details = path.lstat()
        attributes = int(getattr(details, "st_file_attributes", 0))
        if stat.S_ISLNK(details.st_mode) or attributes & 0x0400:
            raise AcceptanceError("A publication tree unexpectedly contains a reparse point.")
        if stat.S_ISDIR(details.st_mode):
            continue
        if not stat.S_ISREG(details.st_mode):
            raise AcceptanceError("A publication tree contains an unsupported file type.")
        receipt = capture_file_receipt(path, label="Synthetic publication artifact")
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(receipt.sha256.encode("ascii"))
        digest.update(b"\n")
        file_count += 1
        total_size += receipt.size_bytes
    return {
        "file_count": file_count,
        "total_size_bytes": total_size,
        "canonical_tree_sha256": digest.hexdigest(),
    }


def _windows_volume(path: Path) -> dict[str, Any]:
    loader: Any = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise AcceptanceError("Windows volume inspection is unavailable.")
    kernel32: Any = loader("kernel32", use_last_error=True)
    volume_root = ctypes.create_unicode_buffer(32768)
    if not kernel32.GetVolumePathNameW(os.fspath(path), volume_root, len(volume_root)):
        raise ctypes.WinError(ctypes.get_last_error())
    label = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32()
    maximum_component = ctypes.c_uint32()
    flags = ctypes.c_uint32()
    if not kernel32.GetVolumeInformationW(
        volume_root.value,
        label,
        len(label),
        ctypes.byref(serial),
        ctypes.byref(maximum_component),
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    sectors_per_cluster = ctypes.c_uint32()
    bytes_per_sector = ctypes.c_uint32()
    free_clusters = ctypes.c_uint32()
    total_clusters = ctypes.c_uint32()
    if not kernel32.GetDiskFreeSpaceW(
        volume_root.value,
        ctypes.byref(sectors_per_cluster),
        ctypes.byref(bytes_per_sector),
        ctypes.byref(free_clusters),
        ctypes.byref(total_clusters),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    drive_type = int(kernel32.GetDriveTypeW(volume_root.value))
    raw_flags = int(flags.value)
    usage = shutil.disk_usage(path)
    return {
        "volume_root": volume_root.value,
        "filesystem": filesystem.value,
        "label": label.value,
        "drive_type": _DRIVE_TYPES.get(drive_type, f"unknown-{drive_type}"),
        "maximum_component_length": int(maximum_component.value),
        "allocation_unit_bytes": (int(sectors_per_cluster.value) * int(bytes_per_sector.value)),
        "total_bytes": usage.total,
        "free_bytes_at_start": usage.free,
        "volume_flags_hex": f"0x{raw_flags:08x}",
        "declared_volume_features": {
            name: bool(raw_flags & mask) for name, mask in _VOLUME_FLAGS.items()
        },
    }


def _volume_details(path: Path) -> dict[str, Any]:
    if os.name == "nt":
        return _windows_volume(path)
    usage = shutil.disk_usage(path)
    return {
        "volume_root": path.anchor,
        "filesystem": "not exposed by portable Python",
        "label": "",
        "drive_type": "unknown",
        "maximum_component_length": None,
        "allocation_unit_bytes": None,
        "total_bytes": usage.total,
        "free_bytes_at_start": usage.free,
        "volume_flags_hex": None,
        "declared_volume_features": {},
    }


def _ffmpeg_path() -> str:
    try:
        value = find_tool("ffmpeg")
        find_tool("ffprobe")
    except DependencyError as exc:
        raise AcceptanceError(
            "FFmpeg and FFprobe are required for the synthetic acceptance fixture."
        ) from exc
    return value


def _create_synthetic_flac(
    path: Path,
    *,
    duration_seconds: float,
    seed: int,
    ffmpeg_path: str | None = None,
) -> None:
    ffmpeg = ffmpeg_path or _ffmpeg_path()
    first = f"anoisesrc=color=pink:sample_rate=48000:duration={duration_seconds}:seed={seed}"
    second = f"anoisesrc=color=white:sample_rate=48000:duration={duration_seconds}:seed={seed + 1}"
    completed = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-filter_complex_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            first,
            "-f",
            "lavfi",
            "-i",
            second,
            "-filter_complex",
            "[0:a][1:a]amerge=inputs=2,volume=0.12[out]",
            "-map",
            "[out]",
            "-map_metadata",
            "-1",
            "-c:a",
            "flac",
            "-compression_level",
            "5",
            "-sample_fmt",
            "s16",
            "-bitexact",
            os.fspath(path),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()[-1000:]
        raise AcceptanceError(f"Synthetic FFmpeg generation failed: {message}")


def _track_ranges(sample_count: int) -> tuple[tuple[int, int], ...]:
    boundaries = (0, sample_count // 3, (sample_count * 2) // 3, sample_count)
    return tuple(zip(boundaries, boundaries[1:]))


def _write_synthetic_album(
    root: Path,
    *,
    duration_seconds: float,
    minimum_source_bytes: int,
    ffmpeg_path: str | None = None,
) -> tuple[Path, tuple[Path, ...], tuple[dict[str, Any], ...]]:
    root.mkdir()
    project_paths: list[Path] = []
    source_paths: list[Path] = []
    source_receipts: list[dict[str, Any]] = []
    sides: list[AlbumSide] = []
    for order, label in enumerate(("A", "B"), start=1):
        source_path = root / f"side-{label.casefold()}-synthetic.flac"
        _create_synthetic_flac(
            source_path,
            duration_seconds=duration_seconds,
            seed=10_000 + order * 100,
            ffmpeg_path=ffmpeg_path,
        )
        source = probe_audio(source_path, stored_path=source_path.name)
        if source.size_bytes < minimum_source_bytes:
            raise AcceptanceError(
                f"Synthetic side {label} is only {source.size_bytes} bytes; expected at "
                f"least {minimum_source_bytes} to cross streaming chunks."
            )
        if source.sample_count is None:
            raise AcceptanceError(f"Synthetic side {label} lacks an exact sample count.")
        sample_count = source.sample_count
        ranges = _track_ranges(sample_count)
        tracks = [
            Track(
                number=index,
                title=f"Synthetic {label}{index}",
                start_sample=start,
                end_sample=end,
                start_seconds=start / source.sample_rate,
                end_seconds=end / source.sample_rate,
                side=label,
            )
            for index, (start, end) in enumerate(ranges, start=1)
        ]
        project = Project(
            source=source,
            settings=AnalysisSettings(min_track_seconds=0.05),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=sample_count / source.sample_rate,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=tracks,
            metadata={"artist": "Synthetic Acceptance", "album": "N Drive Fixture"},
        )
        project_path = root / f"side-{label.casefold()}.groove.json"
        save_project(project, project_path)
        side = AlbumSide(label, order, project_path.name)
        album_path = root / "synthetic-album.groove-album.json"
        pin_album_side(side, album_path)
        sides.append(side)
        project_paths.append(project_path)
        source_paths.append(source_path)
        source_receipts.append(_file_receipt_payload(source_path, label=f"Synthetic side {label}"))
    album_path = root / "synthetic-album.groove-album.json"
    save_album_project(
        AlbumProject(
            metadata={"artist": "Synthetic Acceptance", "album": "N Drive Fixture"},
            sides=sides,
        ),
        album_path,
    )
    plan_path = root / "synthetic.publication-plan.json"
    build_album_publication_plan(
        album_path,
        plan_path,
        selected_profiles=("archival-source", "corrected-lossless", "portable"),
        restoration_mode="none",
    )
    return plan_path, tuple(source_paths), tuple(source_receipts)


def _accept_identity_snapshot(root: Path, source: Path) -> dict[str, Any]:
    root.mkdir()
    snapshot = root / "side-a-verified-snapshot.flac"
    expected = capture_file_receipt(source, label="Synthetic snapshot source")
    captured = capture_verified_copy(
        source,
        snapshot,
        label="Synthetic snapshot source",
        expected_sha256=expected.sha256,
        expected_size_bytes=expected.size_bytes,
    )
    assert_file_receipt(source, expected, label="Synthetic snapshot source")
    assert_path_receipt(
        source,
        captured.source_path_receipt,
        label="Synthetic snapshot source",
    )
    assert_path_receipt(
        snapshot,
        captured.snapshot_path_receipt,
        label="Synthetic verified snapshot",
    )
    if captured.source_receipt.sha256 != captured.snapshot_receipt.sha256:
        raise AcceptanceError("Verified snapshot SHA-256 differs from its synthetic source.")
    return {
        "passed": True,
        "stream_chunk_bytes": _STREAM_CHUNK_BYTES,
        "stream_chunk_count_floor": expected.size_bytes // _STREAM_CHUNK_BYTES,
        "source_receipt": asdict(captured.source_receipt),
        "snapshot_receipt": asdict(captured.snapshot_receipt),
        "path_receipts_reasserted": True,
    }


def _accept_atomic_no_replace(root: Path) -> dict[str, Any]:
    root.mkdir()
    exercised = probe_atomic_no_replace(root)
    if not exercised.samefile(root):
        raise AcceptanceError("Atomic no-replace probe exercised a different directory.")
    first = root / "atomic-first.tmp"
    destination = root / "atomic-published.bin"
    contender = root / "atomic-contender.tmp"
    first.write_bytes(b"first atomic payload\n")
    contender.write_bytes(b"contender must remain\n")
    rename_no_replace(first, destination)
    before = capture_file_receipt(destination, label="Atomic published file")
    rejected = False
    try:
        rename_no_replace(contender, destination)
    except FileExistsError:
        rejected = True
    if not rejected or not contender.is_file():
        raise AcceptanceError("Atomic no-replace did not preserve both existing objects.")
    assert_file_receipt(destination, before, label="Atomic published file")
    return {
        "passed": True,
        "probe_directory": root.name,
        "first_publish_sha256": before.sha256,
        "existing_destination_rejected": rejected,
        "contender_preserved": contender.is_file(),
    }


def _validate_lease_conflict_result(
    completed: subprocess.CompletedProcess[str],
) -> None:
    expected_stdout = [_LEASE_CONFLICT_MARKER, _LEASE_CONFLICT_MESSAGE]
    if (
        completed.returncode != _LEASE_CONFLICT_EXIT_CODE
        or completed.stdout.splitlines() != expected_stdout
        or completed.stderr != ""
    ):
        raise AcceptanceError(
            "A competing native process did not produce the exact expected write-lease "
            "conflict evidence."
        )


def _accept_write_lease(root: Path) -> dict[str, Any]:
    root.mkdir()
    target = root / "synthetic-mutable-target.json"
    repository_root = Path(__file__).resolve(strict=True).parents[1]
    worker = (
        "import sys\n"
        "from pathlib import Path\n"
        "from groove_serpent.errors import ProjectValidationError\n"
        "from groove_serpent.transaction_lock import exclusive_target_write_lease\n"
        "expected=sys.argv[2]\n"
        "marker=sys.argv[3]\n"
        "try:\n"
        "    with exclusive_target_write_lease(Path(sys.argv[1]), timeout_seconds=0.05):\n"
        "        pass\n"
        "except ProjectValidationError as exc:\n"
        "    message=str(exc)\n"
        "    if message != expected:\n"
        "        print(f'UNEXPECTED_PROJECT_VALIDATION_ERROR:{message}')\n"
        "        raise SystemExit(74)\n"
        "    print(marker)\n"
        "    print(message)\n"
        f"    raise SystemExit({_LEASE_CONFLICT_EXIT_CODE})\n"
        "raise SystemExit(0)\n"
    )
    with exclusive_target_write_lease(target, timeout_seconds=2.0) as lease:
        lease.assert_current()
        with target.open("xb") as handle:
            handle.write(b'{"synthetic":true}\n')
            handle.flush()
            os.fsync(handle.fileno())
        child = subprocess.run(
            _isolated_child_command(
                worker,
                os.fspath(target),
                _LEASE_CONFLICT_MESSAGE,
                _LEASE_CONFLICT_MARKER,
            ),
            cwd=repository_root,
            env=_child_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
        lease.assert_current()
    _validate_lease_conflict_result(child)
    with exclusive_target_write_lease(target, timeout_seconds=2.0) as repeated:
        repeated.assert_current()
    lock = target_lock_path(target)
    return {
        "passed": True,
        "native_process_conflict_rejected": True,
        "conflict_exit_code": child.returncode,
        "conflict_marker": _LEASE_CONFLICT_MARKER,
        "conflict_message": _LEASE_CONFLICT_MESSAGE,
        "worker_python_flags": ["-I", "-B", "-X", "utf8"],
        "worker_repository_cwd": os.fspath(repository_root),
        "worker_python_environment_sanitized": True,
        "reacquired_after_release": True,
        "target_receipt": _file_receipt_payload(target, label="Synthetic lease target"),
        "lock_receipt": _file_receipt_payload(lock, label="Synthetic write lock"),
    }


def _kill_publication(plan_path: Path, output: Path, boundary: str) -> int:
    repository_root = Path(__file__).resolve(strict=True).parents[1]
    worker = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from groove_serpent.album_publication_executor import "
        "execute_album_publication_plan\n"
        "boundary=sys.argv[3]\n"
        "def kill(value):\n"
        "    if value == boundary:\n"
        "        os._exit(77)\n"
        "execute_album_publication_plan(Path(sys.argv[1]), Path(sys.argv[2]), "
        "fault_injector=kill)\n"
    )
    completed = subprocess.run(
        _isolated_child_command(
            worker,
            os.fspath(plan_path),
            os.fspath(output),
            boundary,
        ),
        cwd=repository_root,
        env=_child_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=180,
    )
    return completed.returncode


def _accept_interrupted_recovery(root: Path, plan_path: Path) -> dict[str, Any]:
    root.mkdir()
    intended = root / "must-not-exist-after-kill"
    returncode = _kill_publication(plan_path, intended, "after-journal-staging")
    if returncode != 77 or intended.exists():
        raise AcceptanceError("Hard-exit injection did not leave the expected safe partial state.")
    inventory = inventory_album_publication_orphans(root)
    owned = [
        item
        for item in inventory.orphans
        if item.owned and item.intended_output_name == intended.name
    ]
    if len(owned) != 1 or inventory.truncated:
        raise AcceptanceError("Interrupted publication did not yield one bounded owned orphan.")
    orphan = owned[0]
    if orphan.directory_identity is None or orphan.journal_sha256 is None:
        raise AcceptanceError("Owned orphan lacks an explicit recovery identity receipt.")
    quarantined = recover_album_publication_orphan(
        Path(orphan.path),
        expected_identity=orphan.directory_identity,
        expected_journal_sha256=orphan.journal_sha256,
        action="quarantine",
    )
    if quarantined.resulting_path is None:
        raise AcceptanceError("Owned orphan was not moved to an exclusive quarantine name.")
    quarantine_inventory = inventory_album_publication_orphans(root)
    exact = [
        item
        for item in quarantine_inventory.orphans
        if item.path == quarantined.resulting_path and item.owned
    ]
    if len(exact) != 1:
        raise AcceptanceError("Quarantined orphan could not be independently reinventoried.")
    quarantine = exact[0]
    if quarantine.directory_identity is None or quarantine.journal_sha256 is None:
        raise AcceptanceError("Quarantine lacks an exact removal receipt.")
    removed = recover_album_publication_orphan(
        Path(quarantine.path),
        expected_identity=quarantine.directory_identity,
        expected_journal_sha256=quarantine.journal_sha256,
        action="remove",
    )
    final_inventory = inventory_album_publication_orphans(root)
    if not removed.removed or final_inventory.orphans:
        raise AcceptanceError("Receipted quarantine removal left an owned operation orphan.")
    return {
        "passed": True,
        "fault_boundary": "after-journal-staging",
        "hard_exit_code": returncode,
        "initial_orphan": {
            "kind": orphan.kind,
            "state": orphan.state,
            "plan_sha256": orphan.plan_sha256,
            "journal_sha256": orphan.journal_sha256,
            "file_count": orphan.file_count,
            "total_size_bytes": orphan.total_size_bytes,
            "directory_identity": asdict(orphan.directory_identity),
        },
        "quarantine_name_pattern_verified": Path(quarantined.resulting_path).name.startswith(
            ".groove-serpent-album-cleanup-"
        ),
        "quarantine_reinventoried": True,
        "receipted_removal_verified": removed.removed,
        "final_orphan_count": len(final_inventory.orphans),
    }


def _accept_publication(root: Path, plan_path: Path) -> dict[str, Any]:
    root.mkdir()
    output = root / "verified synthetic album"
    lease = _capture_execution_lease(plan_path, None)
    estimated = _estimate_storage(lease)
    free_before = shutil.disk_usage(root).free
    if estimated <= 0 or estimated >= free_before:
        raise AcceptanceError("Production space accounting did not fit the acceptance volume.")
    preflight = preflight_album_publication_plan(plan_path)
    execution = execute_album_publication_plan(plan_path, output)
    before_verify = _tree_receipt(output)
    verification = verify_album_publication(output)
    after_verify = _tree_receipt(output)
    if not verification.ok or before_verify != after_verify:
        raise AcceptanceError("Strict publication verification failed or mutated the output.")
    no_overwrite_rejected = False
    try:
        execute_album_publication_plan(plan_path, output)
    except ExportError:
        no_overwrite_rejected = True
    after_collision = _tree_receipt(output)
    if not no_overwrite_rejected or after_collision != before_verify:
        raise AcceptanceError("Existing publication output was not preserved byte-for-byte.")
    manifest = json.loads(Path(execution.manifest_path).read_text(encoding="utf-8"))
    archival_sources = manifest.get("archival_sources")
    if not isinstance(archival_sources, dict):
        raise AcceptanceError("Publication manifest lacks its source-object ledger.")
    source_objects = archival_sources.get("objects")
    side_bindings = archival_sources.get("side_bindings")
    if not isinstance(source_objects, list) or len(source_objects) != 2:
        raise AcceptanceError("Distinct synthetic sides did not publish two source objects.")
    if not isinstance(side_bindings, list) or len(side_bindings) != 2:
        raise AcceptanceError("Synthetic source-object ledger did not bind both album sides.")
    return {
        "passed": True,
        "preflight": asdict(preflight),
        "estimated_required_bytes": estimated,
        "free_bytes_before_execution": free_before,
        "execution_plan_sha256": execution.plan_sha256,
        "published_artifact_count": len(execution.artifacts),
        "verified_artifact_count": verification.artifact_count,
        "manifest_sha256": verification.manifest_sha256,
        "journal_sha256": verification.journal_sha256,
        "manifest_schema": manifest.get("schema"),
        "source_object_count": len(source_objects),
        "side_binding_count": len(side_bindings),
        "selected_profiles": list(preflight.selected_profiles),
        "tree_receipt": before_verify,
        "verification_read_only": before_verify == after_verify,
        "existing_directory_rejected": no_overwrite_rejected,
        "tree_unchanged_after_collision": after_collision == before_verify,
    }


def _cleanup_owned_run(run: Path, root: Path, identity: Any) -> None:
    if not run.parent.samefile(root):
        raise AcceptanceError("Cleanup target is not a direct child of the acceptance root.")
    if not run.name.startswith("n-drive-") or len(run.name) != 40:
        raise AcceptanceError("Cleanup target does not have an owned acceptance-run name.")
    resolved_run = run.resolve(strict=True)
    resolved_root = root.resolve(strict=True)
    if resolved_run.parent != resolved_root:
        raise AcceptanceError("Resolved cleanup target escaped the acceptance root.")
    _remove_owned_stage(run, identity)
    if os.path.lexists(run):
        raise AcceptanceError("The owned acceptance directory remained after guarded cleanup.")


def run_acceptance(
    target_root: Path,
    *,
    duration_seconds: float = 16.0,
    minimum_source_bytes: int = _DEFAULT_MINIMUM_SOURCE_BYTES,
    enforce_standard_root: bool = True,
    keep_workdir: bool = False,
    promotion_authority: Mapping[str, Any] | None = None,
    non_promotion_diagnostic: bool = False,
) -> dict[str, Any]:
    """Run the complete synthetic acceptance and return its portable receipt."""

    if not 0.25 <= duration_seconds <= 120.0:
        raise AcceptanceError("Synthetic fixture duration must be between 0.25 and 120 seconds.")
    if minimum_source_bytes < 0:
        raise AcceptanceError("Minimum synthetic source bytes cannot be negative.")
    if promotion_authority is None:
        if not non_promotion_diagnostic:
            raise AcceptanceError(
                "Library acceptance without promotion authority must explicitly opt into "
                "non-promotion diagnostic mode."
            )
        requested = (
            validate_standard_acceptance_root(target_root)
            if enforce_standard_root
            else _absolute(target_root)
        )
    else:
        if non_promotion_diagnostic:
            raise AcceptanceError(
                "Promotion authority and non-promotion diagnostic mode are mutually exclusive."
            )
        requested = _validate_promotion_workload(
            target_root,
            duration_seconds=duration_seconds,
            minimum_source_bytes=minimum_source_bytes,
            enforce_standard_root=enforce_standard_root,
            keep_workdir=keep_workdir,
        )
    root = _prepare_root(requested)
    root_identity = _directory_identity(root, label="Acceptance root")
    started_at = _utc_now()
    volume = _volume_details(root)
    ffmpeg_path = (
        _ffmpeg_path()
        if promotion_authority is None
        else _promotion_tool_path(promotion_authority, "ffmpeg")
    )
    run = _new_run_directory(root)
    run_identity = _directory_identity(run, label="Acceptance run")
    cleanup_verified = False
    result: dict[str, Any] | None = None
    try:
        fixture = run / "synthetic album Ω"
        plan_path, sources, source_receipts = _write_synthetic_album(
            fixture,
            duration_seconds=duration_seconds,
            minimum_source_bytes=minimum_source_bytes,
            ffmpeg_path=ffmpeg_path,
        )
        result = {
            "schema": ACCEPTANCE_SCHEMA,
            "started_at": started_at,
            "generated_at": "",
            "result": "passed",
            "scope": {
                "target_root": os.fspath(root),
                "run_directory_name": run.name,
                "synthetic_inputs_only": True,
                "owner_capture_or_project_paths_supplied": False,
                "owner_capture_or_project_content_opened": False,
                "owner_capture_or_project_modified": False,
                "ancestor_directory_entry_names_may_be_observed_for_safe_resolution": True,
                "duration_seconds_per_side": duration_seconds,
                "side_count": 2,
                "tracks_per_side": 3,
                "minimum_source_bytes": minimum_source_bytes,
                "workdir_retained": keep_workdir,
                "cleanup_verified": False,
                "non_promotion_diagnostic": non_promotion_diagnostic,
            },
            "runtime": {
                "groove_serpent_version": __version__,
                "python": platform.python_version(),
                "platform": platform.platform(),
                "os_name": os.name,
            },
            "volume": volume,
            "checks": {
                "source_receipts": list(source_receipts),
                "identity_snapshot": _accept_identity_snapshot(
                    run / "identity snapshot", sources[0]
                ),
                "atomic_no_replace": _accept_atomic_no_replace(run / "atomic no replace"),
                "write_lease": _accept_write_lease(run / "write lease"),
                "interrupted_recovery": _accept_interrupted_recovery(
                    run / "interrupted recovery", plan_path
                ),
                "multi_side_publication": _accept_publication(
                    run / "publication output", plan_path
                ),
            },
            "limitations": [
                "One-directory rename is application-failure atomicity, not proof "
                "of power-loss durability.",
                "Native Windows advisory locks were exercised; mixed Windows/WSL "
                "lock interoperability remains unsupported.",
                "This is one local volume, machine, toolchain, and run; it does not "
                "prove network, Linux, or macOS behavior.",
                "Volume feature flags are operating-system declarations; only checks "
                "named in this receipt were exercised.",
                "Synthetic audio exercises file and media pipelines but is not owner "
                "listening, player, or real-record acceptance.",
            ],
        }
        volume["free_bytes_after_acceptance_before_cleanup"] = shutil.disk_usage(root).free
    finally:
        if not keep_workdir and os.path.lexists(run):
            _cleanup_owned_run(run, root, run_identity)
            cleanup_verified = True
        if _directory_identity(root, label="Acceptance root") != root_identity:
            raise AcceptanceError("Acceptance-root identity changed during the run.")
    if result is None:
        raise AcceptanceError("Acceptance ended without a result receipt.")
    if promotion_authority is not None and not cleanup_verified:
        raise AcceptanceError("Promotion acceptance did not verify owned-workdir cleanup.")
    result["scope"]["cleanup_verified"] = cleanup_verified
    result["authority"] = _finalize_promotion_authority(promotion_authority)
    result["generated_at"] = _utc_now()
    return result


def render_markdown(result: Mapping[str, Any]) -> str:
    """Render a concise human report from one validated receipt mapping."""

    scope = result["scope"]
    volume = result["volume"]
    checks = result["checks"]
    publication = checks["multi_side_publication"]
    snapshot = checks["identity_snapshot"]
    lines = [
        "# N: filesystem acceptance report",
        "",
        f"Result: **{str(result['result']).upper()}**",
        "",
        f"- Receipt schema: `{result['schema']}`",
        f"- Generated: `{result['generated_at']}`",
        f"- Target root: `{scope['target_root']}`",
        f"- Filesystem: `{volume['filesystem']}` (`{volume['drive_type']}`)",
        f"- Allocation unit: `{volume['allocation_unit_bytes']}` bytes",
        f"- Synthetic fixture: `{scope['side_count']}` sides, "
        f"`{scope['tracks_per_side']}` tracks per side, "
        f"`{scope['duration_seconds_per_side']}` seconds per side",
        f"- Guarded work-directory cleanup verified: `{scope['cleanup_verified']}`",
        "",
        "## Exercised checks",
        "",
        "| Check | Result | Direct evidence |",
        "|---|---:|---|",
        "| Verified streaming identity snapshot | PASS | "
        f"{snapshot['stream_chunk_count_floor']} complete 1 MiB chunks; "
        f"SHA-256 `{snapshot['snapshot_receipt']['sha256']}` |",
        "| Atomic create and no-replace rename | PASS | Existing destination rejected; "
        "contender retained |",
        "| Cross-process native write lease | PASS | Competing process rejected, then "
        "lease reacquired |",
        "| Hard-exit inventory/recovery | PASS | `after-journal-staging` orphan "
        "receipted, quarantined, reinventoried, and removed |",
        "| Multi-side publication and space accounting | PASS | "
        f"{publication['estimated_required_bytes']} bytes estimated; "
        f"{publication['published_artifact_count']} artifacts; tree SHA-256 "
        f"`{publication['tree_receipt']['canonical_tree_sha256']}` |",
        "| Strict verify and no overwrite | PASS | Verification was read-only; second "
        "publication was rejected with the tree unchanged |",
        "",
        "## Scope safety",
        "",
        "All audio and project inputs were constructed inside the UUID-named acceptance "
        "directory. No owner-capture or owner-project path is supplied, no owner content is "
        "opened, and no owner input is modified. Production path safety may observe ancestor "
        "directory entry names while resolving the isolated absolute path.",
        "",
        "## Honest limits",
        "",
    ]
    lines.extend(f"- {item}" for item in result["limitations"])
    lines.extend(
        [
            "",
            "This is a bounded candidate acceptance slice. It is not a Groove Serpent 1.0 "
            "claim and is not a substitute for the final full-context and blind review loops.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_new(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-root", type=Path)
    parser.add_argument("--duration-seconds", type=float, default=_PROMOTION_DURATION_SECONDS)
    parser.add_argument(
        "--minimum-source-bytes",
        type=int,
        default=_PROMOTION_MINIMUM_SOURCE_BYTES,
    )
    parser.add_argument("--allow-nonstandard-root", action="store_true")
    parser.add_argument("--keep-workdir", action="store_true")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-markdown", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if argv is not None:
        raise AcceptanceError(
            "Promotion main does not accept injected arguments; launch the canonical script."
        )
    args = _parser().parse_args()
    target_root = args.target_root if args.target_root is not None else configured_acceptance_root()
    _validate_promotion_workload(
        target_root,
        duration_seconds=args.duration_seconds,
        minimum_source_bytes=args.minimum_source_bytes,
        enforce_standard_root=not args.allow_nonstandard_root,
        keep_workdir=args.keep_workdir,
    )
    promotion_authority = _capture_promotion_authority()
    result = run_acceptance(
        target_root,
        duration_seconds=_PROMOTION_DURATION_SECONDS,
        minimum_source_bytes=_PROMOTION_MINIMUM_SOURCE_BYTES,
        enforce_standard_root=True,
        keep_workdir=False,
        promotion_authority=promotion_authority,
    )
    raw = json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.output_json is not None:
        _write_new(args.output_json, raw)
    if args.output_markdown is not None:
        _write_new(args.output_markdown, render_markdown(result))
    print(raw, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
