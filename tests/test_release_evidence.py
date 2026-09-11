from __future__ import annotations

import re
import json
from pathlib import Path

import pytest

from scripts._release_evidence import (
    PRIVATE_POLICY_SCHEMA,
    PrivateContentPolicy,
    assert_public_payload_safe,
    canonical_json_bytes,
    load_private_content_policy,
    release_tool_authority,
    sha256_bytes,
    validate_release_evidence_index,
)
from tests._release_evidence_fixtures import write_valid_evidence_index


ROOT = Path(__file__).resolve().parent.parent


def test_release_tool_authority_inspects_the_current_tree() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", release_tool_authority(ROOT))


def test_bound_gate_receipt_round_trips_with_current_tool_authority(
    tmp_path: Path,
) -> None:
    index = write_valid_evidence_index(
        tmp_path,
        candidate_gates={"quality"},
    )
    validated = validate_release_evidence_index(
        tmp_path,
        index,
        release_version="1.0.0",
        required_candidate_gates={"quality"},
    )
    assert tuple(item["gate"] for item in validated.candidate_entries) == ("quality",)


CANARY = "synthetic-owner-recording-privacy-canary.flac"
SYNTHETIC_POLICY = PrivateContentPolicy("a" * 64, (CANARY,))


@pytest.mark.parametrize("payload", [
    f"VALUE = {CANARY!r}\n".encode(),
    f"VALUE = {CANARY[:20]!r} {CANARY[20:]!r}\n".encode(),
    f"VALUE = ({CANARY[:20]!r} + {CANARY[20:]!r})\n".encode(),
    f"VALUE = {CANARY[:20].encode()!r} {CANARY[20:].encode()!r}\n".encode(),
    ("VALUE = '" + "".join(f"\\x{ord(character):02x}" for character in CANARY) + "'\n")
    .encode(),
])
def test_private_policy_inspects_python_literal_semantics(payload: bytes) -> None:
    with pytest.raises(RuntimeError, match="contains private material") as caught:
        assert_public_payload_safe(
            "example.py", payload, context="Fixture", policy=SYNTHETIC_POLICY
        )
    assert CANARY not in str(caught.value)


def test_private_policy_inspects_json_escapes_and_member_names() -> None:
    escaped = "".join(f"\\u{ord(character):04x}" for character in CANARY)
    for relative, payload in (
        ("example.json", ('{"value":"' + escaped + '"}').encode()),
        (CANARY + ".txt", b"public content"),
    ):
        with pytest.raises(RuntimeError, match="contains private material"):
            assert_public_payload_safe(
                relative, payload, context="Fixture", policy=SYNTHETIC_POLICY
            )


@pytest.mark.parametrize("kind", ["windows", "home", "drvfs", "wsl-unc", "encoded", "utf16"])
def test_generic_private_path_forms_are_rejected(kind: str) -> None:
    windows = "/".join(("X:", "Users", "synthetic-owner", "recording.flac"))
    forms = {
        "windows": windows.replace("/", "\\"),
        "home": "/".join(("", "home", "synthetic-owner", "recording.flac")),
        "drvfs": "/".join(("", "mnt", "x", "synthetic-workspace", "recording.flac")),
        "wsl-unc": "\\".join(("", "", "wsl.localhost", "synthetic-distro", "tmp")),
        "encoded": windows.replace("/", "%2F"),
        "utf16": windows,
    }
    value = forms[kind]
    payload = value.encode("utf-16" if kind == "utf16" else "utf-8")
    with pytest.raises(RuntimeError, match="contains private material"):
        assert_public_payload_safe("example.txt", payload, context="Fixture")


def test_public_checksums_are_not_arbitrarily_private() -> None:
    public_digest = sha256_bytes(b"synthetic public upstream dependency")
    assert_public_payload_safe(
        "checksums.json", json.dumps({"sha256": public_digest}).encode(), context="Fixture"
    )


def test_external_policy_is_exact_hash_bound_and_must_stay_outside_source(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    payload = canonical_json_bytes({
        "schema": PRIVATE_POLICY_SCHEMA,
        "forbidden_literals": [CANARY],
    })
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(payload)
    loaded = load_private_content_policy(policy_path, root=root)
    assert loaded.raw_sha256 == sha256_bytes(payload)
    assert loaded.forbidden_literals == (CANARY,)
    assert CANARY not in repr(loaded)
    nested = root / "policy.json"
    nested.write_bytes(payload)
    with pytest.raises(RuntimeError, match="outside the source tree"):
        load_private_content_policy(nested, root=root)


@pytest.mark.parametrize("terms", [[], [""], ["  "], [True], [CANARY, CANARY.upper()]])
def test_private_policy_rejects_invalid_literals(tmp_path: Path, terms: object) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(canonical_json_bytes({
        "schema": PRIVATE_POLICY_SCHEMA, "forbidden_literals": terms,
    }))
    with pytest.raises(RuntimeError, match="Private content policy"):
        load_private_content_policy(policy_path, root=tmp_path / "source")


def test_privacy_tooling_contains_only_public_generic_rules_and_canaries() -> None:
    for relative in (
        "scripts/_release_evidence.py",
        "scripts/build_public_archive.py",
        "tests/test_release_evidence.py",
        "tests/test_build_public_archive.py",
    ):
        assert_public_payload_safe(
            relative, (ROOT / relative).read_bytes(), context="Public tooling"
        )
