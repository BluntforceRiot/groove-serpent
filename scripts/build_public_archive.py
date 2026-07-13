from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import unicodedata
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RELEASE_NAME = "groove-serpent-0.5.0-alpha.1"
DEFAULT_ARCHIVE = ROOT / "dist" / f"{RELEASE_NAME}-source.zip"
DEFAULT_MANIFEST = ROOT / "dist" / "SOURCE_MANIFEST.sha256"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
ZIP_CREATE_SYSTEM_UNIX = 3
ZIP_VERSION_2_0 = 20
ZIP_REGULAR_FILE_MODE = 0o100644
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "public",
    "public-release",
    "venv",
}
FORBIDDEN_SUFFIXES = {
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".pyo",
    ".wav",
    ".zip",
}
FORBIDDEN_ENDINGS = {
    ".album.json",
    ".click-scan.json",
    ".groove.json",
    ".restoration-recipe.json",
    ".tracklist.json",
}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def included_files(root: Path = ROOT) -> list[Path]:
    selected: list[Path] = []
    portable_names: dict[str, Path] = {}
    total_bytes = 0
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        if path.is_symlink():
            raise RuntimeError(f"Public source archives cannot contain links: {relative}")
        if not path.is_file():
            continue
        lowered = relative.name.casefold()
        if (
            path.suffix.casefold() in FORBIDDEN_SUFFIXES
            or lowered.startswith(".env")
            or any(lowered.endswith(ending) for ending in FORBIDDEN_ENDINGS)
        ):
            raise RuntimeError(f"Refusing private or generated public member: {relative}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise RuntimeError(f"Public member exceeds {MAX_FILE_BYTES} bytes: {relative}")
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise RuntimeError("Public source files exceed the archive size ceiling.")
        portable = unicodedata.normalize("NFC", relative.as_posix()).casefold()
        previous = portable_names.get(portable)
        if previous is not None:
            raise RuntimeError(f"Portable archive-name collision: {previous} and {relative}")
        portable_names[portable] = relative
        selected.append(path)
    return sorted(selected, key=lambda item: item.relative_to(root).as_posix().casefold())


def _run_git(root: Path, *arguments: str) -> bytes:
    command = ["git", "-C", str(root), *arguments]
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Git is required to build a public source archive.") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Git provenance check failed: {detail}") from error
    return completed.stdout


def require_canonical_git_checkout(root: Path, files: list[Path]) -> None:
    root = root.resolve()
    top_level = Path(
        _run_git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    ).resolve()
    if top_level != root:
        raise RuntimeError(f"Archive root must be the Git worktree root: {root}")
    dirty = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", ".")
    if dirty:
        rendered = dirty.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Public source archive requires a clean Git checkout:\n{rendered}")
    for path in files:
        relative = path.relative_to(root).as_posix()
        canonical = _run_git(root, "show", f"HEAD:{relative}")
        if path.read_bytes() != canonical:
            raise RuntimeError(
                "Checkout bytes differ from the canonical Git blob; clean-checkout "
                f"the release commit before building: {relative}"
            )


def zip_bytes(archive: zipfile.ZipFile, relative: str, payload: bytes) -> None:
    info = zipfile.ZipInfo(f"{RELEASE_NAME}/{relative}", (1980, 1, 1, 0, 0, 0))
    info.create_system = ZIP_CREATE_SYSTEM_UNIX
    info.create_version = ZIP_VERSION_2_0
    info.extract_version = ZIP_VERSION_2_0
    info.reserved = 0
    info.flag_bits = 0
    info.volume = 0
    info.internal_attr = 0
    info.external_attr = ZIP_REGULAR_FILE_MODE << 16
    info.extra = b""
    info.comment = b""
    # Stored members avoid zlib-version differences across CI operating systems.
    info.compress_type = zipfile.ZIP_STORED
    archive.writestr(info, payload)


def build_archive(
    archive_path: Path = DEFAULT_ARCHIVE,
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    root: Path = ROOT,
    require_git_checkout: bool = True,
) -> tuple[int, str]:
    root = root.resolve()
    files = included_files(root)
    if require_git_checkout:
        require_canonical_git_checkout(root, files)
    lines = [
        f"{sha256_file(path)}  {path.relative_to(root).as_posix()}" for path in files
    ]
    manifest = ("\n".join(lines) + "\n").encode("utf-8")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_suffix(archive_path.suffix + ".tmp")
    if archive_path.exists() or temporary.exists():
        raise FileExistsError(f"Refusing to replace public release asset: {archive_path}")
    destination_created = False
    try:
        with zipfile.ZipFile(temporary, "x") as archive:
            for path in files:
                zip_bytes(
                    archive,
                    path.relative_to(root).as_posix(),
                    path.read_bytes(),
                )
            zip_bytes(archive, "SOURCE_MANIFEST.sha256", manifest)
        with temporary.open("rb") as source, archive_path.open("xb") as destination:
            destination_created = True
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        temporary.unlink()
        manifest_path.write_bytes(manifest)
    except BaseException:
        temporary.unlink(missing_ok=True)
        if destination_created:
            archive_path.unlink(missing_ok=True)
        raise
    return len(files) + 1, sha256_file(archive_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the deterministic public source ZIP.")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    count, digest = build_archive(args.archive, args.manifest)
    print(f"Created {args.archive} with {count} members")
    print(f"SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
