"""Deterministic, non-authoritative evaluation of verified review evidence.

The evaluator deliberately accepts only a canonical review-evidence export.  It
never opens an evidence store, project, or media file.  Results are aggregates
bound to the exact export, evaluator implementation, application identity, and
evaluation configuration.  They can describe a corpus, but can never approve
an action, apply a change, or alter a default.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence, cast

from . import __file__ as application_module_path
from . import __version__
from .atomic_create import rename_no_replace
from .errors import GrooveSerpentError
from .review_evidence import (
    EVIDENCE_CATEGORIES,
    OWNER_OUTCOMES,
    load_review_evidence_export,
)


EVALUATION_SCHEMA = "groove-serpent.review-evidence-evaluation/1"
EVALUATION_CONFIG_SCHEMA = "groove-serpent.review-evidence-evaluation-config/1"
EVALUATOR_ID = "groove-serpent.review-evidence-evaluator/1"
SOURCE_SPLIT_SCHEMA = "groove-serpent.source-group-split/1"
COMPARISON_SCHEMA = "groove-serpent.review-evidence-config-comparison/1"
EVALUATION_AUTHORITY = "descriptive-evidence-only-never-approval"

MAX_EVALUATION_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_JSON_ITEMS = 16_384
MAX_RECEIPT_JSON_DEPTH = 12
MAX_RECEIPT_TEXT = 512

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MBID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SAFE_KIND = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_REPARSE_POINT = 0x400

SplitName = Literal["development", "evaluation"]


class ReviewEvidenceEvaluationError(GrooveSerpentError):
    """A corpus cannot support a strict, reproducible evaluation."""


@dataclass(frozen=True, slots=True)
class EvaluationConfig:
    """Bounded source-group split and data-sufficiency policy."""

    split_salt_sha256: str = hashlib.sha256(
        b"groove-serpent.review-evidence-evaluation.default-split/1"
    ).hexdigest()
    evaluation_basis_points: int = 2_000
    minimum_metric_records: int = 3
    minimum_metric_sources: int = 2
    minimum_paired_benchmarks: int = 3
    minimum_paired_sources: int = 2
    minimum_pair_coverage_basis_points: int = 8_000

    def to_dict(self) -> dict[str, Any]:
        """Return the exact canonical configuration body."""

        return {
            "schema": EVALUATION_CONFIG_SCHEMA,
            "split_algorithm": "sha256-source-group-basis-points/1",
            "split_salt_sha256": self.split_salt_sha256,
            "evaluation_basis_points": self.evaluation_basis_points,
            "minimum_metric_records": self.minimum_metric_records,
            "minimum_metric_sources": self.minimum_metric_sources,
            "minimum_paired_benchmarks": self.minimum_paired_benchmarks,
            "minimum_paired_sources": self.minimum_paired_sources,
            "minimum_pair_coverage_basis_points": (
                self.minimum_pair_coverage_basis_points
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationConfig":
        """Parse the exact supported configuration and reject extensions."""

        item = _exact(
            value,
            {
                "schema",
                "split_algorithm",
                "split_salt_sha256",
                "evaluation_basis_points",
                "minimum_metric_records",
                "minimum_metric_sources",
                "minimum_paired_benchmarks",
                "minimum_paired_sources",
                "minimum_pair_coverage_basis_points",
            },
            "Evaluation config",
        )
        if item["schema"] != EVALUATION_CONFIG_SCHEMA:
            raise ReviewEvidenceEvaluationError("Evaluation config schema is unsupported.")
        if item["split_algorithm"] != "sha256-source-group-basis-points/1":
            raise ReviewEvidenceEvaluationError("Source split algorithm is unsupported.")
        return cls(
            split_salt_sha256=_digest(
                item["split_salt_sha256"], "Split salt SHA-256"
            ),
            evaluation_basis_points=_integer(
                item["evaluation_basis_points"],
                "Evaluation basis points",
                minimum=1,
                maximum=9_999,
            ),
            minimum_metric_records=_integer(
                item["minimum_metric_records"],
                "Minimum metric records",
                minimum=1,
                maximum=4_096,
            ),
            minimum_metric_sources=_integer(
                item["minimum_metric_sources"],
                "Minimum metric sources",
                minimum=1,
                maximum=4_096,
            ),
            minimum_paired_benchmarks=_integer(
                item["minimum_paired_benchmarks"],
                "Minimum paired benchmarks",
                minimum=1,
                maximum=4_096,
            ),
            minimum_paired_sources=_integer(
                item["minimum_paired_sources"],
                "Minimum paired sources",
                minimum=1,
                maximum=4_096,
            ),
            minimum_pair_coverage_basis_points=_integer(
                item["minimum_pair_coverage_basis_points"],
                "Minimum pair coverage basis points",
                minimum=1,
                maximum=10_000,
            ),
        )


@dataclass(frozen=True, slots=True)
class ConfigComparison:
    """The ordered baseline and candidate feature-config identities."""

    baseline_config_sha256: str
    candidate_config_sha256: str

    def __post_init__(self) -> None:
        _digest(self.baseline_config_sha256, "Baseline config SHA-256")
        _digest(self.candidate_config_sha256, "Candidate config SHA-256")
        if self.baseline_config_sha256 == self.candidate_config_sha256:
            raise ReviewEvidenceEvaluationError(
                "Baseline and candidate config SHA-256 values must differ."
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "baseline_config_sha256": self.baseline_config_sha256,
            "candidate_config_sha256": self.candidate_config_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ConfigComparison":
        item = _exact(
            value,
            {"baseline_config_sha256", "candidate_config_sha256"},
            "Config comparison",
        )
        return cls(
            baseline_config_sha256=_digest(
                item["baseline_config_sha256"], "Baseline config SHA-256"
            ),
            candidate_config_sha256=_digest(
                item["candidate_config_sha256"], "Candidate config SHA-256"
            ),
        )


@dataclass(frozen=True, slots=True)
class _Record:
    record_sha256: str
    source_sha256: str
    category: str
    outcome: str
    region: dict[str, Any]
    proposal_kind: str
    proposal_payload: dict[str, Any]
    owner_payload: dict[str, Any]
    sample_count: int
    tool_identity_sha256: str
    config_identity_sha256: str
    config_sha256: str
    benchmark_sha256: str
    split: SplitName


@dataclass(frozen=True, slots=True)
class _AdaptedTarget:
    loss: Decimal
    target_identity: str
    ppm_loss: Decimal | None = None


def evaluation_may_authorize_action() -> Literal[False]:
    """Return the permanent evaluator authority boundary."""

    return False


def evaluation_may_apply_action() -> Literal[False]:
    """Return the permanent evaluator mutation boundary."""

    return False


def evaluation_may_change_defaults() -> Literal[False]:
    """Return the permanent default-changing boundary."""

    return False


def _canonical_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ReviewEvidenceEvaluationError(
            "Evaluation data is not canonical JSON."
        ) from exc
    return (rendered + "\n").encode("utf-8")


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ReviewEvidenceEvaluationError(
            f"{label} must contain exactly: {', '.join(sorted(keys))}."
        )
    return cast(dict[str, Any], value)


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ReviewEvidenceEvaluationError(
            f"{label} must be a lowercase SHA-256 digest."
        )
    return value


def _integer(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReviewEvidenceEvaluationError(
            f"{label} is outside its supported integer range."
        )
    return value


def _stable_module_sha256(path_value: str | None, label: str) -> str:
    if path_value is None:
        raise ReviewEvidenceEvaluationError(f"{label} has no inspectable module file.")
    path = Path(path_value)
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise ReviewEvidenceEvaluationError(f"{label} could not be inspected.") from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after or len(raw) != before.st_size:
        raise ReviewEvidenceEvaluationError(f"{label} changed while it was inspected.")
    return hashlib.sha256(raw).hexdigest()


def _evaluator_identity(config: EvaluationConfig) -> dict[str, Any]:
    config_body = config.to_dict()
    module_sha256 = _stable_module_sha256(__file__, "Evaluator module")
    application_module_sha256 = _stable_module_sha256(
        application_module_path, "Application module"
    )
    app_body = {
        "name": "groove-serpent",
        "version": __version__,
        "application_module_sha256": application_module_sha256,
    }
    return {
        "id": EVALUATOR_ID,
        "module": "groove_serpent.review_evidence_evaluation",
        "module_sha256": module_sha256,
        "application": {
            **app_body,
            "identity_sha256": _json_sha256(app_body),
        },
        "config": {
            "sha256": _json_sha256(config_body),
            "values": config_body,
        },
    }


def _source_split(source_sha256: str, config: EvaluationConfig) -> SplitName:
    material = (
        SOURCE_SPLIT_SCHEMA
        + "\x00"
        + config.split_salt_sha256
        + "\x00"
        + source_sha256
    ).encode("ascii")
    bucket = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % 10_000
    if bucket < config.evaluation_basis_points:
        return "evaluation"
    return "development"


def _benchmark_identity(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_sha256": record["source"]["sha256"],
        "region": record["region"],
        "category": record["category"],
        "proposal_kind": record["proposal"]["kind"],
    }


def _portable_kind(value: str) -> tuple[str | None, str]:
    identity = _json_sha256({"proposal_kind": value})
    return (value if _SAFE_KIND.fullmatch(value) is not None else None, identity)


def _records_from_export(export: Mapping[str, Any], config: EvaluationConfig) -> list[_Record]:
    result: list[_Record] = []
    duplicate_guard: set[tuple[str, str]] = set()
    for raw_entry in cast(list[dict[str, Any]], export["records"]):
        record = cast(dict[str, Any], raw_entry["record"])
        source_sha256 = cast(str, record["source"]["sha256"])
        config_body = cast(dict[str, Any], record["feature"]["config"])
        config_sha256 = cast(str, config_body["sha256"])
        benchmark_sha256 = _json_sha256(_benchmark_identity(record))
        duplicate_key = (config_sha256, benchmark_sha256)
        if duplicate_key in duplicate_guard:
            raise ReviewEvidenceEvaluationError(
                "The export contains duplicate benchmark keys for one config SHA-256."
            )
        duplicate_guard.add(duplicate_key)
        result.append(
            _Record(
                record_sha256=cast(str, raw_entry["record_sha256"]),
                source_sha256=source_sha256,
                category=cast(str, record["category"]),
                outcome=cast(str, record["outcome"]),
                region=cast(dict[str, Any], record["region"]),
                proposal_kind=cast(str, record["proposal"]["kind"]),
                proposal_payload=cast(dict[str, Any], record["proposal"]["payload"]),
                owner_payload=cast(dict[str, Any], record["owner_result"]["payload"]),
                sample_count=cast(int, record["source"]["sample_count"]),
                tool_identity_sha256=_json_sha256(record["feature"]["tool"]),
                config_identity_sha256=_json_sha256(config_body),
                config_sha256=config_sha256,
                benchmark_sha256=benchmark_sha256,
                split=_source_split(source_sha256, config),
            )
        )
    return result


def _aggregate_coverage(
    records: Sequence[_Record],
    key_values: Sequence[tuple[Any, ...]],
    entry_builder: Callable[[tuple[Any, ...]], dict[str, Any]],
) -> list[dict[str, Any]]:
    counts: dict[tuple[Any, ...], list[_Record]] = {}
    for record, key in zip(records, key_values, strict=True):
        counts.setdefault(key, []).append(record)
    return [
        {
            **entry_builder(key),
            "record_count": len(items),
            "source_count": len({item.source_sha256 for item in items}),
        }
        for key, items in sorted(counts.items())
    ]


def _coverage(records: Sequence[_Record]) -> dict[str, Any]:
    category_entries = []
    for category in sorted(EVIDENCE_CATEGORIES):
        items = [record for record in records if record.category == category]
        category_entries.append(
            {
                "category": category,
                "record_count": len(items),
                "source_count": len({item.source_sha256 for item in items}),
                "metric_policy": (
                    "typed-target-when-available"
                    if category in {"boundary", "endpoint", "recognition", "speed"}
                    else "coverage-and-outcomes-only"
                ),
            }
        )
    outcome_entries = []
    for outcome in sorted(OWNER_OUTCOMES):
        items = [record for record in records if record.outcome == outcome]
        outcome_entries.append(
            {
                "outcome": outcome,
                "record_count": len(items),
                "source_count": len({item.source_sha256 for item in items}),
            }
        )
    category_outcome = []
    for category in sorted(EVIDENCE_CATEGORIES):
        for outcome in sorted(OWNER_OUTCOMES):
            items = [
                record
                for record in records
                if record.category == category and record.outcome == outcome
            ]
            category_outcome.append(
                {
                    "category": category,
                    "outcome": outcome,
                    "record_count": len(items),
                    "source_count": len({item.source_sha256 for item in items}),
                }
            )
    tools = _aggregate_coverage(
        records,
        [(record.tool_identity_sha256,) for record in records],
        lambda key: {"tool_identity_sha256": key[0]},
    )
    configs = _aggregate_coverage(
        records,
        [
            (record.config_sha256, record.config_identity_sha256)
            for record in records
        ],
        lambda key: {
            "config_sha256": key[0],
            "config_identity_sha256": key[1],
        },
    )
    proposal_kinds = _aggregate_coverage(
        records,
        [
            (
                _portable_kind(record.proposal_kind)[1],
                _portable_kind(record.proposal_kind)[0],
            )
            for record in records
        ],
        lambda key: {"identity_sha256": key[0], "kind": key[1]},
    )
    return {
        "categories": category_entries,
        "outcomes": outcome_entries,
        "category_outcomes": category_outcome,
        "tools": tools,
        "configs": configs,
        "proposal_kinds": proposal_kinds,
    }


def _split_report(records: Sequence[_Record]) -> dict[str, Any]:
    sources = sorted({record.source_sha256 for record in records})
    assignments = [
        {
            "source_sha256": source,
            "split": next(
                record.split for record in records if record.source_sha256 == source
            ),
        }
        for source in sources
    ]
    groups = []
    for split in ("development", "evaluation"):
        split_sources = sorted(
            item["source_sha256"] for item in assignments if item["split"] == split
        )
        groups.append(
            {
                "split": split,
                "source_count": len(split_sources),
                "record_count": sum(record.split == split for record in records),
                "source_set_sha256": _json_sha256(split_sources),
            }
        )
    return {
        "schema": SOURCE_SPLIT_SCHEMA,
        "unit": "source_sha256",
        "assignment_sha256": _json_sha256(assignments),
        "unique_source_count": len(sources),
        "groups": groups,
        "source_groups_disjoint": True,
        "record_level_splitting_permitted": False,
    }


def _frame_target(record: _Record) -> _AdaptedTarget | None:
    proposed = record.proposal_payload.get("proposed_frame")
    final = record.owner_payload.get("final_frame")
    if (
        type(proposed) is not int
        or type(final) is not int
        or not 0 <= proposed <= record.sample_count
        or not 0 <= final <= record.sample_count
    ):
        return None
    return _AdaptedTarget(
        loss=Decimal(abs(proposed - final)),
        target_identity=_json_sha256({"final_frame": final}),
    )


def _positive_decimal(value: Any) -> Decimal | None:
    if type(value) not in {int, float} or type(value) is bool:
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    try:
        rendered = Decimal(str(value))
    except InvalidOperation:
        return None
    if not Decimal("0.25") <= rendered <= Decimal("4"):
        return None
    return rendered


def _speed_target(record: _Record) -> _AdaptedTarget | None:
    proposed = _positive_decimal(record.proposal_payload.get("proposed_factor"))
    final = _positive_decimal(record.owner_payload.get("final_factor"))
    if proposed is None or final is None:
        return None
    difference = abs(proposed - final)
    return _AdaptedTarget(
        loss=difference,
        target_identity=_json_sha256(
            {"final_factor_decimal": str(final.normalize())}
        ),
        ppm_loss=(difference / final) * Decimal(1_000_000),
    )


def _recognition_target(record: _Record) -> _AdaptedTarget | None:
    proposed = record.proposal_payload.get("release_mbid")
    final = record.owner_payload.get("release_mbid")
    if (
        type(proposed) is not str
        or type(final) is not str
        or _MBID.fullmatch(proposed) is None
        or _MBID.fullmatch(final) is None
    ):
        return None
    return _AdaptedTarget(
        loss=Decimal(0 if proposed == final else 1),
        target_identity=_json_sha256({"release_mbid": final}),
    )


def _adapter(category: str, record: _Record) -> _AdaptedTarget | None:
    if category in {"boundary", "endpoint"}:
        return _frame_target(record)
    if category == "speed":
        return _speed_target(record)
    if category == "recognition":
        return _recognition_target(record)
    return None


def _decimal_text(value: Decimal, places: int) -> str:
    quantizer = Decimal(1).scaleb(-places)
    return format(value.quantize(quantizer), "f")


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _metric_values(category: str, adapted: Sequence[_AdaptedTarget]) -> dict[str, Any]:
    losses = [item.loss for item in adapted]
    if category in {"boundary", "endpoint"}:
        return {
            "mean_absolute_error_frames": float(_decimal_text(_mean(losses), 6)),
            "median_absolute_error_frames": float(_decimal_text(_median(losses), 6)),
            "maximum_absolute_error_frames": int(max(losses)),
            "exact_frame_match_count": sum(loss == 0 for loss in losses),
        }
    if category == "speed":
        ppm = [
            item.ppm_loss
            for item in adapted
            if item.ppm_loss is not None
        ]
        if len(ppm) != len(adapted):
            raise ReviewEvidenceEvaluationError(
                "A typed speed target is missing its relative PPM error."
            )
        return {
            "mean_absolute_factor_error": _decimal_text(_mean(losses), 12),
            "median_absolute_factor_error": _decimal_text(_median(losses), 12),
            "maximum_absolute_factor_error": _decimal_text(max(losses), 12),
            "mean_absolute_error_ppm": _decimal_text(_mean(ppm), 3),
            "maximum_absolute_error_ppm": _decimal_text(max(ppm), 3),
            "decimal_encoding": "fixed-point-string",
        }
    matches = sum(loss == 0 for loss in losses)
    return {
        "exact_release_match_count": matches,
        "exact_release_mismatch_count": len(losses) - matches,
        "exact_release_match_rate_basis_points": (matches * 10_000) // len(losses),
    }


def _metrics(records: Sequence[_Record], config: EvaluationConfig) -> list[dict[str, Any]]:
    result = []
    for category, metric_id, target_pair in (
        ("boundary", "boundary-frame-error", "proposed_frame/final_frame"),
        ("endpoint", "endpoint-frame-error", "proposed_frame/final_frame"),
        (
            "recognition",
            "recognition-exact-release-match",
            "proposal.release_mbid/owner_result.release_mbid",
        ),
        ("speed", "speed-factor-error", "proposed_factor/final_factor"),
    ):
        scoped = [
            record
            for record in records
            if record.split == "evaluation" and record.category == category
        ]
        adapted_pairs = [
            (record, _adapter(category, record)) for record in scoped
        ]
        eligible = [
            (record, adapted)
            for record, adapted in adapted_pairs
            if adapted is not None
        ]
        sources = {record.source_sha256 for record, _adapted in eligible}
        reasons = []
        if len(eligible) < config.minimum_metric_records:
            reasons.append("insufficient_typed_evaluation_records")
        if len(sources) < config.minimum_metric_sources:
            reasons.append("insufficient_typed_evaluation_sources")
        sufficient = not reasons
        result.append(
            {
                "id": metric_id,
                "category": category,
                "target_pair": target_pair,
                "scope": "evaluation-source-split-only",
                "eligible_record_count": len(eligible),
                "ineligible_record_count": len(scoped) - len(eligible),
                "source_count": len(sources),
                "data_sufficient": sufficient,
                "abstained": not sufficient,
                "abstention_reasons": reasons,
                "values": (
                    _metric_values(
                        category, [adapted for _record, adapted in eligible]
                    )
                    if sufficient
                    else None
                ),
            }
        )
    return result


def _coverage_basis_points(paired: int, total: int) -> int:
    if total == 0:
        return 0
    return (paired * 10_000) // total


def _comparison_metric(
    category: str,
    baseline: Sequence[_Record],
    candidate: Sequence[_Record],
    config: EvaluationConfig,
) -> dict[str, Any]:
    baseline_by_key = {record.benchmark_sha256: record for record in baseline}
    candidate_by_key = {record.benchmark_sha256: record for record in candidate}
    paired_keys = sorted(set(baseline_by_key) & set(candidate_by_key))
    coverage_baseline = _coverage_basis_points(len(paired_keys), len(baseline))
    coverage_candidate = _coverage_basis_points(len(paired_keys), len(candidate))
    no_adapter = category in {"restoration", "structural-event"}
    typed_pairs: list[tuple[_Record, _AdaptedTarget, _AdaptedTarget]] = []
    target_mismatch_count = 0
    untyped_pair_count = 0
    if not no_adapter:
        for key in paired_keys:
            baseline_record = baseline_by_key[key]
            baseline_target = _adapter(category, baseline_record)
            candidate_target = _adapter(category, candidate_by_key[key])
            if baseline_target is None or candidate_target is None:
                untyped_pair_count += 1
                continue
            if baseline_target.target_identity != candidate_target.target_identity:
                target_mismatch_count += 1
                continue
            typed_pairs.append((baseline_record, baseline_target, candidate_target))
    sources = {record.source_sha256 for record, _left, _right in typed_pairs}
    reasons: list[str] = []
    if no_adapter:
        reasons.append("no_defensible_typed_target_adapter")
    else:
        if len(typed_pairs) < config.minimum_paired_benchmarks:
            reasons.append("insufficient_paired_typed_benchmarks")
        if len(sources) < config.minimum_paired_sources:
            reasons.append("insufficient_paired_sources")
        if (
            coverage_baseline < config.minimum_pair_coverage_basis_points
            or coverage_candidate < config.minimum_pair_coverage_basis_points
        ):
            reasons.append("imbalanced_or_unpaired_config_coverage")
        if target_mismatch_count:
            reasons.append("owner_target_mismatch")
    sufficient = not reasons
    values: dict[str, Any] | None = None
    if sufficient:
        baseline_losses = [left.loss for _record, left, _right in typed_pairs]
        candidate_losses = [right.loss for _record, _left, right in typed_pairs]
        candidate_lower = sum(
            right < left for left, right in zip(baseline_losses, candidate_losses, strict=True)
        )
        baseline_lower = sum(
            left < right for left, right in zip(baseline_losses, candidate_losses, strict=True)
        )
        values = {
            "baseline_mean_loss": _decimal_text(_mean(baseline_losses), 12),
            "candidate_mean_loss": _decimal_text(_mean(candidate_losses), 12),
            "candidate_minus_baseline_mean_loss": _decimal_text(
                _mean(candidate_losses) - _mean(baseline_losses), 12
            ),
            "candidate_lower_error_count": candidate_lower,
            "baseline_lower_error_count": baseline_lower,
            "equal_error_count": len(typed_pairs) - candidate_lower - baseline_lower,
            "decimal_encoding": "fixed-point-string",
            "interpretation": "descriptive-paired-error-only-not-causal",
        }
    return {
        "category": category,
        "scope": "evaluation-source-split-only",
        "baseline_record_count": len(baseline),
        "candidate_record_count": len(candidate),
        "paired_benchmark_count": len(paired_keys),
        "typed_pair_count": len(typed_pairs),
        "untyped_pair_count": untyped_pair_count,
        "owner_target_mismatch_count": target_mismatch_count,
        "paired_source_count": len(sources),
        "baseline_pair_coverage_basis_points": coverage_baseline,
        "candidate_pair_coverage_basis_points": coverage_candidate,
        "data_sufficient": sufficient,
        "abstained": not sufficient,
        "abstention_reasons": reasons,
        "values": values,
    }


def _comparison_report(
    records: Sequence[_Record],
    request: ConfigComparison | None,
    config: EvaluationConfig,
) -> dict[str, Any] | None:
    if request is None:
        return None
    metrics = []
    for category in sorted(EVIDENCE_CATEGORIES):
        baseline = [
            record
            for record in records
            if record.split == "evaluation"
            and record.category == category
            and record.config_sha256 == request.baseline_config_sha256
        ]
        candidate = [
            record
            for record in records
            if record.split == "evaluation"
            and record.category == category
            and record.config_sha256 == request.candidate_config_sha256
        ]
        metrics.append(_comparison_metric(category, baseline, candidate, config))
    return {
        "schema": COMPARISON_SCHEMA,
        "request": request.to_dict(),
        "pairing_key": [
            "source_sha256",
            "region",
            "category",
            "proposal_kind",
        ],
        "pairing_scope": "evaluation-source-split-only",
        "data_sufficient_for_any_metric": any(
            item["data_sufficient"] for item in metrics
        ),
        "abstained": not any(item["data_sufficient"] for item in metrics),
        "metrics": metrics,
        "acceptance_rate_used_as_quality_metric": False,
        "causal_claim_permitted": False,
    }


def _sufficiency(
    records: Sequence[_Record], metrics: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    evaluation = [record for record in records if record.split == "evaluation"]
    development = [record for record in records if record.split == "development"]
    sufficient_any = any(item["data_sufficient"] for item in metrics)
    reasons = []
    if not records:
        reasons.append("empty_corpus")
    if not evaluation:
        reasons.append("no_evaluation_sources")
    if not sufficient_any:
        reasons.append("no_typed_metric_has_sufficient_evaluation_data")
    return {
        "record_count": len(records),
        "source_count": len({record.source_sha256 for record in records}),
        "development_record_count": len(development),
        "development_source_count": len(
            {record.source_sha256 for record in development}
        ),
        "evaluation_record_count": len(evaluation),
        "evaluation_source_count": len(
            {record.source_sha256 for record in evaluation}
        ),
        "sufficient_for_any_typed_metric": sufficient_any,
        "abstained": not sufficient_any,
        "abstention_reasons": reasons,
    }


def build_review_evidence_evaluation(
    export: Mapping[str, Any],
    *,
    config: EvaluationConfig | None = None,
    comparison: ConfigComparison | None = None,
) -> dict[str, Any]:
    """Evaluate one already-validated export payload without external reads."""

    from .review_evidence import validate_review_evidence_export

    resolved_config = config or EvaluationConfig()
    # Re-enter the existing strict export validator even for programmatic callers.
    verified_export = validate_review_evidence_export(export)
    records = _records_from_export(verified_export, resolved_config)
    metrics = _metrics(records, resolved_config)
    result = {
        "schema": EVALUATION_SCHEMA,
        "authority": EVALUATION_AUTHORITY,
        "may_authorize_action": False,
        "may_apply_action": False,
        "may_change_defaults": False,
        "corpus_export": {
            "schema": verified_export["schema"],
            "sha256": _json_sha256(verified_export),
            "record_count": verified_export["record_count"],
        },
        "evaluator": _evaluator_identity(resolved_config),
        "source_split": _split_report(records),
        "coverage": _coverage(records),
        "data_sufficiency": _sufficiency(records, metrics),
        "metrics": metrics,
        "comparison": _comparison_report(
            records, comparison, resolved_config
        ),
        "interpretation_limits": [
            "acceptance-rate-is-not-a-quality-metric",
            "paired-differences-are-descriptive-not-causal",
            "restoration-and-structural-events-have-coverage-only",
            "evaluation-never-authorizes-applies-or-changes-defaults",
        ],
    }
    _validate_receipt_json(result)
    if len(_canonical_bytes(result)) > MAX_EVALUATION_BYTES:
        raise ReviewEvidenceEvaluationError(
            "Review-evidence evaluation exceeds its supported size limit."
        )
    return result


def evaluate_review_evidence_export(
    export_path: Path,
    *,
    config: EvaluationConfig | None = None,
    comparison: ConfigComparison | None = None,
) -> dict[str, Any]:
    """Load only through the strict export API and build an evaluation."""

    export = load_review_evidence_export(export_path)
    return build_review_evidence_evaluation(
        export, config=config, comparison=comparison
    )


def _validate_receipt_json(value: Any) -> None:
    remaining = [MAX_RECEIPT_JSON_ITEMS]

    def visit(item: Any, depth: int) -> None:
        if depth > MAX_RECEIPT_JSON_DEPTH:
            raise ReviewEvidenceEvaluationError(
                "Evaluation receipt exceeds its supported nesting depth."
            )
        remaining[0] -= 1
        if remaining[0] < 0:
            raise ReviewEvidenceEvaluationError(
                "Evaluation receipt contains too many values."
            )
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if not -(1 << 63) <= item <= (1 << 63) - 1:
                raise ReviewEvidenceEvaluationError(
                    "Evaluation receipt contains an out-of-range integer."
                )
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ReviewEvidenceEvaluationError(
                    "Evaluation receipt contains a non-finite number."
                )
            return
        if type(item) is str:
            if not item or len(item) > MAX_RECEIPT_TEXT or "\x00" in item:
                raise ReviewEvidenceEvaluationError(
                    "Evaluation receipt contains invalid text."
                )
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or not key or len(key) > 100:
                    raise ReviewEvidenceEvaluationError(
                        "Evaluation receipt contains an invalid object key."
                    )
                if (
                    key == "path"
                    or key.endswith("_path")
                    or key.startswith("audio")
                    or key in {"samples", "waveform", "spectrogram"}
                ):
                    raise ReviewEvidenceEvaluationError(
                        "Evaluation receipts must remain path- and media-free."
                    )
                visit(child, depth + 1)
            return
        raise ReviewEvidenceEvaluationError(
            "Evaluation receipt contains an unsupported JSON value."
        )

    visit(value, 0)


def validate_review_evidence_evaluation(
    value: Mapping[str, Any],
    export_path: Path,
) -> dict[str, Any]:
    """Strictly recompute a receipt from its bound export and current code."""

    receipt = _exact(
        value,
        {
            "schema",
            "authority",
            "may_authorize_action",
            "may_apply_action",
            "may_change_defaults",
            "corpus_export",
            "evaluator",
            "source_split",
            "coverage",
            "data_sufficiency",
            "metrics",
            "comparison",
            "interpretation_limits",
        },
        "Evaluation receipt",
    )
    if receipt["schema"] != EVALUATION_SCHEMA:
        raise ReviewEvidenceEvaluationError("Evaluation receipt schema is unsupported.")
    if (
        receipt["authority"] != EVALUATION_AUTHORITY
        or receipt["may_authorize_action"] is not False
        or receipt["may_apply_action"] is not False
        or receipt["may_change_defaults"] is not False
    ):
        raise ReviewEvidenceEvaluationError(
            "Evaluation receipts can never carry action authority."
        )
    evaluator = _exact(
        receipt["evaluator"],
        {"id", "module", "module_sha256", "application", "config"},
        "Evaluator identity",
    )
    config_identity = _exact(
        evaluator["config"], {"sha256", "values"}, "Evaluator config identity"
    )
    config = EvaluationConfig.from_dict(
        cast(Mapping[str, Any], config_identity["values"])
    )
    if _digest(config_identity["sha256"], "Evaluator config SHA-256") != _json_sha256(
        config.to_dict()
    ):
        raise ReviewEvidenceEvaluationError(
            "Evaluator config SHA-256 does not match its values."
        )
    comparison_value = receipt["comparison"]
    comparison: ConfigComparison | None = None
    if comparison_value is not None:
        comparison_body = _exact(
            comparison_value,
            {
                "schema",
                "request",
                "pairing_key",
                "pairing_scope",
                "data_sufficient_for_any_metric",
                "abstained",
                "metrics",
                "acceptance_rate_used_as_quality_metric",
                "causal_claim_permitted",
            },
            "Comparison receipt",
        )
        comparison = ConfigComparison.from_dict(
            cast(Mapping[str, Any], comparison_body["request"])
        )
    _validate_receipt_json(receipt)
    expected = evaluate_review_evidence_export(
        export_path, config=config, comparison=comparison
    )
    if receipt != expected:
        raise ReviewEvidenceEvaluationError(
            "Evaluation receipt does not match independent recomputation."
        )
    return cast(dict[str, Any], json.loads(_canonical_bytes(receipt)))


def _plain_file_bytes(path: Path, maximum: int, label: str) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReviewEvidenceEvaluationError(f"{label} could not be inspected.") from exc
    if (
        path.is_symlink()
        or bool(int(getattr(before, "st_file_attributes", 0)) & _REPARSE_POINT)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > maximum
    ):
        raise ReviewEvidenceEvaluationError(
            f"{label} must be a bounded regular non-reparse file."
        )
    try:
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            raw = handle.read(maximum + 1)
            closed = os.fstat(handle.fileno())
        after = path.lstat()
    except OSError as exc:
        raise ReviewEvidenceEvaluationError(f"{label} could not be read.") from exc

    def identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            item.st_dev,
            item.st_ino,
            item.st_mode,
            item.st_size,
            item.st_mtime_ns,
        )
    if (
        identity(before) != identity(opened)
        or identity(opened) != identity(closed)
        or identity(closed) != identity(after)
    ):
        raise ReviewEvidenceEvaluationError(f"{label} changed while it was read.")
    if len(raw) > maximum:
        raise ReviewEvidenceEvaluationError(f"{label} exceeds its supported size limit.")
    return raw


def load_review_evidence_evaluation(
    receipt_path: Path,
    export_path: Path,
) -> dict[str, Any]:
    """Reopen a canonical receipt and independently recompute every field."""

    raw = _plain_file_bytes(
        receipt_path.absolute(), MAX_EVALUATION_BYTES, "Evaluation receipt"
    )
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewEvidenceEvaluationError(
            "Evaluation receipt is not valid UTF-8 JSON."
        ) from exc
    if type(decoded) is not dict:
        raise ReviewEvidenceEvaluationError("Evaluation receipt root must be an object.")
    validated = validate_review_evidence_evaluation(decoded, export_path)
    if raw != _canonical_bytes(validated):
        raise ReviewEvidenceEvaluationError("Evaluation receipt is not canonical.")
    return validated


def _write_new_canonical(path: Path, raw: bytes) -> None:
    path = path.absolute()
    if os.path.lexists(path):
        raise ReviewEvidenceEvaluationError(
            "Evaluation receipt destination must not exist."
        )
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise ReviewEvidenceEvaluationError(
            "Evaluation receipt parent could not be inspected."
        ) from exc
    if (
        parent.is_symlink()
        or bool(
            int(getattr(parent_metadata, "st_file_attributes", 0))
            & _REPARSE_POINT
        )
        or not stat.S_ISDIR(parent_metadata.st_mode)
    ):
        raise ReviewEvidenceEvaluationError(
            "Evaluation receipt parent must be a regular non-reparse directory."
        )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        rename_no_replace(temporary, path)
    except FileExistsError as exc:
        raise ReviewEvidenceEvaluationError(
            "Evaluation receipt destination appeared."
        ) from exc
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_review_evidence_evaluation(
    export_path: Path,
    output_path: Path,
    *,
    config: EvaluationConfig | None = None,
    comparison: ConfigComparison | None = None,
) -> str:
    """Write a canonical no-overwrite receipt, then reopen and recompute it."""

    receipt = evaluate_review_evidence_export(
        export_path, config=config, comparison=comparison
    )
    raw = _canonical_bytes(receipt)
    _write_new_canonical(output_path, raw)
    load_review_evidence_evaluation(output_path, export_path)
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "COMPARISON_SCHEMA",
    "EVALUATION_AUTHORITY",
    "EVALUATION_CONFIG_SCHEMA",
    "EVALUATION_SCHEMA",
    "EVALUATOR_ID",
    "MAX_EVALUATION_BYTES",
    "SOURCE_SPLIT_SCHEMA",
    "ConfigComparison",
    "EvaluationConfig",
    "ReviewEvidenceEvaluationError",
    "build_review_evidence_evaluation",
    "evaluate_review_evidence_export",
    "evaluation_may_apply_action",
    "evaluation_may_authorize_action",
    "evaluation_may_change_defaults",
    "load_review_evidence_evaluation",
    "validate_review_evidence_evaluation",
    "write_review_evidence_evaluation",
]
