from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from groove_serpent.cli import main
from groove_serpent.review_evidence import (
    EVIDENCE_AUTHORITY,
    EXPORT_SCHEMA,
    RECORD_SCHEMA,
    ReviewEvidenceError,
    validate_review_evidence_export,
)
from groove_serpent.review_evidence_evaluation import (
    EVALUATION_AUTHORITY,
    EVALUATION_SCHEMA,
    SOURCE_SPLIT_SCHEMA,
    ConfigComparison,
    EvaluationConfig,
    ReviewEvidenceEvaluationError,
    build_review_evidence_evaluation,
    evaluate_review_evidence_export,
    evaluation_may_apply_action,
    evaluation_may_authorize_action,
    evaluation_may_change_defaults,
    load_review_evidence_evaluation,
    validate_review_evidence_evaluation,
    write_review_evidence_evaluation,
)


CONFIG_A = "a" * 64
CONFIG_B = "b" * 64
TOOL_SHA = "c" * 64
STATE_SHA = "d" * 64
MBID_A = "11111111-1111-1111-1111-111111111111"
MBID_B = "22222222-2222-2222-2222-222222222222"


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


def evaluation_config(**overrides: Any) -> EvaluationConfig:
    values = {
        "split_salt_sha256": "e" * 64,
        "evaluation_basis_points": 9_999,
        "minimum_metric_records": 3,
        "minimum_metric_sources": 2,
        "minimum_paired_benchmarks": 3,
        "minimum_paired_sources": 2,
        "minimum_pair_coverage_basis_points": 8_000,
    }
    values.update(overrides)
    return EvaluationConfig(**values)


def split_for(source_sha256: str, config: EvaluationConfig) -> str:
    material = (
        SOURCE_SPLIT_SCHEMA
        + "\x00"
        + config.split_salt_sha256
        + "\x00"
        + source_sha256
    ).encode("ascii")
    bucket = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % 10_000
    return "evaluation" if bucket < config.evaluation_basis_points else "development"


def sources_for_split(
    split: str, count: int, config: EvaluationConfig
) -> list[str]:
    result = []
    index = 1
    while len(result) < count:
        candidate = f"{index:064x}"
        if split_for(candidate, config) == split:
            result.append(candidate)
        index += 1
    return result


def evidence_record(
    *,
    source_sha256: str,
    config_sha256: str = CONFIG_A,
    category: str = "boundary",
    outcome: str = "adjusted",
    start_frame: int = 1_000,
    proposal_kind: str | None = None,
    proposed: Any = 1_010,
    final: Any = 1_000,
    recorded_at: str = "2026-07-13T12:00:00+00:00",
) -> dict[str, Any]:
    if category in {"boundary", "endpoint"}:
        proposal_payload = {"proposed_frame": proposed}
        owner_payload = {"final_frame": final, "decision": "reviewed"}
    elif category == "speed":
        proposal_payload = {"proposed_factor": proposed}
        owner_payload = {"final_factor": final, "decision": "reviewed"}
    elif category == "recognition":
        proposal_payload = {"release_mbid": proposed}
        owner_payload = {"release_mbid": final, "decision": "reviewed"}
    else:
        proposal_payload = {"event_count": proposed}
        owner_payload = {"reviewed_event_count": final, "decision": "reviewed"}
    kind = proposal_kind or {
        "boundary": "move-boundary",
        "endpoint": "trim-endpoint",
        "speed": "correct-speed",
        "recognition": "identify-release",
        "restoration": "repair-click",
        "structural-event": "classify-needle-event",
    }[category]
    return {
        "schema": RECORD_SCHEMA,
        "authority": EVIDENCE_AUTHORITY,
        "category": category,
        "outcome": outcome,
        "recorded_at": recorded_at,
        "project": {
            "schema": "groove-serpent.project/4",
            "sha256": hashlib.sha256(
                f"project:{source_sha256}:{start_frame}".encode("ascii")
            ).hexdigest(),
            "editable_state_sha256": STATE_SHA,
            "source_sha256": source_sha256,
            "revision": 7,
        },
        "source": {
            "sha256": source_sha256,
            "size_bytes": 123_456,
            "sample_rate": 44_100,
            "channels": 2,
            "bits_per_sample": 24,
            "sample_count": 1_000_000,
        },
        "region": {
            "start_frame": start_frame,
            "end_frame_exclusive": start_frame + 128,
            "channels": [0, 1],
        },
        "feature": {
            "schema": f"groove-serpent.{category}-features/1",
            "tool": {
                "name": "groove-serpent",
                "version": "1.0.0",
                "sha256": TOOL_SHA,
            },
            "config": {
                "schema": f"groove-serpent.{category}-config/1",
                "sha256": config_sha256,
            },
            "values": {"confidence": 0.9},
        },
        "proposal": {
            "schema": f"groove-serpent.{category}-proposal/1",
            "kind": kind,
            "payload": proposal_payload,
        },
        "owner_result": {
            "outcome": outcome,
            "schema": "groove-serpent.owner-result/1",
            "payload": owner_payload,
        },
    }


def export_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    entries = []
    for record in records:
        record_sha256 = hashlib.sha256(canonical_bytes(record)).hexdigest()
        entries.append({"record_sha256": record_sha256, "record": record})
    payload = {
        "schema": EXPORT_SCHEMA,
        "authority": EVIDENCE_AUTHORITY,
        "record_count": len(entries),
        "records": sorted(entries, key=lambda item: item["record_sha256"]),
    }
    return validate_review_evidence_export(payload)


def write_export(path: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    payload = export_payload(records)
    path.write_bytes(canonical_bytes(payload))
    return payload


def metric(receipt: dict[str, Any], category: str) -> dict[str, Any]:
    return next(item for item in receipt["metrics"] if item["category"] == category)


def comparison_metric(receipt: dict[str, Any], category: str) -> dict[str, Any]:
    comparison = receipt["comparison"]
    assert comparison is not None
    return next(item for item in comparison["metrics"] if item["category"] == category)


def test_evaluation_is_deterministic_bound_private_and_non_authoritative(
    tmp_path: Path,
) -> None:
    config = evaluation_config()
    source_a, source_b = sources_for_split("evaluation", 2, config)
    records = [
        evidence_record(
            source_sha256=source_a,
            start_frame=1_000,
            proposed=1_000,
            final=1_000,
        ),
        evidence_record(
            source_sha256=source_a,
            start_frame=2_000,
            proposed=2_010,
            final=2_000,
        ),
        evidence_record(
            source_sha256=source_b,
            start_frame=3_000,
            proposed=3_005,
            final=3_000,
        ),
        evidence_record(
            source_sha256=source_a,
            category="endpoint",
            start_frame=3_200,
            proposed=3_204,
            final=3_200,
        ),
        evidence_record(
            source_sha256=source_a,
            category="endpoint",
            start_frame=3_400,
            proposed=3_408,
            final=3_400,
        ),
        evidence_record(
            source_sha256=source_b,
            category="endpoint",
            start_frame=3_600,
            proposed=3_600,
            final=3_600,
        ),
        evidence_record(
            source_sha256=source_a,
            category="speed",
            start_frame=4_000,
            proposed=1.01,
            final=1.0,
        ),
        evidence_record(
            source_sha256=source_a,
            category="speed",
            start_frame=5_000,
            proposed=1.02,
            final=1.0,
        ),
        evidence_record(
            source_sha256=source_b,
            category="speed",
            start_frame=6_000,
            proposed=1.0,
            final=1.0,
        ),
        evidence_record(
            source_sha256=source_a,
            category="recognition",
            start_frame=7_000,
            proposed=MBID_A,
            final=MBID_A,
        ),
        evidence_record(
            source_sha256=source_a,
            category="recognition",
            start_frame=8_000,
            proposed=MBID_B,
            final=MBID_A,
        ),
        evidence_record(
            source_sha256=source_b,
            category="recognition",
            start_frame=9_000,
            proposed=MBID_A,
            final=MBID_A,
        ),
        evidence_record(
            source_sha256=source_a,
            category="restoration",
            start_frame=10_000,
            proposed=2,
            final=1,
        ),
        evidence_record(
            source_sha256=source_b,
            category="structural-event",
            start_frame=11_000,
            proposed=1,
            final=1,
        ),
    ]
    export_path = tmp_path / "synthetic-export.json"
    payload = write_export(export_path, records)

    first = evaluate_review_evidence_export(export_path, config=config)
    second = evaluate_review_evidence_export(export_path, config=config)
    assert first == second
    assert first["schema"] == EVALUATION_SCHEMA
    assert first["authority"] == EVALUATION_AUTHORITY
    assert first["corpus_export"]["sha256"] == hashlib.sha256(
        canonical_bytes(payload)
    ).hexdigest()
    assert first["evaluator"]["module_sha256"]
    assert first["evaluator"]["application"]["identity_sha256"]
    assert first["evaluator"]["config"]["sha256"]
    assert first["may_authorize_action"] is False
    assert first["may_apply_action"] is False
    assert first["may_change_defaults"] is False
    assert evaluation_may_authorize_action() is False
    assert evaluation_may_apply_action() is False
    assert evaluation_may_change_defaults() is False

    boundary = metric(first, "boundary")
    assert boundary["data_sufficient"] is True
    assert boundary["values"] == {
        "mean_absolute_error_frames": 5.0,
        "median_absolute_error_frames": 5.0,
        "maximum_absolute_error_frames": 10,
        "exact_frame_match_count": 1,
    }
    endpoint = metric(first, "endpoint")
    assert endpoint["data_sufficient"] is True
    assert endpoint["values"]["maximum_absolute_error_frames"] == 8
    speed = metric(first, "speed")
    assert speed["data_sufficient"] is True
    assert speed["values"]["mean_absolute_error_ppm"] == "10000.000"
    recognition = metric(first, "recognition")
    assert recognition["values"]["exact_release_match_count"] == 2
    category_coverage = {
        item["category"]: item for item in first["coverage"]["categories"]
    }
    assert category_coverage["restoration"]["metric_policy"] == (
        "coverage-and-outcomes-only"
    )
    assert category_coverage["structural-event"]["record_count"] == 1
    assert len(first["coverage"]["tools"]) == 1
    # The same config digest under six category-specific schemas remains six
    # exact identities rather than being silently conflated by digest alone.
    assert len(first["coverage"]["configs"]) == 6
    assert len(first["coverage"]["proposal_kinds"]) == 6
    rendered = json.dumps(first, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert source_a not in rendered
    assert source_b not in rendered
    assert '"path"' not in rendered
    assert '"audio"' not in rendered
    assert first["interpretation_limits"][0] == (
        "acceptance-rate-is-not-a-quality-metric"
    )


def test_source_groups_never_leak_across_splits_and_tamper_is_recomputed(
    tmp_path: Path,
) -> None:
    config = evaluation_config(evaluation_basis_points=5_000)
    evaluation_source = sources_for_split("evaluation", 1, config)[0]
    development_source = sources_for_split("development", 1, config)[0]
    records = [
        evidence_record(
            source_sha256=evaluation_source,
            start_frame=1_000 + index * 200,
        )
        for index in range(3)
    ] + [
        evidence_record(
            source_sha256=development_source,
            start_frame=5_000 + index * 200,
        )
        for index in range(4)
    ]
    export_path = tmp_path / "export.json"
    write_export(export_path, records)
    receipt = evaluate_review_evidence_export(export_path, config=config)
    groups = {item["split"]: item for item in receipt["source_split"]["groups"]}
    assert groups["evaluation"]["source_count"] == 1
    assert groups["evaluation"]["record_count"] == 3
    assert groups["development"]["source_count"] == 1
    assert groups["development"]["record_count"] == 4
    assert receipt["source_split"]["source_groups_disjoint"] is True
    assert receipt["source_split"]["record_level_splitting_permitted"] is False

    tampered = copy.deepcopy(receipt)
    tampered["source_split"]["source_groups_disjoint"] = False
    with pytest.raises(ReviewEvidenceEvaluationError, match="recomputation"):
        validate_review_evidence_evaluation(tampered, export_path)


def test_insufficient_and_imbalanced_data_abstain_explicitly(tmp_path: Path) -> None:
    config = evaluation_config()
    source_a, source_b = sources_for_split("evaluation", 2, config)
    records = [
        evidence_record(
            source_sha256=source_a,
            config_sha256=CONFIG_A,
            start_frame=1_000,
            proposed=1_020,
            final=1_000,
        ),
        evidence_record(
            source_sha256=source_a,
            config_sha256=CONFIG_A,
            start_frame=2_000,
            proposed=2_020,
            final=2_000,
        ),
        evidence_record(
            source_sha256=source_b,
            config_sha256=CONFIG_A,
            start_frame=3_000,
            proposed=3_020,
            final=3_000,
        ),
        evidence_record(
            source_sha256=source_a,
            config_sha256=CONFIG_B,
            start_frame=1_000,
            proposed=1_010,
            final=1_000,
        ),
    ]
    export_path = tmp_path / "imbalanced.json"
    write_export(export_path, records)
    receipt = evaluate_review_evidence_export(
        export_path,
        config=config,
        comparison=ConfigComparison(CONFIG_A, CONFIG_B),
    )
    compared = comparison_metric(receipt, "boundary")
    assert compared["abstained"] is True
    assert compared["paired_benchmark_count"] == 1
    assert "insufficient_paired_typed_benchmarks" in compared["abstention_reasons"]
    assert "insufficient_paired_sources" in compared["abstention_reasons"]
    assert "imbalanced_or_unpaired_config_coverage" in compared["abstention_reasons"]
    assert compared["values"] is None
    assert receipt["comparison"]["acceptance_rate_used_as_quality_metric"] is False
    assert receipt["comparison"]["causal_claim_permitted"] is False

    endpoint = metric(receipt, "endpoint")
    assert endpoint["abstained"] is True
    assert endpoint["values"] is None
    assert endpoint["abstention_reasons"] == [
        "insufficient_typed_evaluation_records",
        "insufficient_typed_evaluation_sources",
    ]


def test_paired_comparison_uses_only_identical_benchmark_keys(tmp_path: Path) -> None:
    config = evaluation_config()
    source_a, source_b = sources_for_split("evaluation", 2, config)
    records = []
    for index, source in enumerate((source_a, source_a, source_b), start=1):
        start = index * 1_000
        records.append(
            evidence_record(
                source_sha256=source,
                config_sha256=CONFIG_A,
                start_frame=start,
                proposed=start + 20,
                final=start,
            )
        )
        records.append(
            evidence_record(
                source_sha256=source,
                config_sha256=CONFIG_B,
                start_frame=start,
                proposed=start + 5,
                final=start,
            )
        )
    export_path = tmp_path / "paired.json"
    write_export(export_path, records)
    receipt = evaluate_review_evidence_export(
        export_path,
        config=config,
        comparison=ConfigComparison(CONFIG_A, CONFIG_B),
    )
    compared = comparison_metric(receipt, "boundary")
    assert compared["data_sufficient"] is True
    assert compared["paired_benchmark_count"] == 3
    assert compared["typed_pair_count"] == 3
    assert compared["paired_source_count"] == 2
    assert compared["baseline_pair_coverage_basis_points"] == 10_000
    assert compared["candidate_pair_coverage_basis_points"] == 10_000
    assert compared["values"]["candidate_lower_error_count"] == 3
    assert compared["values"]["candidate_minus_baseline_mean_loss"] == (
        "-15.000000000000"
    )
    assert compared["values"]["interpretation"] == (
        "descriptive-paired-error-only-not-causal"
    )
    structural = comparison_metric(receipt, "structural-event")
    assert structural["abstention_reasons"] == [
        "no_defensible_typed_target_adapter"
    ]


def test_comparison_with_no_identical_benchmark_keys_abstains(tmp_path: Path) -> None:
    config = evaluation_config(
        minimum_paired_benchmarks=1,
        minimum_paired_sources=1,
        minimum_pair_coverage_basis_points=1,
    )
    source = sources_for_split("evaluation", 1, config)[0]
    export_path = tmp_path / "unpaired.json"
    write_export(
        export_path,
        [
            evidence_record(
                source_sha256=source,
                config_sha256=CONFIG_A,
                start_frame=1_000,
            ),
            evidence_record(
                source_sha256=source,
                config_sha256=CONFIG_B,
                start_frame=2_000,
            ),
        ],
    )
    receipt = evaluate_review_evidence_export(
        export_path,
        config=config,
        comparison=ConfigComparison(CONFIG_A, CONFIG_B),
    )
    compared = comparison_metric(receipt, "boundary")
    assert compared["paired_benchmark_count"] == 0
    assert compared["typed_pair_count"] == 0
    assert compared["abstained"] is True
    assert "insufficient_paired_typed_benchmarks" in compared["abstention_reasons"]
    assert "imbalanced_or_unpaired_config_coverage" in compared["abstention_reasons"]


def test_target_mismatch_and_missing_recognition_mbid_abstain(tmp_path: Path) -> None:
    config = evaluation_config(
        minimum_metric_records=1,
        minimum_metric_sources=1,
        minimum_paired_benchmarks=1,
        minimum_paired_sources=1,
        minimum_pair_coverage_basis_points=1,
    )
    source = sources_for_split("evaluation", 1, config)[0]
    baseline = evidence_record(
        source_sha256=source,
        config_sha256=CONFIG_A,
        proposed=1_010,
        final=1_000,
    )
    candidate = evidence_record(
        source_sha256=source,
        config_sha256=CONFIG_B,
        proposed=1_005,
        final=999,
    )
    recognition = evidence_record(
        source_sha256=source,
        category="recognition",
        start_frame=2_000,
        proposed=MBID_A,
        final=MBID_A,
    )
    del recognition["owner_result"]["payload"]["release_mbid"]
    export_path = tmp_path / "targets.json"
    write_export(export_path, [baseline, candidate, recognition])
    receipt = evaluate_review_evidence_export(
        export_path,
        config=config,
        comparison=ConfigComparison(CONFIG_A, CONFIG_B),
    )
    compared = comparison_metric(receipt, "boundary")
    assert compared["owner_target_mismatch_count"] == 1
    assert compared["typed_pair_count"] == 0
    assert "owner_target_mismatch" in compared["abstention_reasons"]
    recognition_metric = metric(receipt, "recognition")
    assert recognition_metric["eligible_record_count"] == 0
    assert recognition_metric["ineligible_record_count"] == 1
    assert recognition_metric["abstained"] is True


def test_export_tamper_nonfinite_and_duplicate_benchmarks_fail_closed(
    tmp_path: Path,
) -> None:
    config = evaluation_config(minimum_metric_records=1, minimum_metric_sources=1)
    source = sources_for_split("evaluation", 1, config)[0]
    record = evidence_record(source_sha256=source)
    payload = export_payload([record])

    tampered = copy.deepcopy(payload)
    tampered["records"][0]["record"]["proposal"]["payload"]["proposed_frame"] += 1
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_bytes(canonical_bytes(tampered))
    with pytest.raises(ReviewEvidenceError, match="identity does not match"):
        evaluate_review_evidence_export(tampered_path, config=config)

    nonfinite = copy.deepcopy(payload)
    nonfinite["records"][0]["record"]["feature"]["values"]["confidence"] = float(
        "nan"
    )
    with pytest.raises(ReviewEvidenceError, match="non-finite"):
        build_review_evidence_evaluation(nonfinite, config=config)

    duplicate = evidence_record(
        source_sha256=source,
        outcome="rejected",
        recorded_at="2026-07-13T12:01:00+00:00",
    )
    duplicate_payload = export_payload([record, duplicate])
    with pytest.raises(ReviewEvidenceEvaluationError, match="duplicate benchmark"):
        build_review_evidence_evaluation(duplicate_payload, config=config)


def test_receipt_write_is_no_overwrite_canonical_and_recomputed(
    tmp_path: Path,
) -> None:
    config = evaluation_config(minimum_metric_records=1, minimum_metric_sources=1)
    source = sources_for_split("evaluation", 1, config)[0]
    export_path = tmp_path / "export.json"
    write_export(export_path, [evidence_record(source_sha256=source)])
    output = tmp_path / "evaluation.json"

    receipt_sha256 = write_review_evidence_evaluation(
        export_path, output, config=config
    )
    assert receipt_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    receipt = load_review_evidence_evaluation(output, export_path)
    assert receipt["schema"] == EVALUATION_SCHEMA
    with pytest.raises(ReviewEvidenceEvaluationError, match="must not exist"):
        write_review_evidence_evaluation(export_path, output, config=config)

    tampered = copy.deepcopy(receipt)
    tampered["metrics"][0]["values"]["maximum_absolute_error_frames"] = 999
    tampered_path = tmp_path / "tampered-receipt.json"
    tampered_path.write_bytes(canonical_bytes(tampered))
    with pytest.raises(ReviewEvidenceEvaluationError, match="recomputation"):
        load_review_evidence_evaluation(tampered_path, export_path)

    noncanonical = tmp_path / "noncanonical.json"
    noncanonical.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    with pytest.raises(ReviewEvidenceEvaluationError, match="not canonical"):
        load_review_evidence_evaluation(noncanonical, export_path)


def test_evidence_evaluate_cli_prints_summary_or_writes_new_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    export_path = tmp_path / "export.json"
    write_export(
        export_path,
        [evidence_record(source_sha256="1" * 64)],
    )
    assert main(["evidence", "evaluate", str(export_path)]) == 0
    summary = capsys.readouterr().out
    assert "Review-evidence evaluation" in summary
    assert "cannot approve, apply, or change defaults" in summary

    output = tmp_path / "cli-evaluation.json"
    assert (
        main(
            [
                "evidence",
                "evaluate",
                str(export_path),
                "--output",
                str(output),
                "--json",
            ]
        )
        == 0
    )
    cli_result = json.loads(capsys.readouterr().out)
    assert cli_result["schema"] == EVALUATION_SCHEMA
    assert cli_result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert load_review_evidence_evaluation(output, export_path)["schema"] == (
        EVALUATION_SCHEMA
    )

    assert (
        main(
            [
                "evidence",
                "evaluate",
                str(export_path),
                "--baseline-config-sha256",
                CONFIG_A,
            ]
        )
        == 2
    )
    assert "Both --baseline-config-sha256" in capsys.readouterr().err
