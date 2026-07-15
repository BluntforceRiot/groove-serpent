"""Read-only multimodal proposals for side music endpoints.

This module never edits a project, source capture, or marker.  It creates one
verified audio snapshot, derives waveform, spectral, and transient evidence
from the same exact source-sample windows, and emits review-required proposals
or explicit abstentions.
"""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import numpy as np

from . import __version__
from .atomic_create import rename_no_replace
from .audio_snapshot import verified_audio_snapshot
from .errors import ExportError, GrooveSerpentError, ProjectValidationError
from .media import find_tool, probe_audio, tool_version
from .models import Project, resolve_source_path
from .portable_names import portable_name_key
from .project_io import load_project_with_sha256
from .publication import (
    FileReceipt,
    assert_file_receipt,
    canonical_json_sha256,
    capture_file_receipt,
)
from .subprocess_policy import (
    join_diagnostic_reader,
    start_diagnostic_reader,
    terminate_and_reap,
)


ENDPOINT_PROPOSAL_SCHEMA = "groove-serpent.endpoint-proposals/1"
ENDPOINT_ALGORITHM_ID = "groove-serpent.multimodal-endpoints/1"
ENDPOINT_MODULE_ID = "groove_serpent.endpoint_proposals"
_MAX_PROPOSAL_BYTES = 8 * 1024 * 1024
_MAX_SCOPES = 32
_MAX_WINDOWS_PER_SCOPE = 100_000
_MAX_REASONS = 16
_MAX_CONFIRMATIONS = 16
_REPARSE_POINT = 0x400
_DIGEST_LENGTH = 64


def _finite(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectValidationError(f"{label} must be one finite number.")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ProjectValidationError(
            f"{label} must be between {minimum} and {maximum}."
        )
    return result


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectValidationError(
            f"{label} must be a JSON integer between {minimum} and {maximum}."
        )
    return value


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _text(value: Any, label: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectValidationError(
            f"{label} must be bounded, trimmed, printable text."
        )
    return value


def _strict_keys(value: Mapping[str, Any], required: set[str], label: str) -> None:
    if set(value) != required:
        missing = sorted(required - set(value))
        extra = sorted(set(value) - required)
        raise ProjectValidationError(
            f"{label} fields are invalid (missing={missing}, extra={extra})."
        )


def _quantized(value: float) -> float:
    rounded = round(float(value), 9)
    return 0.0 if rounded == 0.0 else rounded


@dataclass(frozen=True, slots=True)
class EndpointProposalConfig:
    """Deterministic, medium-agnostic endpoint evidence policy."""

    window_ms: int = 250
    spectral_fft_size: int = 2_048
    spectral_frames_per_window: int = 3
    waveform_activity_margin_db: float = 10.0
    spectral_activity_margin_db: float = 4.0
    maximum_spectral_flatness: float = 0.78
    spectral_flux_activity: float = 0.08
    minimum_dynamic_range_db: float = 8.0
    minimum_active_run_ms: int = 1_500
    maximum_quiet_bridge_ms: int = 500
    minimum_quiet_context_ms: int = 750
    maximum_family_spread_ms: int = 5_000
    needle_confirmation_radius_ms: int = 2_500
    transient_sigma: float = 12.0
    minimum_transient_derivative: float = 0.02

    def validate(self) -> None:
        _integer(self.window_ms, "Endpoint window length", 50, 2_000)
        fft_size = _integer(
            self.spectral_fft_size,
            "Endpoint spectral FFT size",
            256,
            16_384,
        )
        if fft_size & (fft_size - 1):
            raise ProjectValidationError(
                "Endpoint spectral FFT size must be a power of two."
            )
        _integer(
            self.spectral_frames_per_window,
            "Endpoint spectral frames per window",
            1,
            8,
        )
        _finite(
            self.waveform_activity_margin_db,
            "Waveform activity margin",
            1.0,
            60.0,
        )
        _finite(
            self.spectral_activity_margin_db,
            "Spectral activity margin",
            1.0,
            40.0,
        )
        _finite(
            self.maximum_spectral_flatness,
            "Maximum spectral flatness",
            0.01,
            1.0,
        )
        _finite(
            self.spectral_flux_activity,
            "Spectral flux activity",
            0.0,
            1.0,
        )
        _finite(
            self.minimum_dynamic_range_db,
            "Minimum endpoint dynamic range",
            1.0,
            80.0,
        )
        _integer(
            self.minimum_active_run_ms,
            "Minimum endpoint active run",
            250,
            30_000,
        )
        _integer(
            self.maximum_quiet_bridge_ms,
            "Maximum endpoint quiet bridge",
            0,
            5_000,
        )
        _integer(
            self.minimum_quiet_context_ms,
            "Minimum endpoint quiet context",
            0,
            30_000,
        )
        _integer(
            self.maximum_family_spread_ms,
            "Maximum endpoint family spread",
            0,
            30_000,
        )
        _integer(
            self.needle_confirmation_radius_ms,
            "Needle confirmation radius",
            0,
            30_000,
        )
        _finite(self.transient_sigma, "Transient sigma", 3.0, 100.0)
        _finite(
            self.minimum_transient_derivative,
            "Minimum transient derivative",
            0.000001,
            2.0,
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> EndpointProposalConfig:
        if not isinstance(value, dict):
            raise ProjectValidationError("Endpoint configuration must be a JSON object.")
        _strict_keys(value, set(cls.__dataclass_fields__), "Endpoint configuration")
        result = cls(**value)
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class EndpointScope:
    """One side-specific sample scope over the shared source capture."""

    label: str
    start_sample: int
    end_sample_exclusive: int

    def validate(self, source_sample_count: int) -> None:
        _text(self.label, "Endpoint scope label", maximum=64)
        _integer(
            self.start_sample,
            f"Endpoint scope {self.label} start",
            0,
            source_sample_count - 1,
        )
        _integer(
            self.end_sample_exclusive,
            f"Endpoint scope {self.label} end",
            1,
            source_sample_count,
        )
        if self.end_sample_exclusive <= self.start_sample:
            raise ProjectValidationError(
                f"Endpoint scope {self.label} must contain at least one sample."
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EndpointWindowFeature:
    """All evidence families computed from one exact PCM window."""

    start_sample: int
    end_sample_exclusive: int
    rms_dbfs: float
    peak_dbfs: float
    crest_factor: float
    spectral_centroid_hz: float
    spectral_flatness: float
    high_frequency_ratio: float
    spectral_flux: float
    derivative_peak: float
    impulse_count: int

    def validate(self, *, sample_rate: int) -> None:
        _integer(self.start_sample, "Feature start sample", 0, (1 << 63) - 1)
        _integer(
            self.end_sample_exclusive,
            "Feature end sample",
            1,
            (1 << 63) - 1,
        )
        if self.end_sample_exclusive <= self.start_sample:
            raise ProjectValidationError("Endpoint feature window is empty.")
        _finite(self.rms_dbfs, "Feature RMS", -180.0, 20.0)
        _finite(self.peak_dbfs, "Feature peak", -180.0, 20.0)
        _finite(self.crest_factor, "Feature crest factor", 0.0, 1_000_000.0)
        _finite(
            self.spectral_centroid_hz,
            "Feature spectral centroid",
            0.0,
            sample_rate / 2.0,
        )
        _finite(self.spectral_flatness, "Feature spectral flatness", 0.0, 1.0)
        _finite(
            self.high_frequency_ratio,
            "Feature high-frequency ratio",
            0.0,
            1.0,
        )
        _finite(self.spectral_flux, "Feature spectral flux", 0.0, 1.0)
        _finite(self.derivative_peak, "Feature derivative peak", 0.0, 2.0)
        _integer(self.impulse_count, "Feature impulse count", 0, 1_000_000)

    @property
    def sample_count(self) -> int:
        return self.end_sample_exclusive - self.start_sample


@dataclass(frozen=True, slots=True)
class EndpointScopeProposal:
    """One review-required proposal or explicit abstention."""

    label: str
    scope_start_sample: int
    scope_end_sample_exclusive: int
    status: Literal["proposed", "abstained"]
    proposed_music_start_sample: int | None
    proposed_music_end_sample_exclusive: int | None
    confidence: float
    reasons: tuple[str, ...]
    requires_review: bool
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "scope_start_sample": self.scope_start_sample,
            "scope_end_sample_exclusive": self.scope_end_sample_exclusive,
            "status": self.status,
            "proposed_music_start_sample": self.proposed_music_start_sample,
            "proposed_music_end_sample_exclusive": (
                self.proposed_music_end_sample_exclusive
            ),
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "requires_review": self.requires_review,
            "evidence": self.evidence,
        }


def _validate_scopes(
    scopes: Sequence[EndpointScope],
    source_sample_count: int,
) -> tuple[EndpointScope, ...]:
    if isinstance(scopes, (str, bytes, bytearray)):
        raise ProjectValidationError("Endpoint scopes must be a bounded sequence.")
    values = tuple(scopes)
    if not 1 <= len(values) <= _MAX_SCOPES:
        raise ProjectValidationError(f"Endpoint analysis requires 1-{_MAX_SCOPES} scopes.")
    labels: set[str] = set()
    previous_end = 0
    for index, scope in enumerate(values):
        if not isinstance(scope, EndpointScope):
            raise ProjectValidationError("Endpoint scopes must use EndpointScope values.")
        scope.validate(source_sample_count)
        key = portable_name_key(scope.label)
        if key in labels:
            raise ProjectValidationError("Endpoint scope labels must be portable-unique.")
        labels.add(key)
        if index and scope.start_sample < previous_end:
            raise ProjectValidationError(
                "Endpoint scopes must be ordered and may not overlap."
            )
        previous_end = scope.end_sample_exclusive
    return values


def _feature_hashes(
    features: Sequence[EndpointWindowFeature],
) -> dict[str, str]:
    waveform = [
        {
            "start_sample": item.start_sample,
            "end_sample_exclusive": item.end_sample_exclusive,
            "rms_dbfs": _quantized(item.rms_dbfs),
            "peak_dbfs": _quantized(item.peak_dbfs),
            "crest_factor": _quantized(item.crest_factor),
        }
        for item in features
    ]
    spectral = [
        {
            "start_sample": item.start_sample,
            "end_sample_exclusive": item.end_sample_exclusive,
            "spectral_centroid_hz": _quantized(item.spectral_centroid_hz),
            "spectral_flatness": _quantized(item.spectral_flatness),
            "high_frequency_ratio": _quantized(item.high_frequency_ratio),
            "spectral_flux": _quantized(item.spectral_flux),
        }
        for item in features
    ]
    transient = [
        {
            "start_sample": item.start_sample,
            "end_sample_exclusive": item.end_sample_exclusive,
            "derivative_peak": _quantized(item.derivative_peak),
            "impulse_count": item.impulse_count,
        }
        for item in features
    ]
    hashes = {
        "waveform_energy_sha256": canonical_json_sha256(waveform),
        "spectral_sha256": canonical_json_sha256(spectral),
        "transient_needle_sha256": canonical_json_sha256(transient),
    }
    hashes["combined_sha256"] = canonical_json_sha256(hashes)
    return hashes


def _ordered_percentile(values: Sequence[float], percentage: int) -> float:
    ordered = sorted(values)
    index = ((len(ordered) - 1) * percentage) // 100
    return ordered[index]


def _bridge_short_gaps(
    mask: Sequence[bool],
    features: Sequence[EndpointWindowFeature],
    maximum_gap_samples: int,
) -> list[bool]:
    result = list(mask)
    index = 0
    while index < len(result):
        if result[index]:
            index += 1
            continue
        start = index
        while index < len(result) and not result[index]:
            index += 1
        if start == 0 or index == len(result):
            continue
        gap_samples = sum(item.sample_count for item in features[start:index])
        if gap_samples <= maximum_gap_samples:
            result[start:index] = [True] * (index - start)
    return result


def _sustained_extent(
    mask: Sequence[bool],
    features: Sequence[EndpointWindowFeature],
    minimum_samples: int,
) -> tuple[int, int] | None:
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(mask):
        if not mask[index]:
            index += 1
            continue
        start = index
        sample_count = 0
        while index < len(mask) and mask[index]:
            sample_count += features[index].sample_count
            index += 1
        if sample_count >= minimum_samples:
            runs.append((start, index))
    if not runs:
        return None
    return runs[0][0], runs[-1][1]


def _extend_extent(
    extent: tuple[int, int] | None,
    continuation: Sequence[bool],
) -> tuple[int, int] | None:
    if extent is None:
        return None
    start, end = extent
    while start > 0 and continuation[start - 1]:
        start -= 1
    while end < len(continuation) and continuation[end]:
        end += 1
    return start, end


def _contiguous_context_samples(
    active: Sequence[bool],
    features: Sequence[EndpointWindowFeature],
    *,
    index: int,
    direction: Literal[-1, 1],
) -> int:
    total = 0
    current = index
    while 0 <= current < len(active) and not active[current]:
        total += features[current].sample_count
        current += direction
    return total


def _contiguous_quiet_tonal_samples(
    features: Sequence[EndpointWindowFeature],
    *,
    index: int,
    direction: Literal[-1, 1],
    maximum_flatness: float,
) -> int:
    total = 0
    current = index
    # A strict tonal threshold distinguishes a decaying musical component from
    # ordinary broadband floor.  If such structure continues beyond the chosen
    # threshold crossing, cutting is ambiguous and the proposal must abstain.
    tonal_limit = min(0.25, maximum_flatness / 3.0)
    while (
        0 <= current < len(features)
        and features[current].spectral_flatness <= tonal_limit
    ):
        total += features[current].sample_count
        current += direction
    return total


def _needle_confirmations(
    features: Sequence[EndpointWindowFeature],
    *,
    proposed_start: int,
    proposed_end: int,
    sample_rate: int,
    config: EndpointProposalConfig,
) -> list[dict[str, Any]]:
    derivatives = [item.derivative_peak for item in features]
    median = _ordered_percentile(derivatives, 50)
    deviations = [abs(value - median) for value in derivatives]
    mad = _ordered_percentile(deviations, 50)
    threshold = max(
        config.minimum_transient_derivative,
        median + config.transient_sigma * 1.4826 * max(mad, 1e-12),
    )
    radius = (sample_rate * config.needle_confirmation_radius_ms + 999) // 1_000
    confirmations: list[dict[str, Any]] = []
    for index, item in enumerate(features):
        if item.impulse_count == 0 or item.derivative_peak < threshold:
            continue
        before = features[max(0, index - 2) : index]
        after = features[index + 1 : min(len(features), index + 3)]
        before_db = (
            sum(value.rms_dbfs for value in before) / len(before)
            if before
            else item.rms_dbfs
        )
        after_db = (
            sum(value.rms_dbfs for value in after) / len(after)
            if after
            else item.rms_dbfs
        )
        sample = item.start_sample
        kind: str | None = None
        anchor = proposed_start
        if after_db - before_db >= 9.0 and abs(sample - proposed_start) <= radius:
            kind = "needle_drop_candidate"
        elif before_db - after_db >= 9.0 and abs(sample - proposed_end) <= radius:
            kind = "needle_pickup_candidate"
            anchor = proposed_end
        if kind is None:
            continue
        score = min(1.0, abs(after_db - before_db) / 30.0)
        confirmations.append(
            {
                "kind": kind,
                "sample": sample,
                "distance_from_structural_anchor_samples": abs(sample - anchor),
                "score": _quantized(score),
                "role": "confirmation-only",
                "protected_by_default": True,
            }
        )
        if len(confirmations) >= _MAX_CONFIRMATIONS:
            break
    return confirmations


def _proposal_evidence(
    features: Sequence[EndpointWindowFeature],
    *,
    sample_rate: int,
    noise_floor: float,
    dynamic_range: float,
    waveform_extent: tuple[int, int] | None,
    spectral_extent: tuple[int, int] | None,
    waveform_threshold: float,
    spectral_threshold: float,
    confirmations: Sequence[Mapping[str, Any]],
    start_context_samples: int,
    end_context_samples: int,
    quiet_tonal_before_start_samples: int,
    quiet_tonal_after_end_samples: int,
) -> dict[str, Any]:
    def extent_payload(value: tuple[int, int] | None) -> dict[str, int] | None:
        if value is None:
            return None
        return {
            "start_sample": features[value[0]].start_sample,
            "end_sample_exclusive": features[value[1] - 1].end_sample_exclusive,
        }

    evidence: dict[str, Any] = {
        "window_count": len(features),
        "first_window_samples": features[0].sample_count,
        "last_window_samples": features[-1].sample_count,
        "feature_hashes": _feature_hashes(features),
        "summary": {
            "noise_floor_dbfs": _quantized(noise_floor),
            "dynamic_range_db": _quantized(dynamic_range),
            "waveform_activity_threshold_dbfs": _quantized(waveform_threshold),
            "spectral_activity_threshold_dbfs": _quantized(spectral_threshold),
        },
        "family_candidates": {
            "waveform_energy": extent_payload(waveform_extent),
            "spectral_structure": extent_payload(spectral_extent),
        },
        "needle_confirmations": [dict(item) for item in confirmations],
        "transition_context": {
            "quiet_before_start_samples": start_context_samples,
            "quiet_after_end_samples": end_context_samples,
            "quiet_tonal_before_start_samples": (
                quiet_tonal_before_start_samples
            ),
            "quiet_tonal_after_end_samples": quiet_tonal_after_end_samples,
        },
        "policy": {
            "needle_events_are_confirmation_only": True,
            "quiet_tonal_regions_are_protected": True,
            "automatic_application_forbidden": True,
        },
    }
    evidence["evidence_sha256"] = canonical_json_sha256(evidence)
    return evidence


def propose_scope_endpoints(
    scope: EndpointScope,
    features: Sequence[EndpointWindowFeature],
    *,
    sample_rate: int,
    config: EndpointProposalConfig | None = None,
) -> EndpointScopeProposal:
    """Make a review-only proposal from already derived multimodal features."""

    settings = config or EndpointProposalConfig()
    settings.validate()
    _integer(sample_rate, "Endpoint sample rate", 1, 768_000)
    scope.validate((1 << 63) - 1)
    values = tuple(features)
    if not values or len(values) > _MAX_WINDOWS_PER_SCOPE:
        raise ProjectValidationError(
            "Endpoint features must be a non-empty bounded window sequence."
        )
    expected_start = scope.start_sample
    for item in values:
        if not isinstance(item, EndpointWindowFeature):
            raise ProjectValidationError(
                "Endpoint evidence must use EndpointWindowFeature values."
            )
        item.validate(sample_rate=sample_rate)
        if item.start_sample != expected_start:
            raise ProjectValidationError(
                "Endpoint feature windows must be exact, adjacent, and scope-bound."
            )
        expected_start = item.end_sample_exclusive
    if expected_start != scope.end_sample_exclusive:
        raise ProjectValidationError(
            "Endpoint feature windows do not reach the exact scope end."
        )

    energies = [item.rms_dbfs for item in values]
    # A side is normally music for far more than eighty percent of its scope.
    # The low tail, rather than a broad lower quintile, is the only defensible
    # baseline for short lead-in/runout contexts.  Sustained-run and spectral
    # agreement below still prevent isolated low windows from granting a cut.
    noise_floor = max(-120.0, _ordered_percentile(energies, 5))
    upper_energy = _ordered_percentile(energies, 90)
    dynamic_range = upper_energy - noise_floor
    waveform_threshold = noise_floor + settings.waveform_activity_margin_db
    spectral_threshold = noise_floor + settings.spectral_activity_margin_db
    waveform_mask = [
        item.rms_dbfs >= waveform_threshold
        and item.peak_dbfs >= waveform_threshold + 2.0
        for item in values
    ]
    spectral_mask = [
        item.rms_dbfs >= spectral_threshold
        and (
            item.spectral_flatness <= settings.maximum_spectral_flatness
            or item.spectral_flux >= settings.spectral_flux_activity
        )
        for item in values
    ]
    spectral_continuation = [
        item.rms_dbfs
        >= noise_floor + max(1.0, settings.spectral_activity_margin_db / 2.0)
        and item.spectral_flatness <= settings.maximum_spectral_flatness
        for item in values
    ]
    maximum_gap = (
        sample_rate * settings.maximum_quiet_bridge_ms + 999
    ) // 1_000
    waveform_mask = _bridge_short_gaps(waveform_mask, values, maximum_gap)
    spectral_mask = _bridge_short_gaps(spectral_mask, values, maximum_gap)
    spectral_continuation = _bridge_short_gaps(
        spectral_continuation,
        values,
        maximum_gap,
    )
    minimum_active = (
        sample_rate * settings.minimum_active_run_ms + 999
    ) // 1_000
    waveform_extent = _sustained_extent(waveform_mask, values, minimum_active)
    spectral_extent = _sustained_extent(spectral_mask, values, minimum_active)
    spectral_extent = _extend_extent(spectral_extent, spectral_continuation)

    combined_active = [
        waveform or spectral
        for waveform, spectral in zip(waveform_mask, spectral_mask, strict=True)
    ]
    empty_confirmations: list[dict[str, Any]] = []
    if dynamic_range < settings.minimum_dynamic_range_db:
        evidence = _proposal_evidence(
            values,
            sample_rate=sample_rate,
            noise_floor=noise_floor,
            dynamic_range=dynamic_range,
            waveform_extent=waveform_extent,
            spectral_extent=spectral_extent,
            waveform_threshold=waveform_threshold,
            spectral_threshold=spectral_threshold,
            confirmations=empty_confirmations,
            start_context_samples=0,
            end_context_samples=0,
            quiet_tonal_before_start_samples=0,
            quiet_tonal_after_end_samples=0,
        )
        return EndpointScopeProposal(
            scope.label,
            scope.start_sample,
            scope.end_sample_exclusive,
            "abstained",
            None,
            None,
            0.0,
            ("silence_or_insufficient_dynamic_range",),
            True,
            evidence,
        )
    if waveform_extent is None or spectral_extent is None:
        evidence = _proposal_evidence(
            values,
            sample_rate=sample_rate,
            noise_floor=noise_floor,
            dynamic_range=dynamic_range,
            waveform_extent=waveform_extent,
            spectral_extent=spectral_extent,
            waveform_threshold=waveform_threshold,
            spectral_threshold=spectral_threshold,
            confirmations=empty_confirmations,
            start_context_samples=0,
            end_context_samples=0,
            quiet_tonal_before_start_samples=0,
            quiet_tonal_after_end_samples=0,
        )
        return EndpointScopeProposal(
            scope.label,
            scope.start_sample,
            scope.end_sample_exclusive,
            "abstained",
            None,
            None,
            0.0,
            ("insufficient_cross_family_evidence",),
            True,
            evidence,
        )

    waveform_start = values[waveform_extent[0]].start_sample
    waveform_end = values[waveform_extent[1] - 1].end_sample_exclusive
    spectral_start = values[spectral_extent[0]].start_sample
    spectral_end = values[spectral_extent[1] - 1].end_sample_exclusive
    start_spread = abs(waveform_start - spectral_start)
    end_spread = abs(waveform_end - spectral_end)
    maximum_spread = (
        sample_rate * settings.maximum_family_spread_ms + 999
    ) // 1_000
    proposed_start = min(waveform_start, spectral_start)
    proposed_end = max(waveform_end, spectral_end)
    start_index = next(
        index for index, item in enumerate(values) if item.start_sample == proposed_start
    )
    end_index = next(
        index
        for index, item in enumerate(values)
        if item.end_sample_exclusive == proposed_end
    )
    start_context = _contiguous_context_samples(
        combined_active,
        values,
        index=start_index - 1,
        direction=-1,
    )
    end_context = _contiguous_context_samples(
        combined_active,
        values,
        index=end_index + 1,
        direction=1,
    )
    quiet_tonal_before = _contiguous_quiet_tonal_samples(
        values,
        index=start_index - 1,
        direction=-1,
        maximum_flatness=settings.maximum_spectral_flatness,
    )
    quiet_tonal_after = _contiguous_quiet_tonal_samples(
        values,
        index=end_index + 1,
        direction=1,
        maximum_flatness=settings.maximum_spectral_flatness,
    )
    confirmations = _needle_confirmations(
        values,
        proposed_start=proposed_start,
        proposed_end=proposed_end,
        sample_rate=sample_rate,
        config=settings,
    )
    evidence = _proposal_evidence(
        values,
        sample_rate=sample_rate,
        noise_floor=noise_floor,
        dynamic_range=dynamic_range,
        waveform_extent=waveform_extent,
        spectral_extent=spectral_extent,
        waveform_threshold=waveform_threshold,
        spectral_threshold=spectral_threshold,
        confirmations=confirmations,
        start_context_samples=start_context,
        end_context_samples=end_context,
        quiet_tonal_before_start_samples=quiet_tonal_before,
        quiet_tonal_after_end_samples=quiet_tonal_after,
    )
    if start_spread > maximum_spread or end_spread > maximum_spread:
        return EndpointScopeProposal(
            scope.label,
            scope.start_sample,
            scope.end_sample_exclusive,
            "abstained",
            None,
            None,
            0.0,
            ("contradictory_endpoint_families",),
            True,
            evidence,
        )
    if quiet_tonal_before > maximum_gap or quiet_tonal_after > maximum_gap:
        return EndpointScopeProposal(
            scope.label,
            scope.start_sample,
            scope.end_sample_exclusive,
            "abstained",
            None,
            None,
            0.0,
            ("quiet_intro_or_tail_transition_ambiguous",),
            True,
            evidence,
        )
    minimum_context = (
        sample_rate * settings.minimum_quiet_context_ms + 999
    ) // 1_000
    if start_context < minimum_context or end_context < minimum_context:
        return EndpointScopeProposal(
            scope.label,
            scope.start_sample,
            scope.end_sample_exclusive,
            "abstained",
            None,
            None,
            0.0,
            ("scope_boundary_truncated_or_transition_ambiguous",),
            True,
            evidence,
        )
    if proposed_end <= proposed_start:
        return EndpointScopeProposal(
            scope.label,
            scope.start_sample,
            scope.end_sample_exclusive,
            "abstained",
            None,
            None,
            0.0,
            ("invalid_or_ambiguous_endpoint_order",),
            True,
            evidence,
        )

    spread_quality = 1.0 - max(start_spread, end_spread) / max(1, maximum_spread)
    dynamic_quality = min(1.0, dynamic_range / 36.0)
    confidence = min(0.95, max(0.5, 0.55 + 0.2 * spread_quality + 0.2 * dynamic_quality))
    reasons = ["cross_family_endpoint_agreement", "human_review_required"]
    if spectral_start < waveform_start or spectral_end > waveform_end:
        reasons.insert(1, "quiet_tonal_extent_protected")
    if confirmations:
        reasons.insert(-1, "needle_morphology_confirms_structural_anchor_only")
    return EndpointScopeProposal(
        scope.label,
        scope.start_sample,
        scope.end_sample_exclusive,
        "proposed",
        proposed_start,
        proposed_end,
        _quantized(confidence),
        tuple(reasons),
        True,
        evidence,
    )


def _feature_from_pcm(
    pcm: np.ndarray,
    *,
    start_sample: int,
    sample_rate: int,
    config: EndpointProposalConfig,
    previous_spectrum: np.ndarray | None,
) -> tuple[EndpointWindowFeature, np.ndarray]:
    values = pcm.astype(np.float64, copy=False)
    if values.size == 0 or not np.all(np.isfinite(values)):
        raise GrooveSerpentError("Endpoint decoder returned invalid floating PCM.")
    power = float(np.mean(np.square(values, dtype=np.float64)))
    rms = math.sqrt(max(power, 0.0))
    peak = float(np.max(np.abs(values)))
    rms_db = 20.0 * math.log10(max(rms, 1e-9))
    peak_db = 20.0 * math.log10(max(peak, 1e-9))
    crest = peak / max(rms, 1e-12)

    fft_size = config.spectral_fft_size
    if values.shape[0] < fft_size:
        padded = np.pad(values, ((0, fft_size - values.shape[0]), (0, 0)))
        starts = np.asarray([0], dtype=np.int64)
    else:
        padded = values
        available = values.shape[0] - fft_size
        frame_count = min(config.spectral_frames_per_window, 1 + available // fft_size)
        starts = np.linspace(0, available, max(1, frame_count), dtype=np.int64)
    window = np.hanning(fft_size)[:, np.newaxis]
    spectra: list[np.ndarray] = []
    for offset in starts:
        segment = padded[int(offset) : int(offset) + fft_size]
        transformed = np.fft.rfft(segment * window, axis=0)
        spectra.append(np.mean(np.square(np.abs(transformed)), axis=1))
    spectrum = np.mean(np.asarray(spectra), axis=0)
    spectrum = np.asarray(spectrum, dtype=np.float64)
    total = max(float(np.sum(spectrum)), 1e-18)
    normalized = spectrum / total
    frequencies = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
    centroid = float(np.sum(frequencies * spectrum) / total)
    positive = spectrum[1:]
    flatness = float(
        math.exp(float(np.mean(np.log(np.maximum(positive, 1e-18)))))
        / max(float(np.mean(positive)), 1e-18)
    )
    high_cutoff = min(5_000.0, sample_rate / 4.0)
    high_ratio = float(np.sum(spectrum[frequencies >= high_cutoff]) / total)
    flux = (
        float(np.sum(np.maximum(normalized - previous_spectrum, 0.0)))
        if previous_spectrum is not None
        else 0.0
    )

    derivative = np.max(
        np.abs(np.diff(values, axis=0, prepend=values[0:1])),
        axis=1,
    )
    derivative_median = float(np.median(derivative))
    derivative_mad = float(np.median(np.abs(derivative - derivative_median)))
    threshold = max(
        config.minimum_transient_derivative,
        derivative_median
        + config.transient_sigma * 1.4826 * max(derivative_mad, 1e-12),
    )
    candidates = np.flatnonzero(derivative >= threshold)
    suppression = max(1, (sample_rate * 15 + 999) // 1_000)
    impulse_count = 0
    previous = -suppression - 1
    for raw_index in candidates:
        index = int(raw_index)
        if index - previous > suppression:
            impulse_count += 1
            previous = index

    feature = EndpointWindowFeature(
        start_sample=start_sample,
        end_sample_exclusive=start_sample + values.shape[0],
        rms_dbfs=_quantized(rms_db),
        peak_dbfs=_quantized(peak_db),
        crest_factor=_quantized(crest),
        spectral_centroid_hz=_quantized(centroid),
        spectral_flatness=_quantized(min(1.0, max(0.0, flatness))),
        high_frequency_ratio=_quantized(min(1.0, max(0.0, high_ratio))),
        spectral_flux=_quantized(min(1.0, max(0.0, flux))),
        derivative_peak=_quantized(min(2.0, float(np.max(derivative)))),
        impulse_count=impulse_count,
    )
    feature.validate(sample_rate=sample_rate)
    return feature, normalized


def _read_exact_pipe(stream: Any, byte_count: int) -> bytes:
    result = bytearray()
    while len(result) < byte_count:
        chunk = stream.read(byte_count - len(result))
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


def _decode_scope_features(
    snapshot_path: Path,
    scope: EndpointScope,
    *,
    sample_rate: int,
    channels: int,
    config: EndpointProposalConfig,
) -> tuple[EndpointWindowFeature, ...]:
    ffmpeg = find_tool("ffmpeg")
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(snapshot_path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        (
            f"atrim=start_sample={scope.start_sample}:"
            f"end_sample={scope.end_sample_exclusive},"
            f"asettb=expr=1/{sample_rate},asetpts=N"
        ),
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]
    process: subprocess.Popen[bytes] | None = None
    diagnostic_thread = None
    completed = False
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        diagnostic, diagnostic_thread = start_diagnostic_reader(
            process,
            name="groove-serpent-endpoint-stderr",
        )
        if process.stdout is None:
            raise GrooveSerpentError("Endpoint decoder did not expose PCM output.")
        window_samples = max(1, (sample_rate * config.window_ms + 500) // 1_000)
        remaining = scope.end_sample_exclusive - scope.start_sample
        current_sample = scope.start_sample
        previous_spectrum: np.ndarray | None = None
        features: list[EndpointWindowFeature] = []
        frame_bytes = channels * 4
        while remaining:
            frames = min(window_samples, remaining)
            raw = _read_exact_pipe(process.stdout, frames * frame_bytes)
            if len(raw) != frames * frame_bytes:
                raise GrooveSerpentError(
                    "Endpoint decoder ended before the exact requested scope boundary."
                )
            pcm = np.frombuffer(raw, dtype="<f4").reshape(frames, channels)
            feature, previous_spectrum = _feature_from_pcm(
                pcm,
                start_sample=current_sample,
                sample_rate=sample_rate,
                config=config,
                previous_spectrum=previous_spectrum,
            )
            features.append(feature)
            if len(features) > _MAX_WINDOWS_PER_SCOPE:
                raise ProjectValidationError(
                    "Endpoint scope exceeds the bounded feature-window limit."
                )
            current_sample += frames
            remaining -= frames
        if process.stdout.read(1):
            raise GrooveSerpentError(
                "Endpoint decoder returned samples beyond the exact requested scope."
            )
        process.stdout.close()
        return_code = process.wait()
        join_diagnostic_reader(process, diagnostic_thread)
        completed = True
        if return_code != 0:
            raise GrooveSerpentError(
                "FFmpeg could not decode endpoint evidence: " + diagnostic.text()
            )
        return tuple(features)
    finally:
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
        if not completed:
            terminate_and_reap(process)
        join_diagnostic_reader(process, diagnostic_thread)


def _source_identity(project: Project) -> dict[str, Any]:
    source = project.source
    if source.sample_count is None:
        raise ProjectValidationError(
            "Endpoint analysis requires an exact source sample count."
        )
    return {
        "sha256": _digest(source.sha256, "Project source SHA-256"),
        "size_bytes": source.size_bytes,
        "sample_rate": source.sample_rate,
        "channels": source.channels,
        "bits_per_raw_sample": source.bits_per_raw_sample,
        "sample_count": source.sample_count,
        "codec_name": source.codec_name,
    }


def _verify_snapshot_geometry(project: Project, snapshot_path: Path) -> None:
    current = probe_audio(snapshot_path, stored_path=snapshot_path.name)
    expected = project.source
    if (
        current.sha256 != expected.sha256
        or current.size_bytes != expected.size_bytes
        or current.sample_rate != expected.sample_rate
        or current.channels != expected.channels
        or current.bits_per_raw_sample != expected.bits_per_raw_sample
        or current.sample_count != expected.sample_count
        or current.codec_name != expected.codec_name
    ):
        raise ProjectValidationError(
            "Verified endpoint snapshot geometry differs from the project source."
        )


def analyze_endpoint_proposals(
    project_path: Path,
    scopes: Sequence[EndpointScope],
    *,
    config: EndpointProposalConfig | None = None,
    snapshot_workspace: Path | None = None,
) -> dict[str, Any]:
    """Analyze one immutable project/source state without applying any change."""

    settings = config or EndpointProposalConfig()
    settings.validate()
    normalized_project_path = Path(
        os.path.abspath(os.fspath(project_path.expanduser()))
    )
    project_receipt = capture_file_receipt(
        normalized_project_path,
        label="Endpoint project",
    )
    project, project_sha256 = load_project_with_sha256(normalized_project_path)
    project.validate()
    if project_sha256 != project_receipt.sha256:
        raise ProjectValidationError("Endpoint project changed while it was loaded.")
    source_identity = _source_identity(project)
    source_sample_count = int(source_identity["sample_count"])
    validated_scopes = _validate_scopes(scopes, source_sample_count)
    source_path = resolve_source_path(project, normalized_project_path)
    module_path = Path(__file__)
    module_receipt = capture_file_receipt(module_path, label="Endpoint proposal module")
    ffmpeg_version = tool_version("ffmpeg")
    proposals: list[dict[str, Any]] = []
    with verified_audio_snapshot(
        source_path,
        expected_sha256=str(source_identity["sha256"]),
        expected_size_bytes=int(source_identity["size_bytes"]),
        workspace=snapshot_workspace,
        label="Endpoint source audio",
    ) as snapshot:
        _verify_snapshot_geometry(project, snapshot.path)
        snapshot.assert_snapshot_unchanged(force=True)
        for scope in validated_scopes:
            snapshot.assert_snapshot_identity()
            features = _decode_scope_features(
                snapshot.path,
                scope,
                sample_rate=project.source.sample_rate,
                channels=project.source.channels,
                config=settings,
            )
            snapshot.assert_snapshot_unchanged(force=True)
            proposals.append(
                propose_scope_endpoints(
                    scope,
                    features,
                    sample_rate=project.source.sample_rate,
                    config=settings,
                ).to_dict()
            )
        snapshot.assert_snapshot_unchanged(force=True)
        snapshot_identity = {
            "sha256": snapshot.sha256,
            "size_bytes": snapshot.size_bytes,
            "verified_copy": True,
        }

    assert_file_receipt(
        normalized_project_path,
        project_receipt,
        label="Endpoint project",
    )
    assert_file_receipt(module_path, module_receipt, label="Endpoint proposal module")
    configuration_values = settings.to_dict()
    document: dict[str, Any] = {
        "schema": ENDPOINT_PROPOSAL_SCHEMA,
        "algorithm": {
            "id": ENDPOINT_ALGORITHM_ID,
            "module": ENDPOINT_MODULE_ID,
            "module_sha256": module_receipt.sha256,
            "app_version": __version__,
            "ffmpeg_version": ffmpeg_version,
        },
        "project": {
            "sha256": project_sha256,
            "revision": project.revision,
            "state_sha256": project.state_sha256,
        },
        "source": source_identity,
        "configuration": {
            "values": configuration_values,
            "sha256": canonical_json_sha256(configuration_values),
        },
        "snapshot": snapshot_identity,
        "scopes": proposals,
    }
    document["proposal_sha256"] = canonical_json_sha256(document)
    validate_endpoint_proposal_document(document)
    return document


def _validate_nullable_sample(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    return _integer(value, label, minimum, maximum)


def _validate_evidence(
    value: Any,
    *,
    scope_start: int,
    scope_end: int,
) -> None:
    if not isinstance(value, dict):
        raise ProjectValidationError("Endpoint evidence must be a JSON object.")
    _strict_keys(
        value,
        {
            "window_count",
            "first_window_samples",
            "last_window_samples",
            "feature_hashes",
            "summary",
            "family_candidates",
            "needle_confirmations",
            "transition_context",
            "policy",
            "evidence_sha256",
        },
        "Endpoint evidence",
    )
    _integer(value["window_count"], "Endpoint window count", 1, _MAX_WINDOWS_PER_SCOPE)
    _integer(
        value["first_window_samples"],
        "Endpoint first window length",
        1,
        scope_end - scope_start,
    )
    _integer(
        value["last_window_samples"],
        "Endpoint last window length",
        1,
        scope_end - scope_start,
    )
    hashes = value["feature_hashes"]
    if not isinstance(hashes, dict):
        raise ProjectValidationError("Endpoint feature hashes must be a JSON object.")
    _strict_keys(
        hashes,
        {
            "waveform_energy_sha256",
            "spectral_sha256",
            "transient_needle_sha256",
            "combined_sha256",
        },
        "Endpoint feature hashes",
    )
    for key, digest_value in hashes.items():
        _digest(digest_value, f"Endpoint feature {key}")
    combined_basis = {
        key: hashes[key]
        for key in (
            "waveform_energy_sha256",
            "spectral_sha256",
            "transient_needle_sha256",
        )
    }
    if hashes["combined_sha256"] != canonical_json_sha256(combined_basis):
        raise ProjectValidationError("Endpoint combined feature hash is inconsistent.")
    summary = value["summary"]
    if not isinstance(summary, dict):
        raise ProjectValidationError("Endpoint evidence summary must be a JSON object.")
    _strict_keys(
        summary,
        {
            "noise_floor_dbfs",
            "dynamic_range_db",
            "waveform_activity_threshold_dbfs",
            "spectral_activity_threshold_dbfs",
        },
        "Endpoint evidence summary",
    )
    _finite(summary["noise_floor_dbfs"], "Endpoint noise floor", -180.0, 20.0)
    _finite(summary["dynamic_range_db"], "Endpoint dynamic range", 0.0, 200.0)
    _finite(
        summary["waveform_activity_threshold_dbfs"],
        "Endpoint waveform threshold",
        -180.0,
        80.0,
    )
    _finite(
        summary["spectral_activity_threshold_dbfs"],
        "Endpoint spectral threshold",
        -180.0,
        80.0,
    )
    family = value["family_candidates"]
    if not isinstance(family, dict):
        raise ProjectValidationError("Endpoint family candidates must be a JSON object.")
    _strict_keys(
        family,
        {"waveform_energy", "spectral_structure"},
        "Endpoint family candidates",
    )
    for name, candidate in family.items():
        if candidate is None:
            continue
        if not isinstance(candidate, dict):
            raise ProjectValidationError(f"Endpoint family {name} must be an object or null.")
        _strict_keys(
            candidate,
            {"start_sample", "end_sample_exclusive"},
            f"Endpoint family {name}",
        )
        start = _integer(
            candidate["start_sample"],
            f"Endpoint family {name} start",
            scope_start,
            scope_end - 1,
        )
        end = _integer(
            candidate["end_sample_exclusive"],
            f"Endpoint family {name} end",
            start + 1,
            scope_end,
        )
        if end <= start:
            raise ProjectValidationError(f"Endpoint family {name} has invalid order.")
    confirmations = value["needle_confirmations"]
    if not isinstance(confirmations, list) or len(confirmations) > _MAX_CONFIRMATIONS:
        raise ProjectValidationError("Endpoint needle confirmations are invalid.")
    for confirmation in confirmations:
        if not isinstance(confirmation, dict):
            raise ProjectValidationError("Endpoint needle confirmation must be an object.")
        _strict_keys(
            confirmation,
            {
                "kind",
                "sample",
                "distance_from_structural_anchor_samples",
                "score",
                "role",
                "protected_by_default",
            },
            "Endpoint needle confirmation",
        )
        if confirmation["kind"] not in {
            "needle_drop_candidate",
            "needle_pickup_candidate",
        }:
            raise ProjectValidationError("Endpoint needle confirmation kind is invalid.")
        _integer(
            confirmation["sample"],
            "Endpoint needle sample",
            scope_start,
            scope_end - 1,
        )
        _integer(
            confirmation["distance_from_structural_anchor_samples"],
            "Endpoint needle distance",
            0,
            scope_end - scope_start,
        )
        _finite(confirmation["score"], "Endpoint needle score", 0.0, 1.0)
        if (
            confirmation["role"] != "confirmation-only"
            or confirmation["protected_by_default"] is not True
        ):
            raise ProjectValidationError(
                "Endpoint needle events must remain protected confirmation-only evidence."
            )
    context = value["transition_context"]
    if not isinstance(context, dict):
        raise ProjectValidationError("Endpoint transition context must be an object.")
    _strict_keys(
        context,
        {
            "quiet_before_start_samples",
            "quiet_after_end_samples",
            "quiet_tonal_before_start_samples",
            "quiet_tonal_after_end_samples",
        },
        "Endpoint transition context",
    )
    for key, context_value in context.items():
        _integer(
            context_value,
            f"Endpoint transition {key}",
            0,
            scope_end - scope_start,
        )
    policy = value["policy"]
    if not isinstance(policy, dict):
        raise ProjectValidationError("Endpoint evidence policy must be an object.")
    _strict_keys(
        policy,
        {
            "needle_events_are_confirmation_only",
            "quiet_tonal_regions_are_protected",
            "automatic_application_forbidden",
        },
        "Endpoint evidence policy",
    )
    if any(item is not True for item in policy.values()):
        raise ProjectValidationError("Endpoint evidence policy protections are mandatory.")
    expected_evidence_hash = value["evidence_sha256"]
    _digest(expected_evidence_hash, "Endpoint evidence SHA-256")
    without_hash = {key: value[key] for key in value if key != "evidence_sha256"}
    if expected_evidence_hash != canonical_json_sha256(without_hash):
        raise ProjectValidationError("Endpoint evidence SHA-256 is inconsistent.")


def validate_endpoint_proposal_document(value: Any) -> dict[str, Any]:
    """Strictly validate a bounded proposal document without blessing it."""

    if not isinstance(value, dict):
        raise ProjectValidationError("Endpoint proposal root must be a JSON object.")
    _strict_keys(
        value,
        {
            "schema",
            "algorithm",
            "project",
            "source",
            "configuration",
            "snapshot",
            "scopes",
            "proposal_sha256",
        },
        "Endpoint proposal",
    )
    if value["schema"] != ENDPOINT_PROPOSAL_SCHEMA:
        raise ProjectValidationError("Endpoint proposal schema is unsupported.")
    algorithm = value["algorithm"]
    if not isinstance(algorithm, dict):
        raise ProjectValidationError("Endpoint algorithm identity must be an object.")
    _strict_keys(
        algorithm,
        {"id", "module", "module_sha256", "app_version", "ffmpeg_version"},
        "Endpoint algorithm identity",
    )
    if algorithm["id"] != ENDPOINT_ALGORITHM_ID or algorithm["module"] != ENDPOINT_MODULE_ID:
        raise ProjectValidationError("Endpoint algorithm identity is unsupported.")
    _digest(algorithm["module_sha256"], "Endpoint module SHA-256")
    _text(algorithm["app_version"], "Endpoint app version", maximum=64)
    _text(algorithm["ffmpeg_version"], "Endpoint FFmpeg version", maximum=1_024)
    project = value["project"]
    if not isinstance(project, dict):
        raise ProjectValidationError("Endpoint project identity must be an object.")
    _strict_keys(
        project,
        {"sha256", "revision", "state_sha256"},
        "Endpoint project identity",
    )
    _digest(project["sha256"], "Endpoint project SHA-256")
    _integer(project["revision"], "Endpoint project revision", 1, (1 << 63) - 1)
    _digest(project["state_sha256"], "Endpoint state SHA-256")
    source = value["source"]
    if not isinstance(source, dict):
        raise ProjectValidationError("Endpoint source identity must be an object.")
    _strict_keys(
        source,
        {
            "sha256",
            "size_bytes",
            "sample_rate",
            "channels",
            "bits_per_raw_sample",
            "sample_count",
            "codec_name",
        },
        "Endpoint source identity",
    )
    _digest(source["sha256"], "Endpoint source SHA-256")
    _integer(source["size_bytes"], "Endpoint source size", 1, (1 << 63) - 1)
    _integer(
        source["sample_rate"],
        "Endpoint source sample rate",
        1,
        768_000,
    )
    _integer(source["channels"], "Endpoint source channels", 1, 64)
    if source["bits_per_raw_sample"] is not None:
        _integer(source["bits_per_raw_sample"], "Endpoint source bit depth", 1, 64)
    sample_count = _integer(
        source["sample_count"],
        "Endpoint source sample count",
        1,
        (1 << 63) - 1,
    )
    _text(source["codec_name"], "Endpoint source codec", maximum=64)
    configuration = value["configuration"]
    if not isinstance(configuration, dict):
        raise ProjectValidationError("Endpoint configuration binding must be an object.")
    _strict_keys(configuration, {"values", "sha256"}, "Endpoint configuration binding")
    settings = EndpointProposalConfig.from_dict(configuration["values"])
    _digest(configuration["sha256"], "Endpoint configuration SHA-256")
    if configuration["sha256"] != canonical_json_sha256(settings.to_dict()):
        raise ProjectValidationError("Endpoint configuration SHA-256 is inconsistent.")
    snapshot = value["snapshot"]
    if not isinstance(snapshot, dict):
        raise ProjectValidationError("Endpoint snapshot identity must be an object.")
    _strict_keys(snapshot, {"sha256", "size_bytes", "verified_copy"}, "Endpoint snapshot")
    if (
        snapshot["sha256"] != source["sha256"]
        or snapshot["size_bytes"] != source["size_bytes"]
        or snapshot["verified_copy"] is not True
    ):
        raise ProjectValidationError("Endpoint snapshot is not bound to the source identity.")
    scopes = value["scopes"]
    if not isinstance(scopes, list) or not 1 <= len(scopes) <= _MAX_SCOPES:
        raise ProjectValidationError("Endpoint proposal scopes are invalid.")
    labels: set[str] = set()
    previous_end = 0
    for raw_scope in scopes:
        if not isinstance(raw_scope, dict):
            raise ProjectValidationError("Endpoint proposal scope must be an object.")
        _strict_keys(
            raw_scope,
            {
                "label",
                "scope_start_sample",
                "scope_end_sample_exclusive",
                "status",
                "proposed_music_start_sample",
                "proposed_music_end_sample_exclusive",
                "confidence",
                "reasons",
                "requires_review",
                "evidence",
            },
            "Endpoint proposal scope",
        )
        label = _text(raw_scope["label"], "Endpoint scope label", maximum=64)
        key = portable_name_key(label)
        if key in labels:
            raise ProjectValidationError("Endpoint proposal scope labels repeat.")
        labels.add(key)
        scope_start = _integer(
            raw_scope["scope_start_sample"],
            f"Endpoint scope {label} start",
            0,
            sample_count - 1,
        )
        scope_end = _integer(
            raw_scope["scope_end_sample_exclusive"],
            f"Endpoint scope {label} end",
            scope_start + 1,
            sample_count,
        )
        if scope_start < previous_end:
            raise ProjectValidationError("Endpoint proposal scopes overlap or are unordered.")
        previous_end = scope_end
        status = raw_scope["status"]
        if status not in {"proposed", "abstained"}:
            raise ProjectValidationError("Endpoint proposal status is invalid.")
        proposed_start = _validate_nullable_sample(
            raw_scope["proposed_music_start_sample"],
            f"Endpoint scope {label} proposed start",
            minimum=scope_start,
            maximum=scope_end - 1,
        )
        proposed_end = _validate_nullable_sample(
            raw_scope["proposed_music_end_sample_exclusive"],
            f"Endpoint scope {label} proposed end",
            minimum=scope_start + 1,
            maximum=scope_end,
        )
        confidence = _finite(
            raw_scope["confidence"],
            f"Endpoint scope {label} confidence",
            0.0,
            1.0,
        )
        if status == "proposed":
            if (
                proposed_start is None
                or proposed_end is None
                or proposed_end <= proposed_start
                or confidence <= 0.0
            ):
                raise ProjectValidationError(
                    "A proposed endpoint scope requires ordered samples and confidence."
                )
        elif proposed_start is not None or proposed_end is not None or confidence != 0.0:
            raise ProjectValidationError(
                "An abstained endpoint scope cannot contain a hidden proposal."
            )
        reasons = raw_scope["reasons"]
        if (
            not isinstance(reasons, list)
            or not 1 <= len(reasons) <= _MAX_REASONS
            or len(set(reasons)) != len(reasons)
        ):
            raise ProjectValidationError("Endpoint proposal reasons are invalid.")
        for reason in reasons:
            _text(reason, "Endpoint proposal reason", maximum=128)
        if raw_scope["requires_review"] is not True:
            raise ProjectValidationError("Every endpoint proposal requires human review.")
        _validate_evidence(
            raw_scope["evidence"],
            scope_start=scope_start,
            scope_end=scope_end,
        )
    expected_hash = _digest(value["proposal_sha256"], "Endpoint proposal SHA-256")
    without_hash = {key: value[key] for key in value if key != "proposal_sha256"}
    if expected_hash != canonical_json_sha256(without_hash):
        raise ProjectValidationError("Endpoint proposal SHA-256 is inconsistent.")
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number {value!r} is forbidden.")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key {key!r}.")
        result[key] = value
    return result


def _read_strict_document(path: Path) -> tuple[dict[str, Any], FileReceipt]:
    normalized = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        metadata = normalized.lstat()
    except OSError as exc:
        raise ProjectValidationError("Endpoint proposal could not be inspected.") from exc
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(metadata.st_mode)
        or attributes & _REPARSE_POINT
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > _MAX_PROPOSAL_BYTES
    ):
        raise ProjectValidationError(
            "Endpoint proposal must be one bounded ordinary non-reparse file."
        )
    try:
        receipt = capture_file_receipt(normalized, label="Endpoint proposal")
        raw = normalized.read_bytes()
        assert_file_receipt(normalized, receipt, label="Endpoint proposal")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except (ExportError, OSError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise ProjectValidationError(f"Endpoint proposal is not strict JSON: {exc}") from exc
    if len(raw) > _MAX_PROPOSAL_BYTES or not isinstance(value, dict):
        raise ProjectValidationError("Endpoint proposal root is invalid or oversized.")
    return value, receipt


def load_endpoint_proposal_document(path: Path) -> dict[str, Any]:
    """Load and strictly validate one deterministic proposal document."""

    value, _receipt = _read_strict_document(path)
    return validate_endpoint_proposal_document(value)


def write_endpoint_proposal_document(
    value: Mapping[str, Any],
    path: Path,
) -> FileReceipt:
    """Atomically create one validated proposal; an existing path is never replaced."""

    document = validate_endpoint_proposal_document(dict(value))
    raw = (
        json.dumps(
            document,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(raw) > _MAX_PROPOSAL_BYTES:
        raise ProjectValidationError("Endpoint proposal exceeds its bounded file size.")
    destination = Path(os.path.abspath(os.fspath(path.expanduser())))
    try:
        parent_metadata = destination.parent.lstat()
    except OSError as exc:
        raise ProjectValidationError(
            "Endpoint proposal parent directory does not exist."
        ) from exc
    attributes = int(getattr(parent_metadata, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(parent_metadata.st_mode)
        or attributes & _REPARSE_POINT
        or not stat.S_ISDIR(parent_metadata.st_mode)
    ):
        raise ProjectValidationError(
            "Endpoint proposal parent must be an ordinary non-reparse directory."
        )
    if os.path.lexists(destination):
        raise ProjectValidationError("Endpoint proposal output already exists.")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            rename_no_replace(temporary, destination)
        except FileExistsError as exc:
            raise ProjectValidationError(
                "Endpoint proposal output appeared before publication."
            ) from exc
        receipt = capture_file_receipt(destination, label="Endpoint proposal")
        if receipt.size_bytes != len(raw):
            raise ProjectValidationError("Endpoint proposal write was incomplete.")
        return receipt
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = [
    "ENDPOINT_ALGORITHM_ID",
    "ENDPOINT_PROPOSAL_SCHEMA",
    "EndpointProposalConfig",
    "EndpointScope",
    "EndpointScopeProposal",
    "EndpointWindowFeature",
    "analyze_endpoint_proposals",
    "load_endpoint_proposal_document",
    "propose_scope_endpoints",
    "validate_endpoint_proposal_document",
    "write_endpoint_proposal_document",
]
