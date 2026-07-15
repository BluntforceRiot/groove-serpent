from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Collection

from scripts._release_evidence import (
    AUTHORITY_SCHEMA,
    CANDIDATE_INDEX_NAME,
    GATE_RECEIPT_SCHEMA,
    INDEX_NAME,
    INDEX_SCHEMA,
    RELEASE_TOOL_PATHS,
    REQUIRED_GATE_ARTIFACTS,
    TOOL_AUTHORITY_SCHEMA,
    canonical_json_bytes,
    product_source_authority,
    public_release_commit_bytes,
    release_tool_authority,
)


TEST_CANDIDATE_AUTHORITY = hashlib.sha256(b"synthetic release candidate authority").hexdigest()
TEST_CANDIDATE_EVIDENCE = hashlib.sha256(b"synthetic candidate evidence").hexdigest()


def materialize_release_tools(root: Path) -> None:
    source_root = Path(__file__).resolve().parent.parent
    for relative in RELEASE_TOOL_PATHS:
        source = source_root.joinpath(*relative.split("/"))
        destination = root.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def write_valid_evidence_index(
    root: Path,
    *,
    candidate_gates: Collection[str],
    promotion_gates: Collection[str] = (),
    candidate_authority: str = TEST_CANDIDATE_AUTHORITY,
) -> Path:
    materialize_release_tools(root)
    evidence_root = root / "evidence"
    entries: dict[str, list[dict[str, object]]] = {
        "candidate": [],
        "promotion": [],
    }
    for phase, gates in (
        ("candidate", candidate_gates),
        ("promotion", promotion_gates),
    ):
        for gate in sorted(gates):
            command_output_path = evidence_root / "artifacts" / phase / f"{gate}.json"
            command_output_path.parent.mkdir(parents=True, exist_ok=True)
            command_output_payload = canonical_json_bytes(
                {
                    "gate": gate,
                    "result": "passed",
                    "schema": "groove-serpent.synthetic-test-evidence/1",
                }
            )
            command_output_path.write_bytes(command_output_payload)
            receipt_artifacts: list[dict[str, object]] = [
                {
                    "bytes": len(command_output_payload),
                    "kind": "command-output",
                    "path": command_output_path.relative_to(root).as_posix(),
                    "preexisting_sha256": None,
                    "sha256": hashlib.sha256(command_output_payload).hexdigest(),
                }
            ]
            for required_path, required_kind in REQUIRED_GATE_ARTIFACTS.get(gate, ()):
                authority_path = root.joinpath(*required_path.split("/"))
                authority_path.parent.mkdir(parents=True, exist_ok=True)
                if not authority_path.exists():
                    source = Path(__file__).resolve().parent.parent / required_path
                    authority_path.write_bytes(
                        source.read_bytes()
                        if source.is_file()
                        else f"synthetic authority input for {gate}\n".encode()
                    )
                authority_payload = authority_path.read_bytes()
                authority_digest = hashlib.sha256(authority_payload).hexdigest()
                receipt_artifacts.append(
                    {
                        "bytes": len(authority_payload),
                        "kind": required_kind,
                        "path": required_path,
                        "preexisting_sha256": authority_digest,
                        "sha256": authority_digest,
                    }
                )
            receipt_artifacts.sort(key=lambda item: str(item["path"]).casefold())
            path = evidence_root / phase / f"{gate}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = canonical_json_bytes(
                {
                    "artifacts": receipt_artifacts,
                    "candidate_authority": candidate_authority,
                    "command": {
                        "argv": ["synthetic-test", gate],
                        "exit_code": 0,
                    },
                    "environment": {
                        "os_name": "synthetic",
                        "platform": "unit-test",
                        "python_version": "3.13",
                    },
                    "gate": gate,
                    "release_version": "1.0.0",
                    "result": "passed",
                    "role": "promotion-evidence",
                    "schema": GATE_RECEIPT_SCHEMA,
                    "source_authority": {
                        "after": candidate_authority,
                        "before": candidate_authority,
                    },
                    "timing": {
                        "finished_utc": "2026-01-01T00:00:01Z",
                        "started_utc": "2026-01-01T00:00:00Z",
                    },
                    "tool_authority": {
                        "canonical_tree_sha256": release_tool_authority(root),
                        "schema": TOOL_AUTHORITY_SCHEMA,
                    },
                }
            )
            path.write_bytes(payload)
            entries[phase].append(
                {
                    "bytes": len(payload),
                    "candidate_authority": candidate_authority,
                    "gate": gate,
                    "path": path.relative_to(root).as_posix(),
                    "release_version": "1.0.0",
                    "result": "passed",
                    "role": "promotion-evidence",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
    shared = {
        "candidate_authority": {
            "canonical_tree_sha256": candidate_authority,
            "schema": AUTHORITY_SCHEMA,
        },
        "candidate_evidence": entries["candidate"],
        "release_version": "1.0.0",
        "schema": INDEX_SCHEMA,
    }
    (root / CANDIDATE_INDEX_NAME).write_bytes(
        canonical_json_bytes({**shared, "promotion_evidence": []})
    )
    index = root / INDEX_NAME
    index.write_bytes(canonical_json_bytes({**shared, "promotion_evidence": entries["promotion"]}))
    return index


def write_public_release_commit(root: Path) -> Path:
    marker = root / "PUBLIC_RELEASE_COMMIT.json"
    directories: set[str] = set()
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if ".git" in path.parts or path == marker:
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.add(relative)
            continue
        payload = path.read_bytes()
        entries.append(
            {
                "bytes": len(payload),
                "kind": "file",
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    entries.extend(
        {"bytes": 0, "kind": "directory", "path": path, "sha256": ""} for path in directories
    )
    entries.sort(key=lambda item: (str(item["path"]).casefold(), str(item["kind"])))
    authority = product_source_authority(
        [
            (str(item["path"]), int(item["bytes"]), str(item["sha256"]))
            for item in entries
            if item["kind"] == "file"
        ],
        release_version="1.0.0",
    )
    marker.write_bytes(
        public_release_commit_bytes(
            release_version="1.0.0",
            release_directory="groove-serpent-1.0.0",
            candidate_authority=authority,
            candidate_evidence_sha256=TEST_CANDIDATE_EVIDENCE,
            entries=entries,
        )
    )
    return marker


__all__ = [
    "TEST_CANDIDATE_AUTHORITY",
    "TEST_CANDIDATE_EVIDENCE",
    "materialize_release_tools",
    "write_public_release_commit",
    "write_valid_evidence_index",
]
