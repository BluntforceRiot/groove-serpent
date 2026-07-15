from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from groove_serpent import review_evidence
from groove_serpent.cli import main
from groove_serpent.review_evidence import (
    EVIDENCE_AUTHORITY,
    EVIDENCE_CATEGORIES,
    EXPORT_SCHEMA,
    OWNER_OUTCOMES,
    RECORD_SCHEMA,
    ReviewEvidenceError,
    append_review_evidence,
    build_review_evidence_export,
    delete_review_evidence,
    export_review_evidence,
    inspect_review_evidence,
    list_review_evidence,
    load_review_evidence_export,
    review_evidence_may_apply_action,
    review_evidence_may_authorize_action,
    set_review_evidence_enabled,
    validate_review_evidence_export,
    validate_review_evidence_record,
)


PROJECT_SHA = "1" * 64
STATE_SHA = "2" * 64
SOURCE_SHA = "3" * 64
TOOL_SHA = "4" * 64
CONFIG_SHA = "5" * 64


def evidence_record(
    *,
    category: str = "boundary",
    outcome: str = "accepted",
    recorded_at: str = "2026-07-13T08:10:00+00:00",
) -> dict[str, Any]:
    return {
        "schema": RECORD_SCHEMA,
        "authority": EVIDENCE_AUTHORITY,
        "category": category,
        "outcome": outcome,
        "recorded_at": recorded_at,
        "project": {
            "schema": "groove-serpent.project/4",
            "sha256": PROJECT_SHA,
            "editable_state_sha256": STATE_SHA,
            "source_sha256": SOURCE_SHA,
            "revision": 7,
        },
        "source": {
            "sha256": SOURCE_SHA,
            "size_bytes": 123_456,
            "sample_rate": 44_100,
            "channels": 2,
            "bits_per_sample": 24,
            "sample_count": 1_000_000,
        },
        "region": {
            "start_frame": 123_400,
            "end_frame_exclusive": 123_528,
            "channels": [0, 1],
        },
        "feature": {
            "schema": "groove-serpent.boundary-features/1",
            "tool": {
                "name": "groove-serpent",
                "version": "1.0.0",
                "sha256": TOOL_SHA,
            },
            "config": {
                "schema": "groove-serpent.boundary-config/1",
                "sha256": CONFIG_SHA,
            },
            "values": {"confidence": 0.91, "peak_ratio": 4.25},
        },
        "proposal": {
            "schema": "groove-serpent.boundary-proposal/1",
            "kind": "move-boundary",
            "payload": {"proposed_frame": 123_464, "confidence": 0.91},
        },
        "owner_result": {
            "outcome": outcome,
            "schema": "groove-serpent.owner-result/1",
            "payload": {"final_frame": 123_464, "decision": "reviewed"},
        },
    }


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def test_disabled_by_default_and_authority_is_permanently_false(tmp_path: Path) -> None:
    root = tmp_path / "private corpus"
    status = inspect_review_evidence(root)
    assert status.enabled is False
    assert status.configured is False
    assert status.record_count == 0
    assert not root.exists()
    assert review_evidence_may_authorize_action() is False
    assert review_evidence_may_apply_action() is False
    with pytest.raises(ReviewEvidenceError, match="disabled"):
        append_review_evidence(root, evidence_record())


@pytest.mark.parametrize("category", sorted(EVIDENCE_CATEGORIES))
@pytest.mark.parametrize("outcome", sorted(OWNER_OUTCOMES))
def test_all_categories_and_owner_outcomes_are_strictly_supported(
    category: str,
    outcome: str,
) -> None:
    validated = validate_review_evidence_record(
        evidence_record(category=category, outcome=outcome)
    )
    assert validated["category"] == category
    assert validated["outcome"] == outcome
    assert validated["owner_result"]["outcome"] == outcome


def test_append_is_content_addressed_immutable_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    first = append_review_evidence(root, evidence_record())
    second = append_review_evidence(root, evidence_record())
    assert first.record_sha256 == second.record_sha256
    assert first.path == second.path
    assert first.path.name == f"{first.record_sha256}.json"
    assert hashlib.sha256(first.path.read_bytes()).hexdigest() == first.record_sha256
    assert list_review_evidence(root) == (first,)

    detached = first.to_dict()
    detached["outcome"] = "rejected"
    assert first.to_dict()["outcome"] == "accepted"
    summary = first.summary_dict()
    summary["region"]["start_frame"] = 0
    assert first.summary_dict()["region"]["start_frame"] == 123_400


def test_disable_preserves_records_and_refuses_future_append(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    stored = append_review_evidence(root, evidence_record())
    status = set_review_evidence_enabled(root, False)
    assert status.enabled is False
    assert status.record_count == 1
    assert list_review_evidence(root)[0].record_sha256 == stored.record_sha256
    with pytest.raises(ReviewEvidenceError, match="disabled"):
        append_review_evidence(
            root,
            evidence_record(recorded_at="2026-07-13T08:11:00+00:00"),
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item.update({"authority": "may-approve"}), "never carry approval"),
        (lambda item: item.update({"unexpected": True}), "contain exactly"),
        (lambda item: item.update({"category": "unknown"}), "category"),
        (lambda item: item.update({"outcome": "maybe"}), "outcome"),
        (lambda item: item["project"].update({"revision": 0}), "revision"),
        (
            lambda item: item["owner_result"].update({"outcome": "rejected"}),
            "disagree",
        ),
        (lambda item: item["source"].update({"sha256": "6" * 64}), "disagree"),
        (lambda item: item["region"].update({"channels": [1, 0]}), "unique and sorted"),
        (lambda item: item["feature"].update({"values": []}), "must be an object"),
        (
            lambda item: item["feature"].update({"values": {"confidence": float("nan")}}),
            "non-finite",
        ),
        (
            lambda item: item["feature"].update({"values": {"count": 1 << 80}}),
            "out-of-range",
        ),
        (
            lambda item: item["feature"].update({"values": {"source_path": "relative"}}),
            "private or audio-bearing",
        ),
        (
            lambda item: item["feature"].update({"values": {"filepath": "relative"}}),
            "private or audio-bearing",
        ),
        (
            lambda item: item["feature"].update({"values": {"audio_blob": [1, 2]}}),
            "private or audio-bearing",
        ),
        (
            lambda item: item["feature"].update({"values": {"note": "C:\\Music\\side.flac"}}),
            "absolute path",
        ),
        (
            lambda item: item["proposal"].update({"payload": {"pcm": [1, 2, 3]}}),
            "private or audio-bearing",
        ),
    ],
)
def test_record_validation_fails_closed(
    mutate: Any,
    message: str,
) -> None:
    record = evidence_record()
    mutate(record)
    with pytest.raises(ReviewEvidenceError, match=message):
        validate_review_evidence_record(record)


def test_record_bounds_nesting_and_item_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    record = evidence_record()
    nested: dict[str, Any] = {}
    cursor = nested
    for _index in range(review_evidence.MAX_JSON_DEPTH + 2):
        child: dict[str, Any] = {}
        cursor["child"] = child
        cursor = child
    record["feature"]["values"] = nested
    with pytest.raises(ReviewEvidenceError, match="nesting depth"):
        validate_review_evidence_record(record)

    record = evidence_record()
    monkeypatch.setattr(review_evidence, "MAX_JSON_ITEMS", 2)
    with pytest.raises(ReviewEvidenceError, match="too many values"):
        validate_review_evidence_record(record)


def test_duplicate_noncanonical_and_reparse_settings_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "settings.json").write_text(
        '{"schema":"groove-serpent.review-evidence-settings/1",'
        '"authority":"evidence-only-never-approval","enabled":true,"enabled":false}\n',
        encoding="utf-8",
    )
    with pytest.raises(ReviewEvidenceError, match="duplicate-free"):
        inspect_review_evidence(root)
    with pytest.raises(ReviewEvidenceError, match="duplicate-free"):
        set_review_evidence_enabled(root, False)

    (root / "settings.json").write_text(
        json.dumps(
            {
                "schema": review_evidence.SETTINGS_SCHEMA,
                "authority": EVIDENCE_AUTHORITY,
                "enabled": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ReviewEvidenceError, match="not canonical"):
        inspect_review_evidence(root)

    (root / "settings.json").unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    try:
        (root / "settings.json").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this account")
    with pytest.raises(ReviewEvidenceError, match="symlinks"):
        inspect_review_evidence(root)


def test_tamper_collision_and_unexpected_entries_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    stored = append_review_evidence(root, evidence_record())
    stored.path.write_bytes(b"{}\n")
    with pytest.raises(ReviewEvidenceError, match="content hash disagree"):
        list_review_evidence(root)

    stored.path.unlink()
    (root / "records" / "notes.txt").write_text("ignored? no\n", encoding="utf-8")
    with pytest.raises(ReviewEvidenceError, match="unexpected entry"):
        list_review_evidence(root)


def test_duplicate_keys_in_record_file_fail_closed_after_hash_binding(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    records = root / "records"
    records.mkdir()
    raw = b'{"schema":"one","schema":"two"}\n'
    digest = hashlib.sha256(raw).hexdigest()
    (records / f"{digest}.json").write_bytes(raw)
    with pytest.raises(ReviewEvidenceError, match="duplicate-free"):
        list_review_evidence(root)


def test_record_symlink_is_rejected_without_following_it(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    stored = append_review_evidence(root, evidence_record())
    raw = stored.path.read_bytes()
    outside = tmp_path / "outside.json"
    outside.write_bytes(raw)
    stored.path.unlink()
    try:
        stored.path.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this account")
    with pytest.raises(ReviewEvidenceError, match="symlinks"):
        list_review_evidence(root)
    assert outside.read_bytes() == raw


def test_records_directory_substitution_is_detected_before_record_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    stored = append_review_evidence(root, evidence_record())
    records = root / "records"
    displaced = root / "records-displaced"
    real_iterdir = Path.iterdir

    def enumerate_then_substitute(path: Path) -> Any:
        entries = list(real_iterdir(path))
        if path == records:
            os.rename(records, displaced)
            records.mkdir()
        return iter(entries)

    monkeypatch.setattr(Path, "iterdir", enumerate_then_substitute)
    with pytest.raises(ReviewEvidenceError, match="changed during enumeration"):
        list_review_evidence(root)
    assert (displaced / stored.path.name).is_file()
    assert list(real_iterdir(records)) == []


def test_export_is_deterministic_private_and_completely_reopenable(tmp_path: Path) -> None:
    root = tmp_path / "private corpus"
    set_review_evidence_enabled(root, True)
    append_review_evidence(
        root,
        evidence_record(category="speed", outcome="adjusted"),
    )
    append_review_evidence(
        root,
        evidence_record(
            category="restoration",
            outcome="protected",
            recorded_at="2026-07-13T08:12:00+00:00",
        ),
    )
    payload = build_review_evidence_export(root)
    assert payload["schema"] == EXPORT_SCHEMA
    assert payload["authority"] == EVIDENCE_AUTHORITY
    hashes = [item["record_sha256"] for item in payload["records"]]
    assert hashes == sorted(hashes)
    assert validate_review_evidence_export(payload) == payload

    first_path = tmp_path / "first-export.json"
    second_path = tmp_path / "second-export.json"
    first_hash = export_review_evidence(root, first_path)
    second_hash = export_review_evidence(root, second_path)
    assert first_hash == second_hash
    assert first_path.read_bytes() == second_path.read_bytes()
    assert load_review_evidence_export(first_path) == payload
    rendered = first_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in rendered
    assert '"path"' not in rendered
    assert '"secret"' not in rendered
    assert '"audio"' not in rendered
    assert not any("path" in entry for entry in payload["records"])

    tampered = json.loads(first_path.read_text(encoding="utf-8"))
    tampered["records"][0]["record"]["outcome"] = "accepted"
    tampered["records"][0]["record"]["owner_result"]["outcome"] = "accepted"
    with pytest.raises(ReviewEvidenceError, match="identity does not match"):
        validate_review_evidence_export(tampered)


def test_export_refuses_existing_destination_and_symlink_parent(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    output = tmp_path / "export.json"
    output.write_text("keep\n", encoding="utf-8")
    with pytest.raises(ReviewEvidenceError, match="must not exist"):
        export_review_evidence(root, output)
    assert output.read_text(encoding="utf-8") == "keep\n"

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable for this account")
    with pytest.raises(ReviewEvidenceError, match="symlinks"):
        export_review_evidence(root, linked / "export.json")


def test_delete_requires_deliberate_exact_hash_and_remains_path_safe(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    stored = append_review_evidence(root, evidence_record())
    with pytest.raises(ReviewEvidenceError, match="deliberate"):
        delete_review_evidence(
            root,
            stored.record_sha256,
            expected_record_sha256=stored.record_sha256,
            deliberate=False,
        )
    with pytest.raises(ReviewEvidenceError, match="must match"):
        delete_review_evidence(
            root,
            stored.record_sha256,
            expected_record_sha256="f" * 64,
            deliberate=True,
        )
    with pytest.raises(ReviewEvidenceError, match="lowercase SHA-256"):
        delete_review_evidence(
            root,
            "../settings.json",
            expected_record_sha256="f" * 64,
            deliberate=True,
        )
    deleted = delete_review_evidence(
        root,
        stored.record_sha256,
        expected_record_sha256=stored.record_sha256,
        deliberate=True,
    )
    assert deleted.record_sha256 == stored.record_sha256
    assert list_review_evidence(root) == ()


def test_delete_quarantines_substitution_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    stored = append_review_evidence(root, evidence_record())
    original_raw = stored.path.read_bytes()
    attacker_raw = b"substituted regular file\n"
    stolen = root / "stolen-original.json"
    real_rename = review_evidence._atomic_no_replace_rename

    def substitute_then_rename(source: Path, destination: Path) -> None:
        os.rename(source, stolen)
        source.write_bytes(attacker_raw)
        real_rename(source, destination)

    monkeypatch.setattr(
        review_evidence,
        "_atomic_no_replace_rename",
        substitute_then_rename,
    )
    with pytest.raises(ReviewEvidenceError, match="substituted during deletion"):
        delete_review_evidence(
            root,
            stored.record_sha256,
            expected_record_sha256=stored.record_sha256,
            deliberate=True,
        )
    assert stolen.read_bytes() == original_raw
    quarantines = list((root / "records").glob("*.delete-quarantine"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == attacker_raw


def test_record_limit_is_enforced_before_another_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "corpus"
    set_review_evidence_enabled(root, True)
    append_review_evidence(root, evidence_record())
    monkeypatch.setattr(review_evidence, "MAX_RECORDS", 1)
    with pytest.raises(ReviewEvidenceError, match="record limit"):
        append_review_evidence(
            root,
            evidence_record(recorded_at="2026-07-13T08:13:00+00:00"),
        )


def test_evidence_cli_status_toggle_list_export_and_delete(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "CLI corpus"
    assert main(["evidence", "status", "--root", str(root), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["enabled"] is False
    assert status["may_authorize_action"] is False

    assert main(["evidence", "enable", "--root", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True
    stored = append_review_evidence(root, evidence_record())

    assert main(["evidence", "list", "--root", str(root), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["record_sha256"] for item in listed] == [stored.record_sha256]
    assert "path" not in listed[0]

    output = tmp_path / "cli-export.json"
    assert (
        main(
            [
                "evidence",
                "export",
                str(output),
                "--root",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    exported = json.loads(capsys.readouterr().out)
    assert exported["record_count"] == 1
    assert exported["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()

    assert (
        main(
            [
                "evidence",
                "delete",
                stored.record_sha256,
                "--expected-record-sha256",
                stored.record_sha256,
                "--yes",
                "--root",
                str(root),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["deleted"] is True
    assert main(["evidence", "disable", "--root", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is False


def test_environment_default_is_local_and_does_not_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "configured corpus"
    monkeypatch.setenv("GROOVE_SERPENT_REVIEW_EVIDENCE_DIR", os.fspath(configured))
    assert review_evidence.default_review_evidence_root() == configured.absolute()
    assert not configured.exists()
