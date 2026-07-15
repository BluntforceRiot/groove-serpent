"""Evidence-first bounded continuous-crackle proposal and audition primitives.

This module deliberately reuses Groove Serpent's conservative isolated-click
detector and exact-window repairer, but adds a separate crackle evidence,
recipe, render, and receipt namespace.  It can only propose a short bounded
three-way audition.  It cannot edit a project, alter source audio, authorize a
restoration, publish an album, or claim perceptual transparency.
"""

from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence, cast

import numpy as np

from .continuous_noise import NoiseAnalysisScope, NoiseReferenceRegion
from .errors import ProjectValidationError
from .publication import canonical_json_sha256
from .restoration import ClickInterval, detect_impulsive_clicks, repair_click_intervals
from .validation import strict_finite_number

CRACKLE_PROPOSAL_SCHEMA = "groove-serpent.continuous-crackle-proposal/1"
CRACKLE_REVIEW_ATTESTATION_SCHEMA = (
    "groove-serpent.continuous-crackle-review-attestation/1"
)
CRACKLE_PREVIEW_RECIPE_SCHEMA = "groove-serpent.continuous-crackle-preview-recipe/1"
CRACKLE_PREVIEW_RENDER_SCHEMA = "groove-serpent.continuous-crackle-preview-render/1"
CRACKLE_PREVIEW_RECEIPT_SCHEMA = "groove-serpent.continuous-crackle-preview-receipt/1"
CRACKLE_ANALYSIS_ALGORITHM_ID = "groove-serpent.bounded-continuous-crackle-evidence/1"
CRACKLE_PREVIEW_ALGORITHM_ID = "groove-serpent.bounded-continuous-crackle-preview/1"
REVIEW_DECISION = "request_owner_audition_preview"
REVIEW_ACKNOWLEDGEMENT = (
    "caller_attestation_is_not_proof_of_human_audition_or_restoration_approval"
)

_MAX_EVENTS = 4_096
_MAX_REFERENCES = 64
_DIGEST_CHARS = frozenset("0123456789abcdef")


def _object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProjectValidationError(f"{label} must be a JSON object.")
    return cast(dict[str, Any], value)


def _array(value: Any, label: str) -> list[Any]:
    if type(value) is not list:
        raise ProjectValidationError(f"{label} must be a JSON array.")
    return value


def _keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ProjectValidationError(
            f"{label} fields are invalid (missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)})."
        )


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectValidationError(
            f"{label} must be an integer between {minimum} and {maximum}."
        )
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    result = strict_finite_number(value, label)
    if not minimum <= result <= maximum:
        raise ProjectValidationError(f"{label} must be between {minimum} and {maximum}.")
    return result


def _text(value: Any, label: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectValidationError(f"{label} must be bounded printable text.")
    return value


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST_CHARS for character in value)
    ):
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _authority() -> dict[str, Any]:
    return {
        "method_profile": "bounded_continuous_crackle_owner_audition_only",
        "automatic_application_forbidden": True,
        "automatic_publication_forbidden": True,
        "may_edit_project": False,
        "may_modify_source_audio": False,
        "may_claim_quality_neutrality": False,
        "owner_audition_required": True,
    }


@dataclass(frozen=True, slots=True)
class CrackleAnalysisConfig:
    threshold_sigma: float = 12.0
    local_window_samples: int = 65
    minimum_slope_ratio: float = 0.35
    maximum_event_samples: int = 8
    minimum_total_events: int = 4
    minimum_reference_events: int = 2
    minimum_events_per_second: float = 0.10
    maximum_events_per_second: float = 80.0
    maximum_repaired_fraction: float = 0.005
    maximum_event_count: int = _MAX_EVENTS

    def validate(self) -> None:
        _number(self.threshold_sigma, "Crackle threshold sigma", 4.0, 100.0)
        _integer(self.local_window_samples, "Crackle local window", 5, 4_095)
        if self.local_window_samples % 2 == 0:
            raise ProjectValidationError("Crackle local window must be odd.")
        _number(self.minimum_slope_ratio, "Crackle slope ratio", 0.0, 1.0)
        _integer(self.maximum_event_samples, "Crackle event samples", 1, 128)
        _integer(self.minimum_total_events, "Crackle minimum events", 1, _MAX_EVENTS)
        _integer(
            self.minimum_reference_events,
            "Crackle minimum reference events",
            1,
            _MAX_EVENTS,
        )
        _number(self.minimum_events_per_second, "Crackle minimum density", 0.0, 1_000.0)
        _number(self.maximum_events_per_second, "Crackle maximum density", 0.001, 10_000.0)
        if self.maximum_events_per_second <= self.minimum_events_per_second:
            raise ProjectValidationError("Crackle density bounds are inverted.")
        _number(
            self.maximum_repaired_fraction,
            "Crackle maximum repaired fraction",
            0.000001,
            0.05,
        )
        _integer(self.maximum_event_count, "Crackle maximum event count", 1, _MAX_EVENTS)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "CrackleAnalysisConfig":
        data = _object(value, "Crackle analysis configuration")
        _keys(data, set(cls.__dataclass_fields__), "Crackle analysis configuration")
        result = cls(**data)
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class CracklePreviewConfig:
    context_samples: int = 256
    lpc_order: int = 16
    maximum_linear_gain_delta: float = 0.01
    residue_monitor_target_rms: float = 0.05
    maximum_residue_monitor_gain: float = 100.0

    def validate(self) -> None:
        _integer(self.context_samples, "Crackle repair context", 16, 8_192)
        _integer(self.lpc_order, "Crackle LPC order", 2, 64)
        _number(
            self.maximum_linear_gain_delta,
            "Crackle matched gain delta",
            0.0,
            0.10,
        )
        _number(
            self.residue_monitor_target_rms,
            "Crackle residue target RMS",
            0.000001,
            1.0,
        )
        _number(
            self.maximum_residue_monitor_gain,
            "Crackle residue gain limit",
            1.0,
            1_000.0,
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "CracklePreviewConfig":
        data = _object(value, "Crackle preview configuration")
        _keys(data, set(cls.__dataclass_fields__), "Crackle preview configuration")
        result = cls(**data)
        result.validate()
        return result


def _event_dict(interval: ClickInterval) -> dict[str, Any]:
    return {
        "start_sample": interval.start_sample,
        "end_sample_exclusive": interval.end_sample,
        "peak_sample": interval.peak_sample,
        "confidence": float(interval.confidence),
        "channels": list(interval.channels),
    }


def _parse_events(
    value: Any,
    *,
    sample_count: int,
    channel_count: int,
) -> list[ClickInterval]:
    items = _array(value, "Crackle events")
    if len(items) > _MAX_EVENTS:
        raise ProjectValidationError("Crackle event list exceeds its bound.")
    result: list[ClickInterval] = []
    for index, raw in enumerate(items):
        item = _object(raw, f"Crackle event {index}")
        _keys(
            item,
            {
                "start_sample",
                "end_sample_exclusive",
                "peak_sample",
                "confidence",
                "channels",
            },
            f"Crackle event {index}",
        )
        start = _integer(item["start_sample"], "Crackle event start", 1, sample_count - 2)
        end = _integer(
            item["end_sample_exclusive"],
            "Crackle event end",
            start + 1,
            sample_count - 1,
        )
        peak = _integer(item["peak_sample"], "Crackle event peak", start, end - 1)
        confidence = _number(item["confidence"], "Crackle confidence", 0.0, 1.0)
        raw_channels = _array(item["channels"], "Crackle event channels")
        if (
            not raw_channels
            or len(raw_channels) > channel_count
            or any(type(channel) is not int for channel in raw_channels)
        ):
            raise ProjectValidationError("Crackle event channels are invalid.")
        channels = tuple(cast(list[int], raw_channels))
        if (
            channels != tuple(sorted(set(channels)))
            or channels[0] < 0
            or channels[-1] >= channel_count
        ):
            raise ProjectValidationError("Crackle event channels are invalid.")
        try:
            result.append(ClickInterval(start, end, peak, confidence, channels))
        except (TypeError, ValueError) as exc:
            raise ProjectValidationError(f"Crackle event is invalid: {exc}") from exc
    if result != sorted(
        result,
        key=lambda event: (event.start_sample, event.end_sample, event.channels),
    ):
        raise ProjectValidationError("Crackle events must be canonically ordered.")
    for offset, previous in enumerate(result):
        for current in result[offset + 1 :]:
            if current.start_sample > previous.end_sample:
                break
            if not set(previous.channels).isdisjoint(current.channels):
                raise ProjectValidationError(
                    "Crackle events may not overlap or touch in the same channel."
                )
    return result


def _proposal_reasons(
    metrics: Mapping[str, Any],
    config: CrackleAnalysisConfig,
) -> list[str]:
    reasons: list[str] = []
    detected = cast(int, metrics["detected_event_count"])
    reference = cast(int, metrics["reference_event_count"])
    density = float(metrics["events_per_second"])
    fraction = float(metrics["repaired_sample_value_fraction"])
    stored = cast(int, metrics["stored_event_count"])
    if detected > config.maximum_event_count:
        reasons.append("event_count_exceeds_bounded_preview")
    if stored < config.minimum_total_events:
        reasons.append("insufficient_conservative_crackle_events")
    if reference < config.minimum_reference_events:
        reasons.append("insufficient_noise_reference_crackle_evidence")
    if density < config.minimum_events_per_second:
        reasons.append("crackle_density_below_configured_minimum")
    if density > config.maximum_events_per_second:
        reasons.append("crackle_density_exceeds_conservative_limit")
    if fraction > config.maximum_repaired_fraction:
        reasons.append("proposed_repair_fraction_exceeds_conservative_limit")
    return reasons


@dataclass(frozen=True, slots=True)
class CrackleProposal:
    _data: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "CrackleProposal":
        return cls(validate_crackle_proposal(value))

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    @property
    def status(self) -> str:
        return cast(str, self._data["status"])

    @property
    def proposal_body_sha256(self) -> str:
        return cast(str, self._data["proposal_body_sha256"])

    @property
    def scope(self) -> dict[str, Any]:
        return cast(dict[str, Any], copy.deepcopy(self._data["scope"]))

    @property
    def events(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], copy.deepcopy(self._data["events"]))


def analyze_crackle(
    audio: np.ndarray,
    *,
    sample_rate: int,
    scope: NoiseAnalysisScope,
    noise_references: Sequence[NoiseReferenceRegion],
    config: CrackleAnalysisConfig | None = None,
) -> CrackleProposal:
    cfg = config or CrackleAnalysisConfig()
    cfg.validate()
    if type(sample_rate) is not int or not 8_000 <= sample_rate <= 768_000:
        raise ProjectValidationError("Crackle sample rate is unsupported.")
    source = np.asarray(audio)
    if source.ndim not in (1, 2) or source.shape[0] < 3:
        raise ProjectValidationError("Crackle audio geometry is invalid.")
    framed = source[:, np.newaxis] if source.ndim == 1 else source
    if framed.shape[1] < 1 or framed.shape[1] > 32:
        raise ProjectValidationError("Crackle channel count is unsupported.")
    values = framed.astype(np.float64, copy=False)
    if not np.all(np.isfinite(values)):
        raise ProjectValidationError("Crackle audio must contain finite samples.")
    sample_count = int(values.shape[0])
    channel_count = int(values.shape[1])
    scope.validate(sample_count)
    references = tuple(noise_references)
    if not 2 <= len(references) <= _MAX_REFERENCES:
        raise ProjectValidationError("Crackle analysis requires 2-64 noise references.")
    for reference in references:
        reference.validate(scope)
    if len({reference.label for reference in references}) != len(references):
        raise ProjectValidationError("Crackle noise-reference labels must be unique.")

    detected = detect_impulsive_clicks(
        values,
        threshold_sigma=cfg.threshold_sigma,
        local_window_samples=cfg.local_window_samples,
        min_slope_ratio=cfg.minimum_slope_ratio,
        max_click_samples=cfg.maximum_event_samples,
        max_gap_samples=0,
    )
    bounded = [
        event
        for event in detected
        if event.start_sample >= scope.start_sample + 1
        and event.end_sample < scope.end_sample_exclusive
    ]
    bounded.sort(key=lambda event: (event.start_sample, event.end_sample, event.channels))
    detected_count = len(bounded)
    stored = bounded if detected_count <= cfg.maximum_event_count else []
    reference_count = sum(
        1
        for event in stored
        if any(
            reference.start_sample <= event.peak_sample < reference.end_sample_exclusive
            for reference in references
        )
    )
    repaired_values = sum(event.length_samples * len(event.channels) for event in stored)
    selected_frames = scope.end_sample_exclusive - scope.start_sample
    duration = selected_frames / sample_rate
    event_payload = [_event_dict(event) for event in stored]
    metrics = {
        "detected_event_count": detected_count,
        "stored_event_count": len(stored),
        "reference_event_count": reference_count,
        "events_per_second": detected_count / duration,
        "repaired_sample_values": repaired_values,
        "repaired_sample_value_fraction": repaired_values
        / (selected_frames * channel_count),
        "median_confidence": (
            float(np.median([event.confidence for event in stored])) if stored else 0.0
        ),
        "event_set_sha256": canonical_json_sha256(event_payload),
    }
    reasons = sorted(_proposal_reasons(metrics, cfg))
    body: dict[str, Any] = {
        "schema": CRACKLE_PROPOSAL_SCHEMA,
        "algorithm_id": CRACKLE_ANALYSIS_ALGORITHM_ID,
        "sample_rate": sample_rate,
        "sample_count": sample_count,
        "channel_count": channel_count,
        "scope": scope.to_dict(),
        "noise_references": [reference.to_dict() for reference in references],
        "config": cfg.to_dict(),
        "events": event_payload,
        "metrics": metrics,
        "status": "proposed" if not reasons else "abstained",
        "abstention_reasons": reasons,
        "authority": _authority(),
    }
    proposal = dict(body)
    proposal["proposal_body_sha256"] = canonical_json_sha256(body)
    return CrackleProposal.from_dict(proposal)


def validate_crackle_proposal(value: Any) -> dict[str, Any]:
    data = _object(copy.deepcopy(value), "Crackle proposal")
    _keys(
        data,
        {
            "schema",
            "algorithm_id",
            "sample_rate",
            "sample_count",
            "channel_count",
            "scope",
            "noise_references",
            "config",
            "events",
            "metrics",
            "status",
            "abstention_reasons",
            "authority",
            "proposal_body_sha256",
        },
        "Crackle proposal",
    )
    if data["schema"] != CRACKLE_PROPOSAL_SCHEMA:
        raise ProjectValidationError("Crackle proposal schema is unsupported.")
    if data["algorithm_id"] != CRACKLE_ANALYSIS_ALGORITHM_ID:
        raise ProjectValidationError("Crackle analysis algorithm is unsupported.")
    sample_rate = _integer(data["sample_rate"], "Crackle sample rate", 8_000, 768_000)
    sample_count = _integer(data["sample_count"], "Crackle sample count", 3, 6_000_000)
    channel_count = _integer(data["channel_count"], "Crackle channels", 1, 32)
    scope = NoiseAnalysisScope.from_dict(data["scope"], sample_count)
    references = [
        NoiseReferenceRegion.from_dict(item, scope)
        for item in _array(data["noise_references"], "Crackle references")
    ]
    if not 2 <= len(references) <= _MAX_REFERENCES:
        raise ProjectValidationError("Crackle proposal requires 2-64 references.")
    if len({reference.label for reference in references}) != len(references):
        raise ProjectValidationError("Crackle reference labels must be unique.")
    cfg = CrackleAnalysisConfig.from_dict(data["config"])
    events = _parse_events(
        data["events"],
        sample_count=sample_count,
        channel_count=channel_count,
    )
    metrics = _object(data["metrics"], "Crackle metrics")
    _keys(
        metrics,
        {
            "detected_event_count",
            "stored_event_count",
            "reference_event_count",
            "events_per_second",
            "repaired_sample_values",
            "repaired_sample_value_fraction",
            "median_confidence",
            "event_set_sha256",
        },
        "Crackle metrics",
    )
    detected = _integer(
        metrics["detected_event_count"],
        "Crackle detected events",
        0,
        100_000_000,
    )
    stored_count = _integer(
        metrics["stored_event_count"],
        "Crackle stored events",
        0,
        cfg.maximum_event_count,
    )
    if stored_count != len(events):
        raise ProjectValidationError("Crackle stored-event count is inconsistent.")
    reference_count = _integer(
        metrics["reference_event_count"],
        "Crackle reference events",
        0,
        stored_count,
    )
    expected_reference = sum(
        1
        for event in events
        if any(
            reference.start_sample <= event.peak_sample < reference.end_sample_exclusive
            for reference in references
        )
    )
    if reference_count != expected_reference:
        raise ProjectValidationError("Crackle reference-event count is inconsistent.")
    duration = (scope.end_sample_exclusive - scope.start_sample) / sample_rate
    density = _number(
        metrics["events_per_second"],
        "Crackle event density",
        0.0,
        100_000_000.0,
    )
    if not math.isclose(density, detected / duration, rel_tol=1e-12, abs_tol=1e-12):
        raise ProjectValidationError("Crackle event density is inconsistent.")
    repaired_values = _integer(
        metrics["repaired_sample_values"],
        "Crackle repaired sample values",
        0,
        sample_count * channel_count,
    )
    expected_repaired = sum(event.length_samples * len(event.channels) for event in events)
    if repaired_values != expected_repaired:
        raise ProjectValidationError("Crackle repaired-value count is inconsistent.")
    fraction = _number(
        metrics["repaired_sample_value_fraction"],
        "Crackle repaired fraction",
        0.0,
        1.0,
    )
    expected_fraction = repaired_values / (
        (scope.end_sample_exclusive - scope.start_sample) * channel_count
    )
    if not math.isclose(fraction, expected_fraction, rel_tol=1e-12, abs_tol=1e-15):
        raise ProjectValidationError("Crackle repaired fraction is inconsistent.")
    median_confidence = _number(
        metrics["median_confidence"],
        "Crackle median confidence",
        0.0,
        1.0,
    )
    expected_median = (
        float(np.median([event.confidence for event in events])) if events else 0.0
    )
    if not math.isclose(
        median_confidence,
        expected_median,
        rel_tol=1e-12,
        abs_tol=1e-15,
    ):
        raise ProjectValidationError("Crackle confidence summary is inconsistent.")
    event_set_sha256 = _digest(
        metrics["event_set_sha256"],
        "Crackle event-set SHA-256",
    )
    if event_set_sha256 != canonical_json_sha256(
        [_event_dict(event) for event in events]
    ):
        raise ProjectValidationError("Crackle event-set identity is invalid.")
    reasons = _array(data["abstention_reasons"], "Crackle abstention reasons")
    if any(not isinstance(reason, str) for reason in reasons) or reasons != sorted(set(reasons)):
        raise ProjectValidationError("Crackle abstention reasons must be ordered and unique.")
    expected_reasons = sorted(_proposal_reasons(metrics, cfg))
    if reasons != expected_reasons:
        raise ProjectValidationError("Crackle proposal status reasons are inconsistent.")
    expected_status = "proposed" if not reasons else "abstained"
    if data["status"] != expected_status:
        raise ProjectValidationError("Crackle proposal status is inconsistent.")
    if detected <= cfg.maximum_event_count and detected != stored_count:
        raise ProjectValidationError("Crackle bounded event inventory is incomplete.")
    if detected > cfg.maximum_event_count and stored_count != 0:
        raise ProjectValidationError("Over-bound crackle evidence must abstain without events.")
    if data["authority"] != _authority():
        raise ProjectValidationError("Crackle proposal authority is invalid.")
    _digest(data["proposal_body_sha256"], "Crackle proposal SHA-256")
    body = dict(data)
    del body["proposal_body_sha256"]
    if canonical_json_sha256(body) != data["proposal_body_sha256"]:
        raise ProjectValidationError("Crackle proposal identity is invalid.")
    return data


def _validate_attestation(
    value: Any,
    proposal: CrackleProposal,
) -> dict[str, Any]:
    data = _object(copy.deepcopy(value), "Crackle review attestation")
    _keys(
        data,
        {
            "schema",
            "attestation_token",
            "decision",
            "proposal_body_sha256",
            "selected_scope",
            "acknowledgement",
        },
        "Crackle review attestation",
    )
    if data["schema"] != CRACKLE_REVIEW_ATTESTATION_SCHEMA:
        raise ProjectValidationError("Crackle review schema is unsupported.")
    token = _digest(data["attestation_token"], "Crackle attestation token")
    if len(set(token)) == 1:
        raise ProjectValidationError("Crackle attestation token is non-distinct.")
    if data["decision"] != REVIEW_DECISION:
        raise ProjectValidationError("Crackle review decision is unsupported.")
    if data["proposal_body_sha256"] != proposal.proposal_body_sha256:
        raise ProjectValidationError("Crackle attestation targets another proposal.")
    if data["selected_scope"] != proposal.scope:
        raise ProjectValidationError("Crackle attestation scope is stale.")
    if data["acknowledgement"] != REVIEW_ACKNOWLEDGEMENT:
        raise ProjectValidationError("Crackle review acknowledgement is required.")
    return data


@dataclass(frozen=True, slots=True)
class CracklePreviewRecipe:
    _data: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "CracklePreviewRecipe":
        return cls(validate_crackle_preview_recipe(value))

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    @property
    def recipe_sha256(self) -> str:
        return cast(str, self._data["recipe_sha256"])

    @property
    def config(self) -> CracklePreviewConfig:
        return CracklePreviewConfig.from_dict(self._data["config"])

    @property
    def events(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], copy.deepcopy(self._data["events"]))


def create_crackle_preview_recipe(
    proposal: CrackleProposal,
    review_attestation: Mapping[str, Any],
    *,
    config: CracklePreviewConfig | None = None,
) -> CracklePreviewRecipe:
    if proposal.status != "proposed":
        raise ProjectValidationError("An abstained crackle proposal cannot be previewed.")
    attestation = _validate_attestation(review_attestation, proposal)
    cfg = config or CracklePreviewConfig()
    cfg.validate()
    event_set_sha256 = canonical_json_sha256(proposal.events)
    body: dict[str, Any] = {
        "schema": CRACKLE_PREVIEW_RECIPE_SCHEMA,
        "algorithm_id": CRACKLE_PREVIEW_ALGORITHM_ID,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "selected_scope": proposal.scope,
        "events": proposal.events,
        "event_set_sha256": event_set_sha256,
        "config": cfg.to_dict(),
        "review_attestation": attestation,
        "authority": _authority(),
    }
    recipe = dict(body)
    recipe["recipe_sha256"] = canonical_json_sha256(body)
    return CracklePreviewRecipe.from_dict(recipe)


def validate_crackle_preview_recipe(value: Any) -> dict[str, Any]:
    data = _object(copy.deepcopy(value), "Crackle preview recipe")
    _keys(
        data,
        {
            "schema",
            "algorithm_id",
            "proposal_body_sha256",
            "selected_scope",
            "events",
            "event_set_sha256",
            "config",
            "review_attestation",
            "authority",
            "recipe_sha256",
        },
        "Crackle preview recipe",
    )
    if data["schema"] != CRACKLE_PREVIEW_RECIPE_SCHEMA:
        raise ProjectValidationError("Crackle preview recipe schema is unsupported.")
    if data["algorithm_id"] != CRACKLE_PREVIEW_ALGORITHM_ID:
        raise ProjectValidationError("Crackle preview algorithm is unsupported.")
    _digest(data["proposal_body_sha256"], "Crackle recipe proposal SHA-256")
    scope_data = _object(data["selected_scope"], "Crackle recipe scope")
    _keys(
        scope_data,
        {"label", "start_sample", "end_sample_exclusive"},
        "Crackle recipe scope",
    )
    start = _integer(scope_data["start_sample"], "Crackle recipe scope start", 0, 5_999_999)
    end = _integer(
        scope_data["end_sample_exclusive"],
        "Crackle recipe scope end",
        start + 3,
        6_000_000,
    )
    _text(scope_data["label"], "Crackle recipe scope label", 256)
    events = _parse_events(
        data["events"],
        sample_count=end,
        channel_count=32,
    )
    _digest(data["event_set_sha256"], "Crackle event-set SHA-256")
    if canonical_json_sha256([_event_dict(event) for event in events]) != data[
        "event_set_sha256"
    ]:
        raise ProjectValidationError("Crackle event-set identity is invalid.")
    CracklePreviewConfig.from_dict(data["config"])
    attestation = _object(data["review_attestation"], "Crackle recipe attestation")
    if (
        attestation.get("schema") != CRACKLE_REVIEW_ATTESTATION_SCHEMA
        or attestation.get("proposal_body_sha256") != data["proposal_body_sha256"]
        or attestation.get("selected_scope") != scope_data
        or attestation.get("acknowledgement") != REVIEW_ACKNOWLEDGEMENT
        or attestation.get("decision") != REVIEW_DECISION
    ):
        raise ProjectValidationError("Crackle recipe attestation is invalid.")
    token = _digest(attestation.get("attestation_token"), "Crackle recipe token")
    if len(set(token)) == 1:
        raise ProjectValidationError("Crackle recipe token is non-distinct.")
    if data["authority"] != _authority():
        raise ProjectValidationError("Crackle recipe authority is invalid.")
    _digest(data["recipe_sha256"], "Crackle recipe SHA-256")
    body = dict(data)
    del body["recipe_sha256"]
    if canonical_json_sha256(body) != data["recipe_sha256"]:
        raise ProjectValidationError("Crackle recipe identity is invalid.")
    return data


@dataclass(frozen=True, slots=True)
class CracklePreviewResult:
    original: np.ndarray
    proposed: np.ndarray
    removed: np.ndarray
    render_manifest: dict[str, Any]
    receipt: dict[str, Any]


def _pcm_sha256(values: np.ndarray) -> str:
    framed = np.ascontiguousarray(values, dtype="<f8")
    return hashlib.sha256(framed.tobytes(order="C")).hexdigest()


def _render_metrics(
    original: np.ndarray,
    proposed: np.ndarray,
    removed: np.ndarray,
    events: Sequence[ClickInterval],
) -> dict[str, Any]:
    framed = original[:, np.newaxis] if original.ndim == 1 else original
    mask = np.zeros(framed.shape, dtype=np.bool_)
    for event in events:
        for channel in event.channels:
            mask[event.start_sample : event.end_sample, channel] = True
    proposed_2d = proposed[:, np.newaxis] if proposed.ndim == 1 else proposed
    outside_changed = int(np.count_nonzero(proposed_2d[~mask] != framed[~mask]))
    changed = int(np.count_nonzero(proposed_2d != framed))
    reconstruction = proposed + removed
    peak_error = float(np.max(np.abs(reconstruction - original)))
    return {
        "event_count": len(events),
        "changed_sample_values": changed,
        "outside_event_changed_sample_values": outside_changed,
        "maximum_removed_absolute_sample": float(np.max(np.abs(removed))),
        "reconstruction_peak_error": peak_error,
    }


def render_crackle_preview(
    audio: np.ndarray,
    proposal: CrackleProposal,
    recipe: CracklePreviewRecipe,
) -> CracklePreviewResult:
    if proposal.status != "proposed":
        raise ProjectValidationError("An abstained crackle proposal cannot be rendered.")
    recipe_data = recipe.to_dict()
    if (
        recipe_data["proposal_body_sha256"] != proposal.proposal_body_sha256
        or recipe_data["events"] != proposal.events
        or recipe_data["selected_scope"] != proposal.scope
    ):
        raise ProjectValidationError("Crackle recipe does not bind this proposal.")
    source = np.asarray(audio)
    if source.ndim not in (1, 2) or not np.issubdtype(source.dtype, np.number):
        raise ProjectValidationError("Crackle preview audio geometry is invalid.")
    original = np.ascontiguousarray(source, dtype=np.float64)
    if not np.all(np.isfinite(original)):
        raise ProjectValidationError("Crackle preview audio must be finite.")
    framed = original[:, np.newaxis] if original.ndim == 1 else original
    events = _parse_events(
        recipe_data["events"],
        sample_count=framed.shape[0],
        channel_count=framed.shape[1],
    )
    cfg = recipe.config
    proposed = np.ascontiguousarray(
        repair_click_intervals(
            original,
            events,
            context_samples=cfg.context_samples,
            lpc_order=cfg.lpc_order,
        ),
        dtype=np.float64,
    )
    removed = original - proposed
    metrics = _render_metrics(original, proposed, removed, events)
    if metrics["outside_event_changed_sample_values"] != 0:
        raise ProjectValidationError("Crackle preview changed audio outside proposed events.")
    if metrics["changed_sample_values"] == 0:
        raise ProjectValidationError("Crackle preview made no bounded event changes.")
    if metrics["reconstruction_peak_error"] > 1e-12:
        raise ProjectValidationError("Crackle preview residue does not reconstruct the original.")

    original_rms = float(np.sqrt(np.mean(np.square(original), dtype=np.float64)))
    proposed_rms = float(np.sqrt(np.mean(np.square(proposed), dtype=np.float64)))
    ratio = (
        original_rms / proposed_rms
        if proposed_rms > np.finfo(np.float64).eps
        else 1.0
    )
    proposed_gain = float(
        np.clip(
            ratio,
            1.0 - cfg.maximum_linear_gain_delta,
            1.0 + cfg.maximum_linear_gain_delta,
        )
    )
    removed_rms = float(np.sqrt(np.mean(np.square(removed), dtype=np.float64)))
    residue_gain = float(
        min(
            cfg.maximum_residue_monitor_gain,
            cfg.residue_monitor_target_rms
            / max(removed_rms, np.finfo(np.float64).eps),
        )
    )
    render_body: dict[str, Any] = {
        "schema": CRACKLE_PREVIEW_RENDER_SCHEMA,
        "algorithm_id": CRACKLE_PREVIEW_ALGORITHM_ID,
        "recipe_sha256": recipe.recipe_sha256,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "frame_count": int(framed.shape[0]),
        "channel_count": int(framed.shape[1]),
        "original_pcm_f64le_sha256": _pcm_sha256(original),
        "proposed_pcm_f64le_sha256": _pcm_sha256(proposed),
        "removed_pcm_f64le_sha256": _pcm_sha256(removed),
        "metrics": metrics,
    }
    render = dict(render_body)
    render["render_sha256"] = canonical_json_sha256(render_body)
    validate_crackle_preview_render_manifest(render, recipe=recipe)

    receipt_body: dict[str, Any] = {
        "schema": CRACKLE_PREVIEW_RECEIPT_SCHEMA,
        "recipe_sha256": recipe.recipe_sha256,
        "render_sha256": render["render_sha256"],
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "event_set_sha256": recipe_data["event_set_sha256"],
        "metrics": metrics,
        "audition": {
            "original_linear_gain": 1.0,
            "proposed_linear_gain": proposed_gain,
            "residue_monitor_linear_gain": residue_gain,
            "matched_loudness_is_not_quality_approval": True,
        },
        "authority": _authority(),
    }
    receipt = dict(receipt_body)
    receipt["receipt_sha256"] = canonical_json_sha256(receipt_body)
    validate_crackle_preview_receipt(
        receipt,
        recipe=recipe,
        render_manifest=render,
    )
    return CracklePreviewResult(original, proposed, removed, render, receipt)


def validate_crackle_preview_render_manifest(
    value: Any,
    *,
    recipe: CracklePreviewRecipe,
) -> dict[str, Any]:
    data = _object(copy.deepcopy(value), "Crackle render manifest")
    _keys(
        data,
        {
            "schema",
            "algorithm_id",
            "recipe_sha256",
            "proposal_body_sha256",
            "frame_count",
            "channel_count",
            "original_pcm_f64le_sha256",
            "proposed_pcm_f64le_sha256",
            "removed_pcm_f64le_sha256",
            "metrics",
            "render_sha256",
        },
        "Crackle render manifest",
    )
    if (
        data["schema"] != CRACKLE_PREVIEW_RENDER_SCHEMA
        or data["algorithm_id"] != CRACKLE_PREVIEW_ALGORITHM_ID
        or data["recipe_sha256"] != recipe.recipe_sha256
        or data["proposal_body_sha256"]
        != recipe.to_dict()["proposal_body_sha256"]
    ):
        raise ProjectValidationError("Crackle render identity is invalid.")
    _integer(data["frame_count"], "Crackle render frames", 3, 6_000_000)
    _integer(data["channel_count"], "Crackle render channels", 1, 32)
    for key in (
        "original_pcm_f64le_sha256",
        "proposed_pcm_f64le_sha256",
        "removed_pcm_f64le_sha256",
        "render_sha256",
    ):
        _digest(data[key], f"Crackle render {key}")
    _validate_metrics(data["metrics"], "Crackle render metrics")
    body = dict(data)
    del body["render_sha256"]
    if canonical_json_sha256(body) != data["render_sha256"]:
        raise ProjectValidationError("Crackle render seal is invalid.")
    return data


def _validate_metrics(value: Any, label: str) -> dict[str, Any]:
    metrics = _object(value, label)
    _keys(
        metrics,
        {
            "event_count",
            "changed_sample_values",
            "outside_event_changed_sample_values",
            "maximum_removed_absolute_sample",
            "reconstruction_peak_error",
        },
        label,
    )
    _integer(metrics["event_count"], f"{label} events", 1, _MAX_EVENTS)
    _integer(
        metrics["changed_sample_values"],
        f"{label} changed values",
        1,
        6_000_000 * 32,
    )
    if metrics["outside_event_changed_sample_values"] != 0:
        raise ProjectValidationError(f"{label} changed values outside events.")
    _number(
        metrics["maximum_removed_absolute_sample"],
        f"{label} maximum removed sample",
        0.0,
        1_000_000.0,
    )
    _number(
        metrics["reconstruction_peak_error"],
        f"{label} reconstruction error",
        0.0,
        1e-12,
    )
    return metrics


def validate_crackle_preview_receipt(
    value: Any,
    *,
    recipe: CracklePreviewRecipe,
    render_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    data = _object(copy.deepcopy(value), "Crackle preview receipt")
    _keys(
        data,
        {
            "schema",
            "recipe_sha256",
            "render_sha256",
            "proposal_body_sha256",
            "event_set_sha256",
            "metrics",
            "audition",
            "authority",
            "receipt_sha256",
        },
        "Crackle preview receipt",
    )
    recipe_data = recipe.to_dict()
    if (
        data["schema"] != CRACKLE_PREVIEW_RECEIPT_SCHEMA
        or data["recipe_sha256"] != recipe.recipe_sha256
        or data["render_sha256"] != render_manifest["render_sha256"]
        or data["proposal_body_sha256"] != recipe_data["proposal_body_sha256"]
        or data["event_set_sha256"] != recipe_data["event_set_sha256"]
        or data["metrics"] != render_manifest["metrics"]
    ):
        raise ProjectValidationError("Crackle preview receipt identity is invalid.")
    _validate_metrics(data["metrics"], "Crackle receipt metrics")
    audition = _object(data["audition"], "Crackle audition")
    _keys(
        audition,
        {
            "original_linear_gain",
            "proposed_linear_gain",
            "residue_monitor_linear_gain",
            "matched_loudness_is_not_quality_approval",
        },
        "Crackle audition",
    )
    _number(audition["original_linear_gain"], "Original gain", 0.000001, 100.0)
    _number(audition["proposed_linear_gain"], "Proposed gain", 0.000001, 100.0)
    _number(audition["residue_monitor_linear_gain"], "Residue gain", 0.000001, 1_000.0)
    if audition["matched_loudness_is_not_quality_approval"] is not True:
        raise ProjectValidationError("Crackle audition cannot imply quality approval.")
    if data["authority"] != _authority():
        raise ProjectValidationError("Crackle preview authority is invalid.")
    _digest(data["receipt_sha256"], "Crackle receipt SHA-256")
    body = dict(data)
    del body["receipt_sha256"]
    if canonical_json_sha256(body) != data["receipt_sha256"]:
        raise ProjectValidationError("Crackle receipt seal is invalid.")
    return data


__all__ = [
    "CRACKLE_PREVIEW_RECEIPT_SCHEMA",
    "CRACKLE_PREVIEW_RECIPE_SCHEMA",
    "CRACKLE_PREVIEW_RENDER_SCHEMA",
    "CRACKLE_PROPOSAL_SCHEMA",
    "CRACKLE_REVIEW_ATTESTATION_SCHEMA",
    "REVIEW_ACKNOWLEDGEMENT",
    "CrackleAnalysisConfig",
    "CracklePreviewConfig",
    "CracklePreviewRecipe",
    "CracklePreviewResult",
    "CrackleProposal",
    "analyze_crackle",
    "create_crackle_preview_recipe",
    "render_crackle_preview",
    "validate_crackle_preview_receipt",
    "validate_crackle_preview_render_manifest",
    "validate_crackle_proposal",
]
