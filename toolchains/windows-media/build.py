#!/usr/bin/python3.12 -I
"""Launch the Windows-media Bash recipe behind an isolated environment boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import NoReturn, Sequence


BASH = Path("/usr/bin/bash")
PYTHON = Path("/usr/bin/python3.12")
JOBS_RE = re.compile(r"[1-9][0-9]{0,3}\Z")
WSL_DISTRO_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
MAX_JOBS = 4_096
MAX_PATH_CHARS = 4_096
CLEAN_MARKER = "GROOVE_SERPENT_WINDOWS_MEDIA_CLEAN_ENV"
TOOLCHAIN_AUTHORITY_FILES = (
    "README.md",
    "bootstrap-ubuntu-24.04.sh",
    "build.py",
    "build.sh",
    "capability_smoke.py",
    "keys/ffmpeg-release-signing-key.asc",
    "keys/zlib-mark-adler.asc",
    "make_manifest.py",
    "ubuntu-24.04-packages.txt",
    "verify_artifact.py",
    "verify_build_host.sh",
)


def _fail(message: str) -> NoReturn:
    raise RuntimeError(f"windows-media launcher failed: {message}")


def _recipe_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _trusted_provider(path: Path) -> None:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        _fail(f"trusted provider cannot be inspected: {path}: {exc}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not metadata.st_mode & stat.S_IXUSR
        or resolved != path
    ):
        _fail(
            "trusted provider must be one exact root-owned, root-group, "
            f"non-group/world-writable executable regular file: {path}"
        )


def _bound_authority_file(path: Path) -> str:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        _fail(f"toolchain authority file cannot be inspected: {path}: {exc}")
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or int(metadata.st_nlink) != 1
        or resolved != path
    ):
        _fail(f"toolchain authority member must be one plain single-link file: {path}")
    flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail(f"toolchain authority file cannot be bound: {path}: {exc}")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > 4 * 1024 * 1024
        ):
            _fail(f"toolchain authority member is invalid or unexpectedly large: {path}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        observed_path = path.lstat()
    except OSError as exc:
        _fail(f"toolchain authority file changed while being bound: {path}: {exc}")
    identity = _recipe_identity(before)
    if identity != _recipe_identity(after) or identity[:-1] != _recipe_identity(observed_path)[:-1]:
        _fail(f"toolchain authority file changed while being bound: {path}")
    return digest.hexdigest()


def _plain_toolchain_authority() -> tuple[Path, str]:
    launcher = Path(os.path.abspath(os.fspath(__file__)))
    root = launcher.parent
    files = [
        {"path": name, "sha256": _bound_authority_file(root / name)}
        for name in sorted(TOOLCHAIN_AUTHORITY_FILES)
    ]
    encoded = json.dumps(
        {
            "files": files,
            "schema": "groove-serpent.windows-media-content-authority/1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return root / "build.sh", hashlib.sha256(encoded).hexdigest()


def _validated_environment(authority_sha256: str) -> dict[str, str]:
    destination = os.environ.get("DIST_DIR", "")
    if (
        not destination
        or len(destination) > MAX_PATH_CHARS
        or not os.path.isabs(destination)
        or os.path.abspath(destination) == os.path.abspath(os.sep)
        or any(ord(character) < 32 for character in destination)
    ):
        _fail("DIST_DIR must be one bounded absolute non-root path")
    jobs = os.environ.get("JOBS", "")
    if jobs and (JOBS_RE.fullmatch(jobs) is None or int(jobs) > MAX_JOBS):
        _fail(f"JOBS must be an integer from 1 through {MAX_JOBS}")
    distro = os.environ.get("WSL_DISTRO_NAME", "")
    if WSL_DISTRO_RE.fullmatch(distro) is None:
        _fail("WSL_DISTRO_NAME must identify one safe WSL distro share")
    return {
        CLEAN_MARKER: "1",
        "DIST_DIR": destination,
        "GROOVE_SERPENT_LAUNCH_AUTHORITY_SHA256": authority_sha256,
        "JOBS": jobs,
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
        "WSL_DISTRO_NAME": distro,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        _fail("this launcher accepts no arguments; use DIST_DIR and optional JOBS")
    if sys.flags.isolated != 1 or sys.flags.dont_write_bytecode != 1:
        _fail(
            "invoke this launcher with the exact isolated, no-bytecode interpreter options: "
            "python3.12 -I -B"
        )
    _trusted_provider(PYTHON)
    _trusted_provider(BASH)
    try:
        exact_python = os.path.samefile(sys.executable, PYTHON)
    except OSError as exc:
        _fail(f"the exact Python provider cannot be inspected: {exc}")
    if not exact_python:
        _fail(f"the launcher requires the exact interpreter {PYTHON}")
    recipe, authority_sha256 = _plain_toolchain_authority()
    environment = _validated_environment(authority_sha256)
    try:
        os.execve(
            BASH,
            [os.fspath(BASH), "--noprofile", "--norc", "-p", os.fspath(recipe)],
            environment,
        )
    except OSError as exc:
        _fail(f"the privileged-mode Bash recipe could not start: {exc}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
