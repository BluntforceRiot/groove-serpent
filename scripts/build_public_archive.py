from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parent.parent
if not __package__:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))

from scripts._release_fs import (  # noqa: E402
    PathIdentity,
    canonical_portable_relative_path,
    capture_descriptor_identity,
    capture_identity,
    ensure_plain_directory_path,
    inspect_plain_directory,
    inspect_single_link_file,
    read_single_link_file,
    require_stable_creation_identity,
    unlink_owned_file_candidates,
    walk_plain_tree,
    zip_layout_is_exact,
)
from scripts._release_evidence import (  # noqa: E402
    ACYCLIC_GENERATED_REPORT_PATHS,
    CANDIDATE_INDEX_NAME,
    INDEX_NAME,
    PublicationAttempt,
    assert_public_payload_safe,
    canonical_json_bytes,
    marker_artifact,
    product_source_authority,
    require_sha256,
    strict_json_object,
    unique_sibling_path,
    validate_marker_artifact,
    validate_public_release_commit,
)
from groove_serpent.executable_discovery import find_executable  # noqa: E402


VERSION = "1.0.0"
RELEASE_NAME = f"groove-serpent-{VERSION}"
DEFAULT_ARCHIVE = ROOT / "dist" / f"{RELEASE_NAME}-source.zip"
DEFAULT_MANIFEST = ROOT / "dist" / "SOURCE_MANIFEST.sha256"
MARKER_NAME = f"{RELEASE_NAME}-source.commit.json"
DEFAULT_MARKER = ROOT / "dist" / MARKER_NAME
MARKER_SCHEMA = "groove-serpent.public-source-archive-commit/1"
PUBLIC_RELEASE_COMMIT_NAME = "PUBLIC_RELEASE_COMMIT.json"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
ZIP_CREATE_SYSTEM_UNIX = 3
ZIP_VERSION_2_0 = 20
ZIP_REGULAR_FILE_MODE = 0o100644
GIT_COMMIT_RE = re.compile(rb"[0-9a-f]{40,64}\Z")
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
    "node_modules",
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


def _excluded(relative: Path) -> bool:
    return any(
        part.casefold() in EXCLUDED_PARTS or part.casefold().endswith(".egg-info")
        for part in relative.parts
    )


def _validate_member_name(relative: Path) -> tuple[str, str]:
    canonical, portable = canonical_portable_relative_path(
        relative.as_posix(),
        "Public source member",
    )
    lowered = relative.name.casefold()
    if (
        relative.suffix.casefold() in FORBIDDEN_SUFFIXES
        or lowered.startswith(".env")
        or any(lowered.endswith(ending) for ending in FORBIDDEN_ENDINGS)
    ):
        raise RuntimeError(f"Refusing private or generated public member: {relative}")
    return canonical, portable


def included_files(root: Path = ROOT) -> list[Path]:
    """Enumerate one plain worktree without following any linked directory."""

    root = Path(os.path.abspath(os.fspath(root)))
    inspect_plain_directory(root, "Public source root")
    selected: list[Path] = []
    portable_names: dict[str, Path] = {}
    total_bytes = 0

    def skip_directory(path: Path) -> bool:
        return _excluded(path.relative_to(root))

    for path in walk_plain_tree(
        root,
        "Public source tree",
        skip_directory=skip_directory,
    ):
        relative = path.relative_to(root)
        if _excluded(relative):
            continue
        details = inspect_single_link_file(path, "Public source member")
        _canonical, portable = _validate_member_name(relative)
        size = int(details.st_size)
        if size > MAX_FILE_BYTES:
            raise RuntimeError(f"Public member exceeds {MAX_FILE_BYTES} bytes: {relative}")
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise RuntimeError("Public source files exceed the archive size ceiling.")
        previous = portable_names.get(portable)
        if previous is not None:
            raise RuntimeError(f"Portable archive-name collision: {previous} and {relative}")
        portable_names[portable] = relative
        selected.append(path)
    return sorted(
        selected,
        key=lambda item: canonical_portable_relative_path(
            item.relative_to(root).as_posix(),
            "Public source member",
        )[1],
    )


def _git_executable() -> str:
    git = find_executable("git")
    if git is None:
        raise RuntimeError("Git is required on an absolute trusted PATH entry.")
    return str(git)


def _run_git(root: Path, git: str, *arguments: str) -> bytes:
    command = [git, "--no-replace-objects", "-C", str(root), *arguments]
    environment = {
        name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except FileNotFoundError as error:
        raise RuntimeError("Git is required to build a public source archive.") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Git provenance check failed: {detail}") from error
    return completed.stdout


def _current_commit(root: Path, git: str) -> str:
    raw = _run_git(root, git, "rev-parse", "--verify", "HEAD^{commit}").strip()
    if GIT_COMMIT_RE.fullmatch(raw) is None:
        raise RuntimeError("Git HEAD did not resolve to one canonical commit.")
    return raw.decode("ascii")


def _assert_git_authority(root: Path, git: str, expected_commit: str | None) -> str:
    top_level = Path(_run_git(root, git, "rev-parse", "--show-toplevel").decode("utf-8").strip())
    try:
        same_root = os.path.samefile(top_level, root)
    except OSError:
        same_root = False
    if not same_root:
        raise RuntimeError(f"Archive root must be the Git worktree root: {root}")
    commit = _current_commit(root, git)
    if expected_commit is not None and commit != expected_commit:
        raise RuntimeError("Git HEAD changed while the public archive was being built.")
    dirty = _run_git(
        root,
        git,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        ".",
    )
    if dirty:
        rendered = dirty.decode("utf-8", errors="replace").replace("\x00", "\n").strip()
        raise RuntimeError(f"Public source archive requires a clean Git checkout:\n{rendered}")
    final_commit = _current_commit(root, git)
    if final_commit != commit:
        raise RuntimeError("Git HEAD changed while public source authority was checked.")
    return final_commit


def _git_inventory(root: Path, git: str, commit: str) -> list[tuple[str, str]]:
    raw = _run_git(root, git, "ls-tree", "-r", "-z", "--full-tree", commit)
    selected: list[tuple[str, str]] = []
    portable_names: dict[str, str] = {}
    for record in raw.split(b"\x00"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ", 2)
            text = encoded_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Git tree inventory is malformed.") from exc
        canonical, portable = canonical_portable_relative_path(
            text,
            "Git tree member",
        )
        relative_posix = PurePosixPath(canonical)
        relative = Path(*relative_posix.parts)
        if _excluded(relative):
            raise RuntimeError(f"Git tree contains an excluded generated path: {text}")
        if (
            mode not in {b"100644", b"100755"}
            or object_type != b"blob"
            or GIT_COMMIT_RE.fullmatch(object_id) is None
        ):
            raise RuntimeError(f"Git tree contains a non-regular public member: {text}")
        _validate_member_name(relative)
        previous = portable_names.get(portable)
        if previous is not None:
            raise RuntimeError(f"Portable archive-name collision: {previous} and {text}")
        portable_names[portable] = text
        selected.append((relative_posix.as_posix(), object_id.decode("ascii")))
    return sorted(selected, key=lambda item: item[0].casefold())


def _canonical_git_payloads(root: Path) -> tuple[str, str, list[tuple[str, bytes]]]:
    git = _git_executable()
    commit = _assert_git_authority(root, git, None)
    git_inventory = _git_inventory(root, git, commit)
    git_paths = [relative for relative, _object_id in git_inventory]
    files = included_files(root)
    worktree_paths = [path.relative_to(root).as_posix() for path in files]
    if worktree_paths != git_paths:
        raise RuntimeError("Worktree inventory differs from the immutable Git tree.")

    records: list[tuple[str, bytes]] = []
    total = 0
    for (relative, object_id), path in zip(git_inventory, files, strict=True):
        canonical = _run_git(root, git, "cat-file", "blob", object_id)
        if len(canonical) > MAX_FILE_BYTES:
            raise RuntimeError(f"Public member exceeds {MAX_FILE_BYTES} bytes: {relative}")
        worktree = read_single_link_file(path, MAX_FILE_BYTES, "Public source member")
        if worktree != canonical:
            raise RuntimeError(f"Checkout bytes differ from the immutable Git blob: {relative}")
        assert_public_payload_safe(
            relative,
            canonical,
            context="Public source Git blob",
        )
        total += len(canonical)
        if total > MAX_TOTAL_BYTES:
            raise RuntimeError("Public source files exceed the archive size ceiling.")
        records.append((relative, canonical))
    _assert_git_authority(root, git, commit)
    return git, commit, records


def _public_release_authority(
    records: Sequence[tuple[str, bytes]],
) -> tuple[str, str]:
    by_name = {relative: payload for relative, payload in records}
    if len(by_name) != len(records):
        raise RuntimeError("Public Git payload contains duplicate paths.")
    if INDEX_NAME in by_name or CANDIDATE_INDEX_NAME in by_name:
        raise RuntimeError("Private release evidence index must not enter the public tree.")
    try:
        marker = by_name[PUBLIC_RELEASE_COMMIT_NAME]
    except KeyError as exc:
        raise RuntimeError("Public Git payload is missing its release commit marker.") from exc
    directories: set[str] = set()
    entries: list[dict[str, object]] = []
    product_records: list[tuple[str, int, str]] = []
    for relative, payload in records:
        assert_public_payload_safe(
            relative,
            payload,
            context="Public source payload",
        )
        if relative == PUBLIC_RELEASE_COMMIT_NAME:
            continue
        path = PurePosixPath(relative)
        for parent in path.parents:
            if parent.as_posix() != ".":
                directories.add(parent.as_posix())
        entries.append(
            {
                "bytes": len(payload),
                "kind": "file",
                "path": relative,
                "sha256": sha256_bytes(payload),
            }
        )
        if relative not in ACYCLIC_GENERATED_REPORT_PATHS:
            product_records.append((relative, len(payload), sha256_bytes(payload)))
    entries.extend(
        {"bytes": 0, "kind": "directory", "path": path, "sha256": ""} for path in directories
    )
    entries.sort(key=lambda item: (str(item["path"]).casefold(), str(item["kind"])))
    authority, candidate_digest = validate_public_release_commit(
        marker,
        release_version=VERSION,
        release_directory=RELEASE_NAME,
        entries=entries,
    )
    if authority != product_source_authority(
        product_records,
        release_version=VERSION,
    ):
        raise RuntimeError(
            "Public release commit identifies the wrong selected product-source authority."
        )
    return authority, candidate_digest


def require_canonical_git_checkout(
    root: Path,
    files: Sequence[Path],
    payloads: Mapping[Path, bytes] | None = None,
) -> None:
    """Compatibility wrapper that now requires the exact immutable inventory."""

    _git, _commit, records = _canonical_git_payloads(root)
    expected = {relative: payload for relative, payload in records}
    supplied_names = [path.relative_to(root).as_posix() for path in files]
    if supplied_names != list(expected):
        raise RuntimeError("Supplied archive inventory differs from immutable Git HEAD.")
    for path in files:
        relative = path.relative_to(root).as_posix()
        payload = (
            read_single_link_file(path, MAX_FILE_BYTES, "Public source member")
            if payloads is None
            else payloads[path]
        )
        if payload != expected[relative]:
            raise RuntimeError(f"Supplied bytes differ from immutable Git HEAD: {relative}")


def zip_bytes(archive: zipfile.ZipFile, relative: str, payload: bytes) -> None:
    relative, _portable = canonical_portable_relative_path(
        relative,
        "Public source ZIP member",
    )
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


def _zip_info_has_release_profile(info: zipfile.ZipInfo) -> bool:
    return (
        not info.is_dir()
        and info.date_time == (1980, 1, 1, 0, 0, 0)
        and info.create_system == ZIP_CREATE_SYSTEM_UNIX
        and info.create_version == ZIP_VERSION_2_0
        and info.extract_version == ZIP_VERSION_2_0
        and info.reserved == 0
        and info.flag_bits == 0
        and info.volume == 0
        and info.internal_attr == 0
        and info.external_attr == ZIP_REGULAR_FILE_MODE << 16
        and info.extra == b""
        and info.comment == b""
        and info.compress_type == zipfile.ZIP_STORED
    )


def _verify_archive(path: Path, records: Sequence[tuple[str, bytes]]) -> bytes:
    portable_names: dict[str, str] = {}
    canonical_records: list[tuple[str, bytes]] = []
    for supplied, record_payload in records:
        relative, portable = canonical_portable_relative_path(
            supplied,
            "Public source ZIP member",
        )
        previous = portable_names.get(portable)
        if previous is not None:
            raise RuntimeError(f"Portable archive-name collision: {previous!r} and {relative!r}")
        portable_names[portable] = relative
        canonical_records.append((relative, record_payload))
    records = canonical_records
    expected_names = [f"{RELEASE_NAME}/{relative}" for relative, _payload in records]
    payload = read_single_link_file(path, MAX_ARCHIVE_BYTES, "Public source ZIP")
    try:
        container = io.BytesIO(payload)
        with zipfile.ZipFile(container, "r") as archive:
            infos = archive.infolist()
            if not zip_layout_is_exact(container, len(payload), infos):
                raise RuntimeError("Public source ZIP container layout is not exact.")
            if [info.filename for info in infos] != expected_names:
                raise RuntimeError("Public source ZIP inventory changed after construction.")
            for info, (_relative, expected) in zip(infos, records, strict=True):
                if not _zip_info_has_release_profile(info) or archive.read(info) != expected:
                    raise RuntimeError(
                        f"Public source ZIP member failed verification: {info.filename}"
                    )
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("Public source ZIP cannot be reopened safely.") from exc
    return payload


def _source_marker_bytes(
    *,
    candidate_authority: str,
    candidate_evidence_sha256: str,
    git_commit: str,
    archive_path: Path,
    archive_payload: bytes,
    manifest_path: Path,
    manifest_payload: bytes,
    member_count: int,
) -> bytes:
    return canonical_json_bytes(
        {
            "archive": marker_artifact(
                archive_path,
                archive_payload,
                member_count=member_count,
            ),
            "candidate_authority": candidate_authority,
            "candidate_evidence_sha256": candidate_evidence_sha256,
            "git_commit": git_commit,
            "manifest": marker_artifact(manifest_path, manifest_payload),
            "release_version": VERSION,
            "schema": MARKER_SCHEMA,
        }
    )


def _source_manifest_entries(payload: bytes) -> list[tuple[str, str]]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Public source manifest is not UTF-8.") from exc
    if not text.endswith("\n") or "\r" in text:
        raise RuntimeError("Public source manifest line endings are invalid.")
    entries: list[tuple[str, str]] = []
    portable_names: set[str] = set()
    for line in text.splitlines():
        if len(line) < 67 or line[64:66] != "  ":
            raise RuntimeError("Public source manifest record is malformed.")
        digest = require_sha256(line[:64], "Public source manifest digest")
        relative, portable = canonical_portable_relative_path(
            line[66:],
            "Public source manifest member",
        )
        if portable in portable_names:
            raise RuntimeError("Public source manifest contains a duplicate member.")
        portable_names.add(portable)
        entries.append((relative, digest))
    if not entries:
        raise RuntimeError("Public source manifest is empty.")
    if entries != sorted(entries, key=lambda item: item[0].casefold()):
        raise RuntimeError("Public source manifest entries are not canonically ordered.")
    return entries


def verify_source_archive_commit(
    marker_path: Path,
    archive_path: Path,
    manifest_path: Path,
    *,
    expected_commit: str | None = None,
    archive_name: str | None = None,
    manifest_name: str | None = None,
) -> tuple[int, str]:
    marker_payload = read_single_link_file(
        marker_path,
        MAX_FILE_BYTES,
        "Public source commit marker",
    )
    marker = strict_json_object(marker_payload, "Public source commit marker")
    expected_keys = {
        "archive",
        "candidate_authority",
        "candidate_evidence_sha256",
        "git_commit",
        "manifest",
        "release_version",
        "schema",
    }
    if set(marker) != expected_keys:
        raise RuntimeError("Public source commit marker keys are invalid.")
    if marker["schema"] != MARKER_SCHEMA or marker["release_version"] != VERSION:
        raise RuntimeError("Public source commit marker schema or version is invalid.")
    authority = require_sha256(marker["candidate_authority"], "Public source authority")
    candidate_digest = require_sha256(
        marker["candidate_evidence_sha256"],
        "Public source candidate evidence",
    )
    commit = marker["git_commit"]
    if (
        not isinstance(commit, str)
        or GIT_COMMIT_RE.fullmatch(commit.encode("ascii", errors="ignore")) is None
    ):
        raise RuntimeError("Public source commit marker Git identity is invalid.")
    if expected_commit is not None and commit != expected_commit:
        raise RuntimeError("Public source commit marker identifies the wrong Git commit.")
    archive_payload = read_single_link_file(
        archive_path,
        MAX_ARCHIVE_BYTES,
        "Public source ZIP",
    )
    manifest_payload = read_single_link_file(
        manifest_path,
        MAX_TOTAL_BYTES,
        "Public source manifest",
    )
    archive_value = marker["archive"]
    if not isinstance(archive_value, dict):
        raise RuntimeError("Public source archive marker is invalid.")
    member_count = archive_value.get("member_count")
    if not isinstance(member_count, int) or isinstance(member_count, bool) or member_count < 1:
        raise RuntimeError("Public source archive member count is invalid.")
    validate_marker_artifact(
        archive_value,
        archive_path,
        archive_payload,
        context="Public source archive",
        extra_keys={"member_count"},
        expected_name=archive_path.name if archive_name is None else archive_name,
    )
    validate_marker_artifact(
        marker["manifest"],
        manifest_path,
        manifest_payload,
        context="Public source manifest",
        expected_name=manifest_path.name if manifest_name is None else manifest_name,
    )
    manifest_entries = _source_manifest_entries(manifest_payload)
    _manifest_relative, manifest_portable = canonical_portable_relative_path(
        "SOURCE_MANIFEST.sha256",
        "Generated public source manifest",
    )
    if any(
        canonical_portable_relative_path(relative, "Public source manifest member")[1]
        == manifest_portable
        for relative, _digest in manifest_entries
    ):
        raise RuntimeError("Public source manifest collides with its generated ZIP member.")
    expected_names = [f"{RELEASE_NAME}/{relative}" for relative, _digest in manifest_entries]
    expected_names.append(f"{RELEASE_NAME}/SOURCE_MANIFEST.sha256")
    payload_records: list[tuple[str, bytes]] = []
    try:
        container = io.BytesIO(archive_payload)
        with zipfile.ZipFile(container, "r") as archive:
            infos = archive.infolist()
            if not zip_layout_is_exact(container, len(archive_payload), infos):
                raise RuntimeError("Public source ZIP container layout is not exact.")
            if len(infos) != member_count or [info.filename for info in infos] != expected_names:
                raise RuntimeError("Public source ZIP inventory does not match its commit marker.")
            for info, (relative, digest) in zip(
                infos[:-1],
                manifest_entries,
                strict=True,
            ):
                if not _zip_info_has_release_profile(info):
                    raise RuntimeError("Public source ZIP member profile is not canonical.")
                payload = archive.read(info)
                if sha256_bytes(payload) != digest:
                    raise RuntimeError("Public source ZIP member does not match its manifest.")
                payload_records.append((relative, payload))
            if not _zip_info_has_release_profile(infos[-1]):
                raise RuntimeError("Public source manifest member profile is not canonical.")
            if archive.read(infos[-1]) != manifest_payload:
                raise RuntimeError("Public source ZIP carries a different manifest.")
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("Public source ZIP cannot be independently verified.") from exc
    public_authority, public_candidate_digest = _public_release_authority(payload_records)
    if authority != public_authority or candidate_digest != public_candidate_digest:
        raise RuntimeError("Public source commit marker disagrees with the public release commit.")
    return member_count, commit


def build_archive(
    archive_path: Path = DEFAULT_ARCHIVE,
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    root: Path = ROOT,
    require_git_checkout: bool = True,
    marker_path: Path | None = None,
) -> tuple[int, str]:
    if not require_git_checkout:
        raise RuntimeError("Unanchored public source archives are unsupported.")
    root = Path(os.path.abspath(os.fspath(root)))
    git, commit, payload_records = _canonical_git_payloads(root)
    candidate_authority, candidate_evidence_sha256 = _public_release_authority(payload_records)
    _manifest_name, manifest_portable = canonical_portable_relative_path(
        "SOURCE_MANIFEST.sha256",
        "Generated source manifest",
    )
    if any(
        canonical_portable_relative_path(relative, "Public source member")[1] == manifest_portable
        for relative, _payload in payload_records
    ):
        raise RuntimeError("Git tree collides with the generated source manifest.")
    lines = [f"{sha256_bytes(payload)}  {relative}" for relative, payload in payload_records]
    manifest = ("\n".join(lines) + "\n").encode("utf-8")
    archive_path = Path(os.path.abspath(os.fspath(archive_path.expanduser())))
    manifest_path = Path(os.path.abspath(os.fspath(manifest_path.expanduser())))
    marker_path = Path(
        os.path.abspath(
            os.fspath(archive_path.parent / MARKER_NAME if marker_path is None else marker_path)
        )
    )
    output_paths = (archive_path, manifest_path, marker_path)
    if len(set(output_paths)) != len(output_paths):
        raise RuntimeError("Public source archive, manifest, and marker paths must be distinct.")
    require_stable_creation_identity(
        archive_path.parent,
        "Public source archive output",
    )
    require_stable_creation_identity(
        manifest_path.parent,
        "Public source manifest output",
    )
    require_stable_creation_identity(marker_path.parent, "Public source marker output")
    ensure_plain_directory_path(
        archive_path.parent,
        "Public source archive output directory",
        create=True,
    )
    ensure_plain_directory_path(
        marker_path.parent,
        "Public source marker output directory",
        create=True,
    )
    ensure_plain_directory_path(
        manifest_path.parent,
        "Public source manifest output directory",
        create=True,
    )
    existing = next(
        (path for path in output_paths if os.path.lexists(path)),
        None,
    )
    if existing is not None:
        raise FileExistsError(f"Refusing to replace public release asset: {existing}")
    temporary = unique_sibling_path(archive_path, "source-archive")
    manifest_temporary = unique_sibling_path(manifest_path, "source-manifest")
    marker_temporary = unique_sibling_path(marker_path, "source-marker")
    if len({*output_paths, temporary, manifest_temporary, marker_temporary}) != 6:
        raise RuntimeError("Public source staging and final paths must be distinct.")
    archive_identity: PathIdentity | None = None
    manifest_identity: PathIdentity | None = None
    marker_identity: PathIdentity | None = None
    manifest_publication = PublicationAttempt()
    archive_publication = PublicationAttempt()
    marker_publication = PublicationAttempt()
    records = [*payload_records, ("SOURCE_MANIFEST.sha256", manifest)]
    try:
        with temporary.open("xb") as raw:
            archive_identity = capture_descriptor_identity(raw.fileno())
            with zipfile.ZipFile(raw, "w") as archive:
                for relative, payload in records:
                    zip_bytes(archive, relative, payload)
            raw.flush()
            os.fsync(raw.fileno())
        archive_payload = read_single_link_file(
            temporary,
            MAX_ARCHIVE_BYTES,
            "Public source staging ZIP",
        )
        archive_details = temporary.lstat()
        if not archive_identity.matches_path(temporary, archive_details):
            raise RuntimeError("Public source staging ZIP changed identity.")
        archive_identity = capture_identity(
            temporary,
            bind_file=True,
            content_sha256=sha256_bytes(archive_payload),
        )
        if _verify_archive(temporary, records) != archive_payload:
            raise RuntimeError("Public source staging ZIP changed during verification.")
        if not archive_identity.matches_path(temporary):
            raise RuntimeError("Public source staging ZIP changed before publication.")
        with manifest_temporary.open("xb") as destination:
            manifest_identity = capture_descriptor_identity(destination.fileno())
            destination.write(manifest)
            destination.flush()
            os.fsync(destination.fileno())
        staged_manifest = read_single_link_file(
            manifest_temporary,
            MAX_TOTAL_BYTES,
            "Public source staging manifest",
        )
        if staged_manifest != manifest:
            raise RuntimeError("Public source staging manifest changed before publication.")
        manifest_details = manifest_temporary.lstat()
        if not manifest_identity.matches_path(manifest_temporary, manifest_details):
            raise RuntimeError("Public source staging manifest changed identity.")
        manifest_identity = capture_identity(
            manifest_temporary,
            bind_file=True,
            content_sha256=sha256_bytes(staged_manifest),
        )
        marker_bytes = _source_marker_bytes(
            candidate_authority=candidate_authority,
            candidate_evidence_sha256=candidate_evidence_sha256,
            git_commit=commit,
            archive_path=archive_path,
            archive_payload=archive_payload,
            manifest_path=manifest_path,
            manifest_payload=manifest,
            member_count=len(records),
        )
        with marker_temporary.open("xb") as destination:
            marker_identity = capture_descriptor_identity(destination.fileno())
            destination.write(marker_bytes)
            destination.flush()
            os.fsync(destination.fileno())
        staged_marker = read_single_link_file(
            marker_temporary,
            MAX_FILE_BYTES,
            "Public source staging marker",
        )
        if staged_marker != marker_bytes or not marker_identity.matches_path(marker_temporary):
            raise RuntimeError("Public source staging marker changed before publication.")
        marker_identity = capture_identity(
            marker_temporary,
            bind_file=True,
            content_sha256=sha256_bytes(staged_marker),
        )
        verify_source_archive_commit(
            marker_temporary,
            temporary,
            manifest_temporary,
            expected_commit=commit,
            archive_name=archive_path.name,
            manifest_name=manifest_path.name,
        )

        manifest_publication.publish(
            manifest_temporary,
            manifest_path,
            manifest_identity,
        )
        if os.path.lexists(marker_path):
            raise RuntimeError("Public source marker appeared before ZIP publication.")
        if (
            read_single_link_file(
                manifest_path,
                MAX_TOTAL_BYTES,
                "Published source manifest",
            )
            != manifest
        ):
            raise RuntimeError("Published source manifest changed before ZIP publication.")
        if _verify_archive(temporary, records) != archive_payload:
            raise RuntimeError("Public source staging ZIP changed before publication.")

        archive_publication.publish(temporary, archive_path, archive_identity)
        if os.path.lexists(marker_path):
            raise RuntimeError("Public source marker appeared before final verification.")
        verify_source_archive_commit(
            marker_temporary,
            archive_path,
            manifest_path,
            expected_commit=commit,
            archive_name=archive_path.name,
            manifest_name=manifest_path.name,
        )
        _assert_git_authority(root, git, commit)
        marker_publication.publish(marker_temporary, marker_path, marker_identity)
        return len(records), sha256_bytes(archive_payload)
    except BaseException:
        attempts = (manifest_publication, archive_publication, marker_publication)
        if all(attempt.cleanup_is_safe for attempt in attempts):
            cleanup_ok = True
            for stage, identity in (
                (temporary, archive_identity),
                (manifest_temporary, manifest_identity),
                (marker_temporary, marker_identity),
            ):
                if identity is not None:
                    cleanup_ok = unlink_owned_file_candidates((stage,), identity) and cleanup_ok
            if not cleanup_ok:
                active = sys.exception()
                message = "Public source cleanup lost ownership; unknown paths were preserved."
                if active is None:
                    raise RuntimeError(message)
                active.add_note(message)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the deterministic public source ZIP.")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--marker", type=Path, default=DEFAULT_MARKER)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="independently verify an existing ZIP, manifest, and commit marker",
    )
    args = parser.parse_args()
    if args.verify:
        git, commit, records = _canonical_git_payloads(ROOT)
        count, _marker_commit = verify_source_archive_commit(
            args.marker,
            args.archive,
            args.manifest,
            expected_commit=commit,
        )
        expected_manifest = (
            "\n".join(f"{sha256_bytes(payload)}  {relative}" for relative, payload in records)
            + "\n"
        ).encode("utf-8")
        if (
            read_single_link_file(
                args.manifest,
                MAX_TOTAL_BYTES,
                "Public source manifest",
            )
            != expected_manifest
        ):
            raise RuntimeError("Public source manifest does not match the authoritative Git tree.")
        _assert_git_authority(ROOT, git, commit)
        print(f"Verified {args.archive} with {count} members at Git commit {commit}")
        return 0
    count, digest = build_archive(
        args.archive,
        args.manifest,
        marker_path=args.marker,
    )
    print(f"Created {args.archive} with {count} members")
    print(f"SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
