#!/usr/bin/env python3
"""Create and validate the deterministic Windows media runtime manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA = "groove-serpent.windows-media-runtime-manifest/1"
EXPECTED_BINARIES = {
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
SYSTEM_DLLS = {"bcrypt.dll", "kernel32.dll", "msvcrt.dll", "shell32.dll"}
SOURCE_INPUTS = [
    {
        "name": "chromaprint-1.6.0.tar.gz",
        "sha256": "9d33482e56a1389a37a0d6742c376139fa43e3b8a63d29003222b93db2cb40da",
        "url": (
            "https://github.com/acoustid/chromaprint/releases/download/"
            "v1.6.0/chromaprint-1.6.0.tar.gz"
        ),
    },
    {
        "name": "ffmpeg-8.1.2.tar.xz",
        "sha256": "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c",
        "signature": "ffmpeg-8.1.2.tar.xz.asc",
        "signing_fingerprint": "FCF986EA15E6E293A5644F10B4322F04D67658D8",
        "url": "https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz",
    },
    {
        "name": "soxr-0.1.3-Source.tar.xz",
        "sha256": "b111c15fdc8c029989330ff559184198c161100a59312f5dc19ddeb9b5a15889",
        "url": (
            "https://downloads.sourceforge.net/project/soxr/"
            "soxr-0.1.3-Source.tar.xz"
        ),
    },
    {
        "name": "zlib-1.3.2.tar.xz",
        "sha256": "d7a0654783a4da529d1bb793b7ad9c3318020af77667bcae35f95d0e42a792f3",
        "signature": "zlib-1.3.2.tar.xz.asc",
        "signing_fingerprint": "5ED46A6721D365587791E2AA783FCD8E58BCAFBA",
        "url": "https://zlib.net/zlib-1.3.2.tar.xz",
    },
]


class ManifestFailure(RuntimeError):
    """The staged runtime is not the narrow, auditable build expected here."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _imports(path: Path) -> list[str]:
    completed = subprocess.run(
        ["x86_64-w64-mingw32-objdump", "-p", str(path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ManifestFailure(f"Could not inspect PE imports for {path.name}.")
    return sorted(
        set(re.findall(r"^\s*DLL Name:\s*(\S+)\s*$", completed.stdout, re.MULTILINE)),
        key=str.casefold,
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestFailure(f"Could not load strict JSON from {path.name}.") from exc
    if not isinstance(payload, dict):
        raise ManifestFailure(f"{path.name} must contain a JSON object.")
    return payload


def build_manifest(
    runtime_dir: Path,
    *,
    configure_file: Path,
    environment_file: Path,
    source_date_epoch: int,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    binary_names = {
        path.name for path in runtime_dir.iterdir() if path.suffix.casefold() in {".dll", ".exe"}
    }
    if binary_names != EXPECTED_BINARIES:
        raise ManifestFailure(
            "Runtime binary inventory differs from the exact allowlist: "
            f"{sorted(binary_names ^ EXPECTED_BINARIES)}"
        )

    configuration = [
        line.strip()
        for line in configure_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not configuration or configuration[0] != "FFmpeg 8.1.2 configure arguments:":
        raise ManifestFailure("The FFmpeg configure argument record is malformed.")
    flags = configuration[1:]
    for forbidden in ("--enable-gpl", "--enable-nonfree", "--enable-version3"):
        if forbidden in flags:
            raise ManifestFailure(f"Forbidden FFmpeg flag present: {forbidden}")
    for required in (
        "--disable-network",
        "--disable-static",
        "--enable-shared",
        "--enable-chromaprint",
        "--enable-libsoxr",
        "--enable-zlib",
    ):
        if required not in flags:
            raise ManifestFailure(f"Required FFmpeg flag missing: {required}")

    smoke = _load_json(runtime_dir / "CAPABILITY-SMOKE.json")
    if (
        smoke.get("schema") != "groove-serpent.windows-media-capability-smoke/1"
        or smoke.get("result") != "passed"
    ):
        raise ManifestFailure("The staged capability smoke did not pass.")

    bundled = {name.casefold() for name in EXPECTED_BINARIES}
    import_inventory: dict[str, Any] = {}
    for name in sorted(EXPECTED_BINARIES, key=str.casefold):
        imported = _imports(runtime_dir / name)
        unexpected = [
            item
            for item in imported
            if item.casefold() not in bundled and item.casefold() not in SYSTEM_DLLS
        ]
        if unexpected:
            raise ManifestFailure(f"Unexpected PE imports in {name}: {unexpected}")
        import_inventory[name] = {
            "bundled": sorted(
                (item for item in imported if item.casefold() in bundled),
                key=str.casefold,
            ),
            "windows_system": sorted(
                (item for item in imported if item.casefold() in SYSTEM_DLLS),
                key=str.casefold,
            ),
        }

    excluded = {"BUILD-MANIFEST.json", "SHA256SUMS"}
    inventory: list[dict[str, Any]] = []
    for path in sorted(runtime_dir.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name in excluded:
            continue
        relative = path.relative_to(runtime_dir).as_posix()
        inventory.append(
            {
                "mode": "0755" if path.stat().st_mode & stat.S_IXUSR else "0644",
                "path": relative,
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )

    return {
        "schema": SCHEMA,
        "artifact": {
            "architecture": "x86_64-w64-mingw32",
            "ffmpeg_version": "8.1.2",
            "profile": "groove-serpent-minimal-audio-shared-v1",
            "source_date_epoch": source_date_epoch,
        },
        "build_environment": environment_file.read_text(encoding="utf-8").splitlines(),
        "capability_smoke_sha256": _sha256(runtime_dir / "CAPABILITY-SMOKE.json"),
        "ffmpeg_configure_arguments": flags,
        "license_evidence": {
            "classification_reported_by_ffmpeg": "LGPL version 2.1 or later",
            "chromaprint": "LGPL-2.1 as a whole; FFT_LIB=kissfft (BSD-3-Clause)",
            "ffmpeg_gpl_flag": False,
            "ffmpeg_nonfree_flag": False,
            "ffmpeg_version3_flag": False,
            "gcc_runtime": (
                "statically linked under the GCC Runtime Library Exception 3.1; "
                "the exact Debian notice is included"
            ),
            "libsoxr": "LGPL-2.1-or-later shared DLL",
            "linking": "shared FFmpeg, Chromaprint, and libsoxr libraries",
            "notice": (
                "Evidence inventory only. It is not legal advice or a legal-compliance "
                "certification."
            ),
            "zlib": "zlib License; statically linked only for PNG artwork parsing",
        },
        "pe_imports": import_inventory,
        "runtime_files": inventory,
        "source_inputs": SOURCE_INPUTS,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--configure-file", type=Path, required=True)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = build_manifest(
            args.runtime_dir,
            configure_file=args.configure_file,
            environment_file=args.environment_file,
            source_date_epoch=args.source_date_epoch,
        )
        encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
        args.output.write_text(encoded, encoding="utf-8", newline="\n")
    except (ManifestFailure, OSError, ValueError) as exc:
        print(f"manifest generation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Runtime manifest written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
