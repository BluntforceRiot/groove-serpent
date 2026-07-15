from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence

from scripts._release_fs import (
    PathIdentity,
    canonical_portable_relative_path,
    ensure_plain_ancestry,
    read_single_link_file,
    rename_no_replace,
)


INDEX_NAME = "RELEASE_EVIDENCE_INDEX_1.0.json"
CANDIDATE_INDEX_NAME = "RELEASE_EVIDENCE_CANDIDATE_1.0.json"
INDEX_SCHEMA = "groove-serpent.release-evidence-index/1"
AUTHORITY_SCHEMA = "groove-serpent.release-candidate-authority/1"
ACYCLIC_GENERATED_REPORT_PATHS = frozenset(
    {
        "BROWSER_ACCEPTANCE.md",
        "WINDOWS_PORTABLE_ACCEPTANCE_1.0.md",
    }
)
CANDIDATE_SECTION_SCHEMA = "groove-serpent.release-candidate-evidence/1"
PUBLIC_RELEASE_COMMIT_SCHEMA = "groove-serpent.public-release-commit/1"
PRODUCT_SOURCE_AUTHORITY_SCHEMA = "groove-serpent.selected-product-source/1"
GATE_RECEIPT_SCHEMA = "groove-serpent.release-gate-receipt/1"
TOOL_AUTHORITY_SCHEMA = "groove-serpent.release-evidence-tool-authority/1"
RELEASE_TOOL_PATHS = (
    "scripts/_release_evidence.py",
    "scripts/_release_fs.py",
    "scripts/build_public_release.py",
    "scripts/build_python_distributions.py",
    "scripts/build_release_evidence.py",
    "scripts/build_handoff.py",
    "tests/test_build_handoff.py",
    "tests/test_build_public_release.py",
    "tests/test_build_release_evidence.py",
    "tests/test_release_evidence.py",
)
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
GATE_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
REQUIRED_GATE_ARTIFACTS = {
    "capture-envelope": (
        ("evidence/harnesses/.codex-capture-envelope-acceptance.py", "authority-input"),
        ("evidence/harnesses/.codex-capture-v3-adversarial.py", "authority-input"),
    ),
    "n-drive-filesystem": (("scripts/accept_n_drive_filesystem.py", "authority-input"),),
    "real-corpus": (
        ("REAL_CORPUS_PROFILE_1.0.json", "authority-input"),
        ("scripts/run_real_corpus_acceptance.py", "authority-input"),
    ),
}
PUBLIC_FORBIDDEN_CONTENT_PATTERNS = (
    re.compile(
        rb"[A-Za-z]:(?:/+|\\+)Users(?:/+|\\+)[^\\/\r\n]+",
        re.IGNORECASE,
    ),
    re.compile(rb"[A-Za-z]:(?:/+|\\+)HomelabForge(?:/+|\\+)", re.IGNORECASE),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"sk-" rb"proj-[A-Za-z0-9_-]+"),
    re.compile(
        rb"AF079DFF63DFE4AE725F9197ACC9FBAE"
        rb"A65FECBFD4C2EA93E50864D01771281E",
        re.IGNORECASE,
    ),
    re.compile(
        rb"9EAEDFE0464CEC026A9484314FF48F59"
        rb"A87C8491A16C15C0A17640DE00C2523D",
        re.IGNORECASE,
    ),
    re.compile(
        rb"fc4e27f1413532c2e2c26c56c679af6"
        rb"514b3787c0806acebc357e1513b98a14f",
        re.IGNORECASE,
    ),
    re.compile(rb"(?:untitled|mystery)\.flac", re.IGNORECASE),
    re.compile(rb"the arcacia strain - you are safe from god here\.flac", re.IGNORECASE),
    re.compile(rb"whitechapel - the valley\.flac", re.IGNORECASE),
    re.compile(rb"larcenia roe - extraction\.flac", re.IGNORECASE),
    re.compile(
        rb"lorna shore - i feel the everblack festering within me\.flac",
        re.IGNORECASE,
    ),
)


class PublicationState(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    AMBIGUOUS = "AMBIGUOUS"
    COMMITTED = "COMMITTED"
    FAILED_KNOWN = "FAILED_KNOWN"


@dataclass
class PublicationAttempt:
    state: PublicationState = PublicationState.NOT_ATTEMPTED

    @property
    def cleanup_is_safe(self) -> bool:
        return self.state in {
            PublicationState.NOT_ATTEMPTED,
            PublicationState.FAILED_KNOWN,
        }

    def publish(
        self,
        source: Path,
        destination: Path,
        identity: PathIdentity,
    ) -> None:
        if self.state is not PublicationState.NOT_ATTEMPTED:
            raise RuntimeError("Publication was attempted more than once.")
        self.state = PublicationState.AMBIGUOUS
        try:
            rename_no_replace(source, destination)
        except BaseException:
            try:
                source_unchanged = identity.matches_path(source)
                destination_is_not_source = not identity.matches_path(destination)
            except OSError:
                source_unchanged = False
                destination_is_not_source = False
            if source_unchanged and destination_is_not_source:
                self.state = PublicationState.FAILED_KNOWN
            raise
        self.state = PublicationState.COMMITTED


@dataclass(frozen=True)
class EvidenceIndex:
    path: Path
    raw_sha256: str
    raw_bytes: int
    release_version: str
    candidate_authority: str
    candidate_evidence_sha256: str
    candidate_entries: tuple[Mapping[str, Any], ...]
    promotion_entries: tuple[Mapping[str, Any], ...]
    referenced_artifact_paths: tuple[str, ...]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def release_tool_authority(
    root: Path | None = None,
    payloads: Mapping[str, bytes] | None = None,
) -> str:
    """Bind receipts to the exact release runner and validator implementation."""

    repository_root = (
        Path(__file__).resolve().parent.parent
        if root is None
        else Path(os.path.abspath(os.fspath(root)))
    )
    records: list[dict[str, Any]] = []
    for relative in sorted(RELEASE_TOOL_PATHS, key=str.casefold):
        if payloads is None:
            tool_path = repository_root.joinpath(*relative.split("/"))
            ensure_plain_ancestry(
                tool_path,
                repository_root,
                f"Release evidence tool {relative}",
            )
            payload = read_single_link_file(
                tool_path,
                8 * 1024 * 1024,
                f"Release evidence tool {relative}",
            )
        else:
            try:
                payload = payloads[relative]
            except KeyError as exc:
                raise RuntimeError(f"Embedded release evidence tool is absent: {relative}") from exc
            if len(payload) > 8 * 1024 * 1024:
                raise RuntimeError(f"Embedded release evidence tool is too large: {relative}")
        records.append(
            {
                "bytes": len(payload),
                "path": relative,
                "sha256": sha256_bytes(payload),
            }
        )
    value = {"files": records, "schema": TOOL_AUTHORITY_SCHEMA}
    return sha256_bytes(canonical_json_bytes(value))


def assert_public_payload_safe(relative: str, payload: bytes, *, context: str) -> None:
    relative_payload = relative.encode("utf-8", errors="strict")
    if any(
        pattern.search(relative_payload) or pattern.search(payload)
        for pattern in PUBLIC_FORBIDDEN_CONTENT_PATTERNS
    ):
        raise RuntimeError(f"{context} contains private material.")


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is forbidden: {value}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_object(payload: bytes, context: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{context} is not strict canonical JSON.") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} must be a JSON object.")
    if canonical_json_bytes(value) != payload:
        raise RuntimeError(f"{context} is not canonically serialized.")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Collection[str], context: str) -> None:
    observed = set(value)
    required = set(expected)
    if observed != required:
        raise RuntimeError(
            f"{context} keys are invalid: missing={sorted(required - observed)!r}, "
            f"unexpected={sorted(observed - required)!r}."
        )


def require_sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise RuntimeError(f"{context} must be one lowercase SHA-256 digest.")
    return value


def require_text(value: Any, context: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise RuntimeError(f"{context} must be non-empty bounded text.")
    return value


def unique_sibling_path(destination: Path, purpose: str) -> Path:
    token = secrets.token_hex(16)
    return destination.with_name(f".{destination.name}.{purpose}.{token}.stage")


def _indexed_payload(
    root: Path,
    relative: str,
    *,
    context: str,
    payloads: Mapping[str, bytes] | None,
) -> bytes:
    if payloads is None:
        path = root.joinpath(*relative.split("/"))
        ensure_plain_ancestry(path, root, context)
        return read_single_link_file(path, MAX_EVIDENCE_BYTES, context)
    try:
        payload = payloads[relative]
    except KeyError as exc:
        raise RuntimeError(f"{context} is absent: {relative}") from exc
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise RuntimeError(f"{context} exceeds its byte ceiling: {relative}")
    return payload


def validate_gate_receipt(
    payload: bytes,
    *,
    receipt_path: str,
    root: Path,
    evidence_payloads: Mapping[str, bytes] | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    value = strict_json_object(payload, "Release gate receipt")
    _exact_keys(
        value,
        {
            "artifacts",
            "candidate_authority",
            "command",
            "environment",
            "gate",
            "release_version",
            "result",
            "role",
            "schema",
            "source_authority",
            "timing",
            "tool_authority",
        },
        "Release gate receipt",
    )
    if value["schema"] != GATE_RECEIPT_SCHEMA:
        raise RuntimeError("Release gate receipt schema is invalid.")
    gate = require_text(value["gate"], "Release gate receipt gate", maximum=64)
    if GATE_RE.fullmatch(gate) is None:
        raise RuntimeError("Release gate receipt gate is not canonical.")
    require_text(value["release_version"], "Receipt release version", maximum=64)
    authority = require_sha256(value["candidate_authority"], "Receipt authority")
    role = require_text(value["role"], "Receipt role", maximum=64)
    result = require_text(value["result"], "Receipt result", maximum=32)
    if (role, result) not in {
        ("promotion-evidence", "passed"),
        ("historical-evidence", "historical"),
    }:
        raise RuntimeError("Release gate receipt role/result classification is invalid.")

    source_authority = value["source_authority"]
    if not isinstance(source_authority, dict):
        raise RuntimeError("Receipt source authority must be an object.")
    _exact_keys(source_authority, {"after", "before"}, "Receipt source authority")
    if (
        require_sha256(source_authority["before"], "Receipt source authority before") != authority
        or require_sha256(source_authority["after"], "Receipt source authority after") != authority
    ):
        raise RuntimeError("Release gate receipt ran against a different source authority.")

    tool_authority = value["tool_authority"]
    if not isinstance(tool_authority, dict):
        raise RuntimeError("Receipt tool authority must be an object.")
    _exact_keys(
        tool_authority,
        {"canonical_tree_sha256", "schema"},
        "Receipt tool authority",
    )
    if tool_authority["schema"] != TOOL_AUTHORITY_SCHEMA:
        raise RuntimeError("Receipt tool authority schema is invalid.")
    if require_sha256(
        tool_authority["canonical_tree_sha256"],
        "Receipt tool authority",
    ) != release_tool_authority(root, evidence_payloads):
        raise RuntimeError("Release gate receipt was made by different release tooling.")

    command = value["command"]
    if not isinstance(command, dict):
        raise RuntimeError("Receipt command must be an object.")
    _exact_keys(command, {"argv", "exit_code"}, "Receipt command")
    argv = command["argv"]
    if not isinstance(argv, list) or not argv or len(argv) > 128:
        raise RuntimeError("Receipt command argv is invalid.")
    for argument in argv:
        require_text(argument, "Receipt command argument", maximum=4096)
    exit_code = command["exit_code"]
    if (
        not isinstance(exit_code, int)
        or isinstance(exit_code, bool)
        or not -(2**31) <= exit_code <= 2**32 - 1
    ):
        raise RuntimeError("Receipt command exit code is invalid.")
    if role == "promotion-evidence" and exit_code != 0:
        raise RuntimeError("Passed release gate receipt has a nonzero exit code.")

    timing = value["timing"]
    if not isinstance(timing, dict):
        raise RuntimeError("Receipt timing must be an object.")
    _exact_keys(timing, {"finished_utc", "started_utc"}, "Receipt timing")
    parsed_times: list[datetime] = []
    for key in ("started_utc", "finished_utc"):
        timestamp = require_text(timing[key], f"Receipt {key}", maximum=64)
        if not timestamp.endswith("Z"):
            raise RuntimeError("Receipt timestamps must be UTC Z timestamps.")
        try:
            parsed_times.append(datetime.fromisoformat(timestamp[:-1] + "+00:00"))
        except ValueError as exc:
            raise RuntimeError("Receipt timestamp is invalid.") from exc
    if parsed_times[1] < parsed_times[0]:
        raise RuntimeError("Receipt finish time precedes its start time.")

    environment = value["environment"]
    if not isinstance(environment, dict):
        raise RuntimeError("Receipt environment must be an object.")
    _exact_keys(
        environment,
        {"os_name", "platform", "python_version"},
        "Receipt environment",
    )
    for key in sorted(environment):
        require_text(environment[key], f"Receipt environment {key}", maximum=256)

    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or not artifacts or len(artifacts) > 256:
        raise RuntimeError("Release gate receipt artifacts are invalid.")
    artifact_paths: list[str] = []
    portable_paths: set[str] = set()
    _receipt_relative, receipt_portable = canonical_portable_relative_path(
        receipt_path,
        "Release gate receipt path",
    )
    index_portables = {
        canonical_portable_relative_path(name, "Release evidence index path")[1]
        for name in (INDEX_NAME, CANDIDATE_INDEX_NAME)
    }
    artifact_kinds: dict[str, str] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise RuntimeError("Receipt artifact must be an object.")
        _exact_keys(
            artifact,
            {"bytes", "kind", "path", "preexisting_sha256", "sha256"},
            "Receipt artifact",
        )
        supplied = require_text(artifact["path"], "Receipt artifact path", maximum=1024)
        relative, portable = canonical_portable_relative_path(
            supplied,
            "Receipt artifact path",
        )
        if relative != supplied or portable == receipt_portable or portable in index_portables:
            raise RuntimeError("Receipt artifact path is recursive or non-canonical.")
        if portable in portable_paths:
            raise RuntimeError("Receipt contains duplicate portable artifact paths.")
        portable_paths.add(portable)
        kind = require_text(artifact["kind"], "Receipt artifact kind", maximum=32)
        if kind not in {"authority-input", "command-output"}:
            raise RuntimeError("Receipt artifact kind is invalid.")
        size = artifact["bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RuntimeError("Receipt artifact byte count is invalid.")
        digest = require_sha256(artifact["sha256"], "Receipt artifact SHA-256")
        preexisting_digest = artifact["preexisting_sha256"]
        if kind == "authority-input":
            if (
                require_sha256(
                    preexisting_digest,
                    "Receipt authority-input preexisting SHA-256",
                )
                != digest
            ):
                raise RuntimeError("Receipt authority input changed during the gate run.")
        elif preexisting_digest is not None:
            raise RuntimeError("Receipt command output cannot claim a preexisting digest.")
        artifact_payload = _indexed_payload(
            root,
            relative,
            context="Receipt artifact",
            payloads=evidence_payloads,
        )
        if len(artifact_payload) != size or sha256_bytes(artifact_payload) != digest:
            raise RuntimeError(f"Receipt artifact bytes do not match: {relative}")
        artifact_paths.append(relative)
        artifact_kinds[relative] = kind
    if artifact_paths != sorted(artifact_paths, key=str.casefold):
        raise RuntimeError("Receipt artifact paths are not canonically ordered.")
    if role == "promotion-evidence" and "command-output" not in artifact_kinds.values():
        raise RuntimeError("Passed promotion evidence has no fresh command output.")
    for required_path, required_kind in REQUIRED_GATE_ARTIFACTS.get(gate, ()):
        if artifact_kinds.get(required_path) != required_kind:
            raise RuntimeError(
                "Release gate receipt omits or misclassifies its required artifact: "
                f"{required_path}"
            )
    return value, tuple(artifact_paths)


def _validate_entry(
    root: Path,
    supplied: Any,
    *,
    phase: str,
    release_version: str,
    candidate_authority: str,
    seen_paths: set[str],
    seen_portable: set[str],
    payloads: Mapping[str, bytes] | None,
    referenced_artifacts: set[str],
    referenced_artifact_portable: set[str],
) -> dict[str, Any]:
    if not isinstance(supplied, dict):
        raise RuntimeError(f"{phase} evidence entries must be JSON objects.")
    _exact_keys(
        supplied,
        {
            "bytes",
            "candidate_authority",
            "gate",
            "path",
            "release_version",
            "result",
            "role",
            "sha256",
        },
        f"{phase} evidence entry",
    )
    path_text = require_text(supplied["path"], f"{phase} evidence path", maximum=1024)
    canonical, portable = canonical_portable_relative_path(
        path_text,
        f"{phase} evidence path",
    )
    if canonical != path_text or canonical in {INDEX_NAME, CANDIDATE_INDEX_NAME}:
        raise RuntimeError(f"{phase} evidence path is not the exact canonical relative path.")
    if canonical in seen_paths or portable in seen_portable:
        raise RuntimeError(f"Duplicate evidence path: {canonical}")
    seen_paths.add(canonical)
    seen_portable.add(portable)

    gate = require_text(supplied["gate"], f"{phase} evidence gate", maximum=64)
    if GATE_RE.fullmatch(gate) is None:
        raise RuntimeError(f"{phase} evidence gate is not canonical.")
    role = require_text(supplied["role"], f"{phase} evidence role", maximum=64)
    result = require_text(supplied["result"], f"{phase} evidence result", maximum=32)
    if role == "promotion-evidence":
        if result != "passed":
            raise RuntimeError(f"Promotion evidence is not passed: {canonical}")
    elif role == "historical-evidence":
        if result != "historical":
            raise RuntimeError(f"Historical evidence is misclassified: {canonical}")
    else:
        raise RuntimeError(f"Evidence role is unclassified: {canonical}")
    if result in {"missing", "pending", "superseded"}:
        raise RuntimeError(f"Unresolved evidence is forbidden: {canonical}")
    if supplied["release_version"] != release_version:
        raise RuntimeError(f"Evidence release version is wrong: {canonical}")
    if supplied["candidate_authority"] != candidate_authority:
        raise RuntimeError(f"Evidence candidate authority is wrong: {canonical}")
    size = supplied["bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise RuntimeError(f"Evidence byte count is invalid: {canonical}")
    expected_sha = require_sha256(supplied["sha256"], f"Evidence SHA-256 for {canonical}")
    payload = _indexed_payload(
        root,
        canonical,
        context=f"{phase} evidence receipt",
        payloads=payloads,
    )
    if len(payload) != size or sha256_bytes(payload) != expected_sha:
        raise RuntimeError(f"Evidence bytes do not match the index: {canonical}")
    receipt, artifact_paths = validate_gate_receipt(
        payload,
        receipt_path=canonical,
        root=root,
        evidence_payloads=payloads,
    )
    for field in (
        "candidate_authority",
        "gate",
        "release_version",
        "result",
        "role",
    ):
        if receipt[field] != supplied[field]:
            raise RuntimeError(f"Evidence index relabels its gate receipt: {canonical}")
    for artifact_path in artifact_paths:
        _artifact_relative, artifact_portable = canonical_portable_relative_path(
            artifact_path,
            "Receipt artifact path",
        )
        if artifact_portable in referenced_artifact_portable:
            raise RuntimeError(
                f"Evidence artifact is credited by multiple receipts: {artifact_path}"
            )
        referenced_artifact_portable.add(artifact_portable)
        referenced_artifacts.add(artifact_path)
    return dict(supplied)


def _required_gates(
    entries: Sequence[Mapping[str, Any]],
    required: Collection[str],
    phase: str,
) -> None:
    promotion_gates = {
        str(entry["gate"])
        for entry in entries
        if entry["role"] == "promotion-evidence" and entry["result"] == "passed"
    }
    missing = set(required) - promotion_gates
    if missing:
        raise RuntimeError(f"{phase} promotion evidence is incomplete: {sorted(missing)!r}")


def release_evidence_index_bindings(
    payload: bytes,
    *,
    release_version: str,
) -> tuple[str, str]:
    """Return the authority and candidate-section digest bound by an index."""

    value = strict_json_object(payload, "Release evidence index")
    _exact_keys(
        value,
        {
            "candidate_authority",
            "candidate_evidence",
            "promotion_evidence",
            "release_version",
            "schema",
        },
        "Release evidence index",
    )
    if value["schema"] != INDEX_SCHEMA or value["release_version"] != release_version:
        raise RuntimeError("Release evidence index schema or release version is invalid.")
    authority = value["candidate_authority"]
    if not isinstance(authority, dict):
        raise RuntimeError("Release evidence candidate authority must be an object.")
    _exact_keys(
        authority,
        {"canonical_tree_sha256", "schema"},
        "Release evidence candidate authority",
    )
    if authority["schema"] != AUTHORITY_SCHEMA:
        raise RuntimeError("Release evidence candidate authority schema is invalid.")
    authority_sha = require_sha256(
        authority["canonical_tree_sha256"],
        "Release evidence candidate authority",
    )
    candidate = value["candidate_evidence"]
    if not isinstance(candidate, list) or not isinstance(value["promotion_evidence"], list):
        raise RuntimeError("Release evidence phases must be arrays.")
    candidate_section = {
        "candidate_authority": authority,
        "entries": candidate,
        "release_version": release_version,
        "schema": CANDIDATE_SECTION_SCHEMA,
    }
    return authority_sha, sha256_bytes(canonical_json_bytes(candidate_section))


def validate_release_evidence_index(
    root: Path,
    index_path: Path,
    *,
    release_version: str,
    required_candidate_gates: Collection[str],
    required_promotion_gates: Collection[str] = (),
    index_payload: bytes | None = None,
    evidence_payloads: Mapping[str, bytes] | None = None,
) -> EvidenceIndex:
    root = Path(os.path.abspath(os.fspath(root)))
    index_path = Path(os.path.abspath(os.fspath(index_path)))
    if index_path.name not in {INDEX_NAME, CANDIDATE_INDEX_NAME}:
        raise RuntimeError("Release evidence index must use a canonical candidate or final name.")
    if index_payload is None and index_path.parent != root:
        raise RuntimeError("Release evidence index must be at the evidence root.")
    payload = (
        read_single_link_file(index_path, MAX_INDEX_BYTES, "Release evidence index")
        if index_payload is None
        else index_payload
    )
    if len(payload) > MAX_INDEX_BYTES:
        raise RuntimeError("Release evidence index exceeds its byte ceiling.")
    value = strict_json_object(payload, "Release evidence index")
    _exact_keys(
        value,
        {
            "candidate_authority",
            "candidate_evidence",
            "promotion_evidence",
            "release_version",
            "schema",
        },
        "Release evidence index",
    )
    if value["schema"] != INDEX_SCHEMA or value["release_version"] != release_version:
        raise RuntimeError("Release evidence index schema or release version is invalid.")
    authority = value["candidate_authority"]
    if not isinstance(authority, dict):
        raise RuntimeError("Release evidence candidate authority must be an object.")
    _exact_keys(
        authority,
        {"canonical_tree_sha256", "schema"},
        "Release evidence candidate authority",
    )
    if authority["schema"] != AUTHORITY_SCHEMA:
        raise RuntimeError("Release evidence candidate authority schema is invalid.")
    authority_sha = require_sha256(
        authority["canonical_tree_sha256"],
        "Release evidence candidate authority",
    )
    candidate_raw = value["candidate_evidence"]
    promotion_raw = value["promotion_evidence"]
    if not isinstance(candidate_raw, list) or not isinstance(promotion_raw, list):
        raise RuntimeError("Release evidence phases must be arrays.")
    if index_path.name == CANDIDATE_INDEX_NAME and promotion_raw:
        raise RuntimeError("Candidate evidence index cannot contain promotion evidence.")
    seen_paths: set[str] = set()
    seen_portable: set[str] = set()
    referenced_artifacts: set[str] = set()
    referenced_artifact_portable: set[str] = set()
    candidate = tuple(
        _validate_entry(
            root,
            item,
            phase="Candidate",
            release_version=release_version,
            candidate_authority=authority_sha,
            seen_paths=seen_paths,
            seen_portable=seen_portable,
            payloads=evidence_payloads,
            referenced_artifacts=referenced_artifacts,
            referenced_artifact_portable=referenced_artifact_portable,
        )
        for item in candidate_raw
    )
    promotion = tuple(
        _validate_entry(
            root,
            item,
            phase="Promotion",
            release_version=release_version,
            candidate_authority=authority_sha,
            seen_paths=seen_paths,
            seen_portable=seen_portable,
            payloads=evidence_payloads,
            referenced_artifacts=referenced_artifacts,
            referenced_artifact_portable=referenced_artifact_portable,
        )
        for item in promotion_raw
    )
    for phase, entries in (("Candidate", candidate), ("Promotion", promotion)):
        ordered = sorted(entries, key=lambda item: (str(item["gate"]), str(item["path"])))
        if list(entries) != ordered:
            raise RuntimeError(f"{phase} evidence entries are not in canonical order.")
    recursive_artifacts = referenced_artifacts & seen_paths
    index_portables = {
        canonical_portable_relative_path(name, "Release evidence index path")[1]
        for name in (INDEX_NAME, CANDIDATE_INDEX_NAME)
    }
    if (
        recursive_artifacts
        or referenced_artifact_portable & seen_portable
        or index_portables & referenced_artifact_portable
    ):
        raise RuntimeError(
            "Evidence receipt artifacts recurse into receipts or the index: "
            f"{sorted(recursive_artifacts)!r}"
        )
    _required_gates(candidate, required_candidate_gates, "Candidate")
    _required_gates(promotion, required_promotion_gates, "Promotion")
    candidate_section = {
        "candidate_authority": authority,
        "entries": list(candidate),
        "release_version": release_version,
        "schema": CANDIDATE_SECTION_SCHEMA,
    }
    return EvidenceIndex(
        path=index_path,
        raw_sha256=sha256_bytes(payload),
        raw_bytes=len(payload),
        release_version=release_version,
        candidate_authority=authority_sha,
        candidate_evidence_sha256=sha256_bytes(canonical_json_bytes(candidate_section)),
        candidate_entries=candidate,
        promotion_entries=promotion,
        referenced_artifact_paths=tuple(sorted(referenced_artifacts, key=str.casefold)),
    )


def marker_artifact(path: Path, payload: bytes, **extra: Any) -> dict[str, Any]:
    return {
        "bytes": len(payload),
        "name": path.name,
        "sha256": sha256_bytes(payload),
        **extra,
    }


def product_source_authority(
    records: Sequence[tuple[str, int, str]],
    *,
    release_version: str,
) -> str:
    entries: list[dict[str, Any]] = []
    portable_paths: set[str] = set()
    for supplied, size, digest in records:
        relative, portable = canonical_portable_relative_path(
            supplied,
            "Selected product release path",
        )
        if portable in portable_paths:
            raise RuntimeError(f"Duplicate selected product release path: {relative}")
        portable_paths.add(portable)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RuntimeError(f"Selected product byte count is invalid: {relative}")
        entries.append(
            {
                "bytes": size,
                "path": relative,
                "sha256": require_sha256(
                    digest,
                    f"Selected product SHA-256 for {relative}",
                ),
            }
        )
    entries.sort(key=lambda item: str(item["path"]).casefold())
    return sha256_bytes(
        canonical_json_bytes(
            {
                "entries": entries,
                "release_version": require_text(
                    release_version,
                    "Selected product release version",
                    maximum=64,
                ),
                "schema": PRODUCT_SOURCE_AUTHORITY_SCHEMA,
            }
        )
    )


def canonical_payload_inventory(
    entries: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    paths: set[str] = set()
    portable_paths: set[str] = set()
    for supplied in entries:
        if not isinstance(supplied, dict):
            raise RuntimeError("Public payload inventory entries must be objects.")
        _exact_keys(
            supplied,
            {"bytes", "kind", "path", "sha256"},
            "Public payload inventory entry",
        )
        path = require_text(supplied["path"], "Public payload path", maximum=1024)
        canonical, portable = canonical_portable_relative_path(path, "Public payload path")
        if canonical != path or canonical in paths or portable in portable_paths:
            raise RuntimeError("Public payload inventory path is duplicate or non-canonical.")
        paths.add(canonical)
        portable_paths.add(portable)
        kind = supplied["kind"]
        size = supplied["bytes"]
        digest = supplied["sha256"]
        if kind == "directory":
            if size != 0 or digest != "":
                raise RuntimeError("Public directory inventory metadata is invalid.")
        elif kind == "file":
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise RuntimeError("Public file inventory byte count is invalid.")
            require_sha256(digest, f"Public payload SHA-256 for {canonical}")
        else:
            raise RuntimeError("Public payload inventory kind is invalid.")
        result.append(dict(supplied))
    ordered = sorted(result, key=lambda item: (str(item["path"]).casefold(), str(item["kind"])))
    if result != ordered:
        raise RuntimeError("Public payload inventory is not canonically ordered.")
    return tuple(result)


def public_release_commit_bytes(
    *,
    release_version: str,
    release_directory: str,
    candidate_authority: str,
    candidate_evidence_sha256: str,
    entries: Sequence[Mapping[str, Any]],
) -> bytes:
    inventory = canonical_payload_inventory(entries)
    files = [entry for entry in inventory if entry["kind"] == "file"]
    directories = [entry for entry in inventory if entry["kind"] == "directory"]
    inventory_value = {"entries": list(inventory)}
    value = {
        "candidate_authority": require_sha256(
            candidate_authority,
            "Public release candidate authority",
        ),
        "candidate_evidence_sha256": require_sha256(
            candidate_evidence_sha256,
            "Public release candidate evidence",
        ),
        "payload": {
            "directory_count": len(directories),
            "entries": list(inventory),
            "file_count": len(files),
            "inventory_sha256": sha256_bytes(canonical_json_bytes(inventory_value)),
            "total_bytes": sum(int(entry["bytes"]) for entry in files),
        },
        "release_directory": require_text(
            release_directory,
            "Public release directory",
            maximum=255,
        ),
        "release_version": require_text(
            release_version,
            "Public release version",
            maximum=64,
        ),
        "schema": PUBLIC_RELEASE_COMMIT_SCHEMA,
    }
    return canonical_json_bytes(value)


def validate_public_release_commit(
    payload: bytes,
    *,
    release_version: str,
    release_directory: str,
    entries: Sequence[Mapping[str, Any]],
) -> tuple[str, str]:
    value = strict_json_object(payload, "Public release commit")
    _exact_keys(
        value,
        {
            "candidate_authority",
            "candidate_evidence_sha256",
            "payload",
            "release_directory",
            "release_version",
            "schema",
        },
        "Public release commit",
    )
    if value["schema"] != PUBLIC_RELEASE_COMMIT_SCHEMA:
        raise RuntimeError("Public release commit schema is invalid.")
    authority = require_sha256(value["candidate_authority"], "Public release authority")
    evidence = require_sha256(
        value["candidate_evidence_sha256"],
        "Public release candidate evidence",
    )
    expected = public_release_commit_bytes(
        release_version=release_version,
        release_directory=release_directory,
        candidate_authority=authority,
        candidate_evidence_sha256=evidence,
        entries=entries,
    )
    if payload != expected:
        raise RuntimeError("Public release commit does not match the exact public payload.")
    return authority, evidence


def validate_marker_artifact(
    value: Any,
    path: Path,
    payload: bytes,
    *,
    context: str,
    extra_keys: Collection[str] = (),
    expected_name: str | None = None,
) -> None:
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} marker artifact must be an object.")
    _exact_keys(value, {"bytes", "name", "sha256", *extra_keys}, context)
    if (
        not isinstance(value["bytes"], int)
        or isinstance(value["bytes"], bool)
        or value["bytes"] < 0
        or value["name"] != (path.name if expected_name is None else expected_name)
        or value["bytes"] != len(payload)
        or value["sha256"] != sha256_bytes(payload)
    ):
        raise RuntimeError(f"{context} marker artifact does not match its payload.")


__all__ = [
    "AUTHORITY_SCHEMA",
    "ACYCLIC_GENERATED_REPORT_PATHS",
    "CANDIDATE_INDEX_NAME",
    "CANDIDATE_SECTION_SCHEMA",
    "EvidenceIndex",
    "GATE_RECEIPT_SCHEMA",
    "INDEX_NAME",
    "INDEX_SCHEMA",
    "RELEASE_TOOL_PATHS",
    "TOOL_AUTHORITY_SCHEMA",
    "PublicationAttempt",
    "PublicationState",
    "PUBLIC_RELEASE_COMMIT_SCHEMA",
    "canonical_json_bytes",
    "assert_public_payload_safe",
    "marker_artifact",
    "public_release_commit_bytes",
    "product_source_authority",
    "release_tool_authority",
    "release_evidence_index_bindings",
    "rename_no_replace",
    "require_sha256",
    "require_text",
    "sha256_bytes",
    "strict_json_object",
    "unique_sibling_path",
    "validate_gate_receipt",
    "validate_marker_artifact",
    "validate_public_release_commit",
    "validate_release_evidence_index",
]
