"""Deterministic, evidence-only hum and rumble proposals from PCM arrays.

This module is deliberately narrower than a restoration engine.  It does not
design filters, render audio, mutate project state, or approve a change.  A
caller supplies an explicit analysis scope plus at least two regions asserted
to contain only source noise.  The analyzer compares multiple non-overlapping
windows in every channel and emits either review-required evidence or a
conservative abstention.

Hum evidence looks for a stationary 50 or 60 Hz fundamental together with at
least one independently persistent harmonic.  Rumble evidence looks for
diffuse, stationary energy in 5--30 Hz; a concentrated or strongly modulated
low-frequency component remains ambiguous with musical bass and is rejected.
These tests reduce false positives but cannot prove that a component is not
part of the recording.  Owner audition and visual review therefore remain
mandatory.

The spectral calculations use only NumPy's documented real FFT and Hann
window primitives.  See the NumPy reference for ``numpy.fft.rfft`` and
``numpy.hanning``.  No claim is made that these proposal thresholds generalize
to every cartridge, turntable, master, or musical genre.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

import numpy as np

from .errors import ProjectValidationError
from .publication import canonical_json_sha256
from .validation import strict_finite_number

CONTINUOUS_NOISE_DOCUMENT_SCHEMA = "groove-serpent.continuous-noise-proposals/1"
HUM_PROPOSAL_SCHEMA = "groove-serpent.hum-proposal/1"
RUMBLE_PROPOSAL_SCHEMA = "groove-serpent.rumble-proposal/1"
CONTINUOUS_NOISE_ALGORITHM_ID = "groove-serpent.continuous-noise-evidence/1"
CONTINUOUS_NOISE_MODULE_ID = "groove_serpent.continuous_noise"
FLOAT_CONTRACT = "normalized-float-pcm-f64le/1"

_DIGEST_CHARACTERS = frozenset("0123456789abcdef")
_REFERENCE_ROLES = frozenset({"lead_in", "lead_out", "inter_track", "user_selected"})
_MAX_CHANNELS = 32
_MAX_TEXT_LENGTH = 128
_EPSILON = np.finfo(np.float64).tiny
_HUM_REASONS = frozenset(
    {
        "50_60_identity_ambiguous",
        "channels_or_references_disagree",
        "clipping_invalidates_continuous_noise_evidence",
        "harmonics_do_not_agree_across_channels",
        "insufficient_program_windows",
        "insufficient_reference_windows",
        "isolated_tone_or_musical_bass_ambiguous",
        "line_not_confirmed_in_program_windows",
        "line_not_persistent_across_references",
        "reference_level_suggests_program_audio",
        "reference_signal_is_temporally_unstable",
        "silence_or_signal_below_analysis_floor",
        "stationary_line_and_harmonics_agree_across_noise_references_channels_and_program",
    }
)
_RUMBLE_REASONS = frozenset(
    {
        "channels_or_references_disagree",
        "clipping_invalidates_continuous_noise_evidence",
        "diffuse_rumble_not_persistent_across_references",
        "diffuse_stationary_low_frequency_energy_agrees_across_"
        "noise_references_channels_and_program",
        "insufficient_program_windows",
        "insufficient_reference_windows",
        "plausible_low_frequency_music_or_tone",
        "reference_level_suggests_program_audio",
        "reference_signal_is_temporally_unstable",
        "rumble_not_confirmed_in_program_windows",
        "silence_or_signal_below_analysis_floor",
    }
)


def _strict_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ProjectValidationError(
            f"{label} fields are invalid (missing={missing}, extra={extra})."
        )


def _object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProjectValidationError(f"{label} must be a JSON object.")
    return cast(dict[str, Any], value)


def _array(value: Any, label: str) -> list[Any]:
    if type(value) is not list:
        raise ProjectValidationError(f"{label} must be a JSON array.")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectValidationError(
            f"{label} must be a JSON integer between {minimum} and {maximum}."
        )
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    result = strict_finite_number(value, label)
    if not minimum <= result <= maximum:
        raise ProjectValidationError(f"{label} must be between {minimum} and {maximum}.")
    return result


def _text(value: Any, label: str, maximum: int = _MAX_TEXT_LENGTH) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ProjectValidationError(f"{label} must be bounded, trimmed, nonempty printable text.")
    return value


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST_CHARACTERS for character in value)
    ):
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _quantize(value: float) -> float:
    result = round(float(value), 9)
    return 0.0 if result == 0.0 else result


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(np.median(np.asarray(values, dtype=np.float64)))


def _spread(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    data = np.asarray(values, dtype=np.float64)
    return float(np.max(data) - np.min(data))


@dataclass(frozen=True, slots=True)
class ContinuousNoiseConfig:
    """Version-one deterministic thresholds serialized into every document."""

    window_ms: int = 2_000
    minimum_reference_windows: int = 2
    minimum_program_windows: int = 2
    silence_rms_dbfs: float = -88.0
    clipping_amplitude: float = 0.999
    maximum_reference_rms_dbfs: float = -24.0
    maximum_reference_rms_spread_db: float = 8.0
    hum_line_half_width_hz: float = 0.8
    hum_neighborhood_half_width_hz: float = 6.0
    hum_line_excess_db: float = 10.0
    hum_harmonic_excess_db: float = 8.0
    hum_reference_persistence: float = 0.75
    hum_program_persistence: float = 0.25
    hum_identity_margin_db: float = 4.0
    hum_maximum_peak_spread_hz: float = 1.0
    rumble_lower_hz: float = 5.0
    rumble_upper_hz: float = 30.0
    rumble_comparison_lower_hz: float = 40.0
    rumble_comparison_upper_hz: float = 160.0
    rumble_low_to_bass_db: float = 5.0
    rumble_minimum_low_fraction: float = 0.30
    rumble_minimum_flatness: float = 0.08
    rumble_maximum_peak_concentration: float = 0.22
    rumble_reference_persistence: float = 0.75
    rumble_program_persistence: float = 0.25
    rumble_maximum_ratio_spread_db: float = 8.0

    def validate(self) -> None:
        _integer(self.window_ms, "Continuous-noise window length", 500, 10_000)
        _integer(
            self.minimum_reference_windows,
            "Minimum reference windows",
            2,
            100,
        )
        _integer(self.minimum_program_windows, "Minimum program windows", 1, 10_000)
        _number(self.silence_rms_dbfs, "Silence RMS threshold", -160.0, -40.0)
        _number(self.clipping_amplitude, "Clipping amplitude", 0.9, 1.0)
        _number(
            self.maximum_reference_rms_dbfs,
            "Maximum reference RMS",
            -80.0,
            -6.0,
        )
        _number(
            self.maximum_reference_rms_spread_db,
            "Maximum reference RMS spread",
            0.1,
            40.0,
        )
        _number(self.hum_line_half_width_hz, "Hum line half width", 0.1, 2.0)
        neighborhood = _number(
            self.hum_neighborhood_half_width_hz,
            "Hum neighborhood half width",
            3.0,
            20.0,
        )
        if neighborhood <= self.hum_line_half_width_hz * 2.0:
            raise ProjectValidationError(
                "Hum neighborhood must be wider than twice the line half width."
            )
        _number(self.hum_line_excess_db, "Hum line excess", 3.0, 60.0)
        _number(self.hum_harmonic_excess_db, "Hum harmonic excess", 3.0, 60.0)
        _number(self.hum_reference_persistence, "Hum reference persistence", 0.5, 1.0)
        _number(self.hum_program_persistence, "Hum program persistence", 0.0, 1.0)
        _number(self.hum_identity_margin_db, "Hum identity margin", 0.5, 30.0)
        _number(
            self.hum_maximum_peak_spread_hz,
            "Hum maximum peak spread",
            0.0,
            3.0,
        )
        lower = _number(self.rumble_lower_hz, "Rumble lower frequency", 1.0, 20.0)
        upper = _number(self.rumble_upper_hz, "Rumble upper frequency", 15.0, 40.0)
        comparison_lower = _number(
            self.rumble_comparison_lower_hz,
            "Rumble comparison lower frequency",
            30.0,
            100.0,
        )
        comparison_upper = _number(
            self.rumble_comparison_upper_hz,
            "Rumble comparison upper frequency",
            80.0,
            500.0,
        )
        if not lower < upper < comparison_lower < comparison_upper:
            raise ProjectValidationError(
                "Rumble and comparison frequency bands must be strictly ordered."
            )
        _number(self.rumble_low_to_bass_db, "Rumble band excess", 0.0, 40.0)
        _number(
            self.rumble_minimum_low_fraction,
            "Rumble minimum low fraction",
            0.05,
            0.95,
        )
        _number(self.rumble_minimum_flatness, "Rumble minimum flatness", 0.001, 1.0)
        _number(
            self.rumble_maximum_peak_concentration,
            "Rumble maximum peak concentration",
            0.01,
            1.0,
        )
        _number(
            self.rumble_reference_persistence,
            "Rumble reference persistence",
            0.5,
            1.0,
        )
        _number(
            self.rumble_program_persistence,
            "Rumble program persistence",
            0.0,
            1.0,
        )
        _number(
            self.rumble_maximum_ratio_spread_db,
            "Rumble maximum ratio spread",
            0.1,
            40.0,
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> ContinuousNoiseConfig:
        fields = set(cls.__dataclass_fields__)
        data = _object(value, "Continuous-noise configuration")
        _strict_keys(data, fields, "Continuous-noise configuration")
        result = cls(**data)
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class NoiseAnalysisScope:
    label: str
    start_sample: int
    end_sample_exclusive: int

    def validate(self, sample_count: int) -> None:
        _text(self.label, "Analysis scope label")
        _integer(self.start_sample, "Analysis scope start", 0, sample_count - 1)
        _integer(
            self.end_sample_exclusive,
            "Analysis scope end",
            1,
            sample_count,
        )
        if self.end_sample_exclusive <= self.start_sample:
            raise ProjectValidationError("Analysis scope must contain at least one sample.")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any, sample_count: int) -> NoiseAnalysisScope:
        data = _object(value, "Analysis scope")
        _strict_keys(data, set(cls.__dataclass_fields__), "Analysis scope")
        result = cls(**data)
        result.validate(sample_count)
        return result


@dataclass(frozen=True, slots=True)
class NoiseReferenceRegion:
    """A caller-asserted noise-only sample interval inside the analysis scope."""

    label: str
    role: Literal["lead_in", "lead_out", "inter_track", "user_selected"]
    start_sample: int
    end_sample_exclusive: int

    def validate(self, scope: NoiseAnalysisScope) -> None:
        _text(self.label, "Noise-reference label")
        if self.role not in _REFERENCE_ROLES:
            raise ProjectValidationError("Noise-reference role is unsupported.")
        _integer(
            self.start_sample,
            f"Noise-reference {self.label} start",
            scope.start_sample,
            scope.end_sample_exclusive - 1,
        )
        _integer(
            self.end_sample_exclusive,
            f"Noise-reference {self.label} end",
            scope.start_sample + 1,
            scope.end_sample_exclusive,
        )
        if self.end_sample_exclusive <= self.start_sample:
            raise ProjectValidationError(
                f"Noise-reference {self.label} must contain at least one sample."
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls,
        value: Any,
        scope: NoiseAnalysisScope,
    ) -> NoiseReferenceRegion:
        data = _object(value, "Noise-reference region")
        _strict_keys(data, set(cls.__dataclass_fields__), "Noise-reference region")
        result = cls(**data)
        result.validate(scope)
        return result


@dataclass(frozen=True, slots=True)
class HumRegionChannelEvidence:
    region_label: str
    region_role: Literal["reference", "program"]
    channel_index: int
    window_count: int
    median_rms_dbfs: float
    rms_spread_db: float
    candidate_50_score_db: float
    candidate_50_persistence: float
    candidate_50_peak_spread_hz: float
    candidate_50_harmonics: tuple[int, ...]
    candidate_60_score_db: float
    candidate_60_persistence: float
    candidate_60_peak_spread_hz: float
    candidate_60_harmonics: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["candidate_50_harmonics"] = list(self.candidate_50_harmonics)
        value["candidate_60_harmonics"] = list(self.candidate_60_harmonics)
        return value

    @classmethod
    def from_dict(cls, value: Any, channel_count: int) -> HumRegionChannelEvidence:
        data = _object(value, "Hum region/channel evidence")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hum region/channel evidence")
        for key in ("candidate_50_harmonics", "candidate_60_harmonics"):
            values = _array(data[key], f"Hum evidence {key}")
            if any(type(item) is not int or item not in (1, 2, 3, 4) for item in values):
                raise ProjectValidationError(f"Hum evidence {key} is invalid.")
            if values != sorted(set(values)):
                raise ProjectValidationError(f"Hum evidence {key} must be ordered and unique.")
        result = cls(
            region_label=_text(data["region_label"], "Hum evidence region label"),
            region_role=cast(Literal["reference", "program"], data["region_role"]),
            channel_index=_integer(
                data["channel_index"],
                "Hum evidence channel",
                0,
                channel_count - 1,
            ),
            window_count=_integer(data["window_count"], "Hum evidence windows", 0, 1_000_000),
            median_rms_dbfs=_number(data["median_rms_dbfs"], "Hum evidence RMS", -400.0, 1.0),
            rms_spread_db=_number(data["rms_spread_db"], "Hum evidence RMS spread", 0.0, 400.0),
            candidate_50_score_db=_number(
                data["candidate_50_score_db"], "50 Hz score", -200.0, 400.0
            ),
            candidate_50_persistence=_number(
                data["candidate_50_persistence"], "50 Hz persistence", 0.0, 1.0
            ),
            candidate_50_peak_spread_hz=_number(
                data["candidate_50_peak_spread_hz"], "50 Hz peak spread", 0.0, 100.0
            ),
            candidate_50_harmonics=tuple(cast(list[int], data["candidate_50_harmonics"])),
            candidate_60_score_db=_number(
                data["candidate_60_score_db"], "60 Hz score", -200.0, 400.0
            ),
            candidate_60_persistence=_number(
                data["candidate_60_persistence"], "60 Hz persistence", 0.0, 1.0
            ),
            candidate_60_peak_spread_hz=_number(
                data["candidate_60_peak_spread_hz"], "60 Hz peak spread", 0.0, 100.0
            ),
            candidate_60_harmonics=tuple(cast(list[int], data["candidate_60_harmonics"])),
        )
        if result.region_role not in {"reference", "program"}:
            raise ProjectValidationError("Hum evidence region role is invalid.")
        return result


@dataclass(frozen=True, slots=True)
class RumbleRegionChannelEvidence:
    region_label: str
    region_role: Literal["reference", "program"]
    channel_index: int
    window_count: int
    median_rms_dbfs: float
    rms_spread_db: float
    median_low_to_bass_db: float
    low_to_bass_spread_db: float
    median_low_fraction: float
    median_low_flatness: float
    median_peak_concentration: float
    qualifying_persistence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(
        cls,
        value: Any,
        channel_count: int,
    ) -> RumbleRegionChannelEvidence:
        data = _object(value, "Rumble region/channel evidence")
        _strict_keys(
            data,
            set(cls.__dataclass_fields__),
            "Rumble region/channel evidence",
        )
        result = cls(
            region_label=_text(data["region_label"], "Rumble evidence region label"),
            region_role=cast(Literal["reference", "program"], data["region_role"]),
            channel_index=_integer(
                data["channel_index"],
                "Rumble evidence channel",
                0,
                channel_count - 1,
            ),
            window_count=_integer(data["window_count"], "Rumble evidence windows", 0, 1_000_000),
            median_rms_dbfs=_number(data["median_rms_dbfs"], "Rumble evidence RMS", -400.0, 1.0),
            rms_spread_db=_number(data["rms_spread_db"], "Rumble evidence RMS spread", 0.0, 400.0),
            median_low_to_bass_db=_number(
                data["median_low_to_bass_db"], "Rumble band ratio", -400.0, 400.0
            ),
            low_to_bass_spread_db=_number(
                data["low_to_bass_spread_db"], "Rumble ratio spread", 0.0, 800.0
            ),
            median_low_fraction=_number(
                data["median_low_fraction"], "Rumble low fraction", 0.0, 1.0
            ),
            median_low_flatness=_number(
                data["median_low_flatness"], "Rumble low flatness", 0.0, 1.0
            ),
            median_peak_concentration=_number(
                data["median_peak_concentration"], "Rumble peak concentration", 0.0, 1.0
            ),
            qualifying_persistence=_number(
                data["qualifying_persistence"], "Rumble persistence", 0.0, 1.0
            ),
        )
        if result.region_role not in {"reference", "program"}:
            raise ProjectValidationError("Rumble evidence region role is invalid.")
        return result


@dataclass(frozen=True, slots=True)
class HumProposal:
    schema: str
    status: Literal["proposed", "abstained"]
    confidence: float
    fundamental_hz: int | None
    detected_harmonics: tuple[int, ...]
    reasons: tuple[str, ...]
    requires_review: bool
    evidence: tuple[HumRegionChannelEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "confidence": self.confidence,
            "fundamental_hz": self.fundamental_hz,
            "detected_harmonics": list(self.detected_harmonics),
            "reasons": list(self.reasons),
            "requires_review": self.requires_review,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, value: Any, channel_count: int) -> HumProposal:
        data = _object(value, "Hum proposal")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hum proposal")
        if data["schema"] != HUM_PROPOSAL_SCHEMA:
            raise ProjectValidationError("Hum proposal schema is unsupported.")
        status = data["status"]
        if status not in {"proposed", "abstained"}:
            raise ProjectValidationError("Hum proposal status is invalid.")
        confidence = _number(data["confidence"], "Hum confidence", 0.0, 1.0)
        fundamental = data["fundamental_hz"]
        if fundamental is not None and fundamental not in (50, 60):
            raise ProjectValidationError("Hum fundamental must be 50 Hz, 60 Hz, or null.")
        harmonic_values = _array(data["detected_harmonics"], "Detected hum harmonics")
        if any(type(item) is not int or item not in (1, 2, 3, 4) for item in harmonic_values):
            raise ProjectValidationError("Detected hum harmonics are invalid.")
        if harmonic_values != sorted(set(harmonic_values)):
            raise ProjectValidationError("Detected hum harmonics must be ordered and unique.")
        reasons = tuple(
            _text(item, "Hum proposal reason")
            for item in _array(data["reasons"], "Hum proposal reasons")
        )
        if len(reasons) != 1 or reasons[0] not in _HUM_REASONS:
            raise ProjectValidationError("Hum proposal must contain one supported reason.")
        if data["requires_review"] is not True:
            raise ProjectValidationError("Hum proposals always require review.")
        evidence = tuple(
            HumRegionChannelEvidence.from_dict(item, channel_count)
            for item in _array(data["evidence"], "Hum proposal evidence")
        )
        if status == "proposed":
            if confidence <= 0.0:
                raise ProjectValidationError(
                    "A proposed hum must have positive evidence consistency."
                )
            if fundamental is None or 1 not in harmonic_values or len(harmonic_values) < 2:
                raise ProjectValidationError(
                    "A proposed hum must bind a fundamental and persistent harmonic."
                )
        elif fundamental is not None or harmonic_values or confidence != 0.0:
            raise ProjectValidationError(
                "An abstained hum proposal cannot hide a target or confidence."
            )
        return cls(
            schema=HUM_PROPOSAL_SCHEMA,
            status=cast(Literal["proposed", "abstained"], status),
            confidence=confidence,
            fundamental_hz=cast(int | None, fundamental),
            detected_harmonics=tuple(cast(list[int], harmonic_values)),
            reasons=reasons,
            requires_review=True,
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class RumbleProposal:
    schema: str
    status: Literal["proposed", "abstained"]
    confidence: float
    observed_lower_hz: float | None
    observed_upper_hz: float | None
    reasons: tuple[str, ...]
    requires_review: bool
    evidence: tuple[RumbleRegionChannelEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "confidence": self.confidence,
            "observed_lower_hz": self.observed_lower_hz,
            "observed_upper_hz": self.observed_upper_hz,
            "reasons": list(self.reasons),
            "requires_review": self.requires_review,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, value: Any, channel_count: int) -> RumbleProposal:
        data = _object(value, "Rumble proposal")
        _strict_keys(data, set(cls.__dataclass_fields__), "Rumble proposal")
        if data["schema"] != RUMBLE_PROPOSAL_SCHEMA:
            raise ProjectValidationError("Rumble proposal schema is unsupported.")
        status = data["status"]
        if status not in {"proposed", "abstained"}:
            raise ProjectValidationError("Rumble proposal status is invalid.")
        confidence = _number(data["confidence"], "Rumble confidence", 0.0, 1.0)
        lower_value = data["observed_lower_hz"]
        upper_value = data["observed_upper_hz"]
        lower = (
            None
            if lower_value is None
            else _number(lower_value, "Observed rumble lower frequency", 1.0, 40.0)
        )
        upper = (
            None
            if upper_value is None
            else _number(upper_value, "Observed rumble upper frequency", 1.0, 80.0)
        )
        reasons = tuple(
            _text(item, "Rumble proposal reason")
            for item in _array(data["reasons"], "Rumble proposal reasons")
        )
        if len(reasons) != 1 or reasons[0] not in _RUMBLE_REASONS:
            raise ProjectValidationError("Rumble proposal must contain one supported reason.")
        if data["requires_review"] is not True:
            raise ProjectValidationError("Rumble proposals always require review.")
        evidence = tuple(
            RumbleRegionChannelEvidence.from_dict(item, channel_count)
            for item in _array(data["evidence"], "Rumble proposal evidence")
        )
        if status == "proposed":
            if confidence <= 0.0:
                raise ProjectValidationError(
                    "A proposed rumble must have positive evidence consistency."
                )
            if lower is None or upper is None or not lower < upper:
                raise ProjectValidationError("A proposed rumble must bind a valid observed band.")
        elif lower is not None or upper is not None or confidence != 0.0:
            raise ProjectValidationError(
                "An abstained rumble proposal cannot hide a band or confidence."
            )
        return cls(
            schema=RUMBLE_PROPOSAL_SCHEMA,
            status=cast(Literal["proposed", "abstained"], status),
            confidence=confidence,
            observed_lower_hz=lower,
            observed_upper_hz=upper,
            reasons=reasons,
            requires_review=True,
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class ContinuousNoiseProposalDocument:
    schema: str
    proposal_body_sha256: str
    algorithm: dict[str, str]
    policy: dict[str, Any]
    config: ContinuousNoiseConfig
    sample_rate: int
    sample_count: int
    channel_count: int
    normalized_pcm_sha256: str
    scope: NoiseAnalysisScope
    noise_references: tuple[NoiseReferenceRegion, ...]
    hum: HumProposal
    rumble: RumbleProposal

    def body_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "algorithm": dict(self.algorithm),
            "policy": dict(self.policy),
            "config": self.config.to_dict(),
            "sample_rate": self.sample_rate,
            "sample_count": self.sample_count,
            "channel_count": self.channel_count,
            "normalized_pcm_sha256": self.normalized_pcm_sha256,
            "scope": self.scope.to_dict(),
            "noise_references": [item.to_dict() for item in self.noise_references],
            "hum": self.hum.to_dict(),
            "rumble": self.rumble.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.body_dict()
        value["proposal_body_sha256"] = self.proposal_body_sha256
        return value

    @classmethod
    def from_dict(cls, value: Any) -> ContinuousNoiseProposalDocument:
        data = _object(value, "Continuous-noise proposal document")
        _strict_keys(data, set(cls.__dataclass_fields__), "Continuous-noise proposal document")
        if data["schema"] != CONTINUOUS_NOISE_DOCUMENT_SCHEMA:
            raise ProjectValidationError("Continuous-noise proposal schema is unsupported.")
        proposal_body_sha256 = _digest(
            data["proposal_body_sha256"],
            "Continuous-noise proposal body SHA-256",
        )
        unhashed = dict(data)
        del unhashed["proposal_body_sha256"]
        if canonical_json_sha256(unhashed) != proposal_body_sha256:
            raise ProjectValidationError(
                "Continuous-noise proposal body does not match its root identity."
            )
        sample_count = _integer(data["sample_count"], "Proposal sample count", 1, 2**63 - 1)
        channel_count = _integer(data["channel_count"], "Proposal channel count", 1, _MAX_CHANNELS)
        sample_rate = _integer(data["sample_rate"], "Proposal sample rate", 8_000, 768_000)
        scope = NoiseAnalysisScope.from_dict(data["scope"], sample_count)
        references = tuple(
            NoiseReferenceRegion.from_dict(item, scope)
            for item in _array(data["noise_references"], "Noise references")
        )
        _validate_geometry(scope, references)
        config = ContinuousNoiseConfig.from_dict(data["config"])
        algorithm = _object(data["algorithm"], "Continuous-noise algorithm identity")
        _strict_keys(
            algorithm,
            {"id", "module", "module_sha256", "config_sha256", "numpy_version", "float_contract"},
            "Continuous-noise algorithm identity",
        )
        if algorithm["id"] != CONTINUOUS_NOISE_ALGORITHM_ID:
            raise ProjectValidationError("Continuous-noise algorithm ID is unsupported.")
        if algorithm["module"] != CONTINUOUS_NOISE_MODULE_ID:
            raise ProjectValidationError("Continuous-noise module ID is unsupported.")
        if algorithm["float_contract"] != FLOAT_CONTRACT:
            raise ProjectValidationError("Continuous-noise float contract is unsupported.")
        _digest(algorithm["module_sha256"], "Continuous-noise module SHA-256")
        _digest(algorithm["config_sha256"], "Continuous-noise config SHA-256")
        if algorithm["config_sha256"] != canonical_json_sha256(config.to_dict()):
            raise ProjectValidationError(
                "Continuous-noise config identity does not match its body."
            )
        _text(algorithm["numpy_version"], "NumPy version", maximum=64)
        policy = _object(data["policy"], "Continuous-noise policy")
        expected_policy = _proposal_policy()
        _strict_keys(policy, set(expected_policy), "Continuous-noise policy")
        if policy != expected_policy:
            raise ProjectValidationError("Continuous-noise proposal protections are mandatory.")
        hum = HumProposal.from_dict(data["hum"], channel_count)
        rumble = RumbleProposal.from_dict(data["rumble"], channel_count)
        _validate_evidence_geometry(
            scope=scope,
            references=references,
            sample_rate=sample_rate,
            channel_count=channel_count,
            config=config,
            hum=hum,
            rumble=rumble,
        )
        _validate_proposed_evidence(config=config, hum=hum, rumble=rumble)
        return cls(
            schema=CONTINUOUS_NOISE_DOCUMENT_SCHEMA,
            proposal_body_sha256=proposal_body_sha256,
            algorithm={key: cast(str, algorithm[key]) for key in sorted(algorithm)},
            policy=expected_policy,
            config=config,
            sample_rate=sample_rate,
            sample_count=sample_count,
            channel_count=channel_count,
            normalized_pcm_sha256=_digest(
                data["normalized_pcm_sha256"],
                "Normalized PCM SHA-256",
            ),
            scope=scope,
            noise_references=references,
            hum=hum,
            rumble=rumble,
        )


@dataclass(frozen=True, slots=True)
class _HumWindow:
    rms_dbfs: float
    scores: dict[int, float]
    peak_hz: dict[int, float]
    clear_harmonics: dict[int, tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class _RumbleWindow:
    rms_dbfs: float
    low_to_bass_db: float
    low_fraction: float
    low_flatness: float
    peak_concentration: float
    qualifies: bool


def _validate_evidence_geometry(
    *,
    scope: NoiseAnalysisScope,
    references: Sequence[NoiseReferenceRegion],
    sample_rate: int,
    channel_count: int,
    config: ContinuousNoiseConfig,
    hum: HumProposal,
    rumble: RumbleProposal,
) -> None:
    """Bind evidence exactly to every declared region/channel/window count."""

    window_samples = round(sample_rate * config.window_ms / 1_000)
    expected: list[tuple[str, str, int, int]] = []
    for reference in sorted(references, key=lambda item: item.start_sample):
        count = len(
            _window_starts(
                ((reference.start_sample, reference.end_sample_exclusive),),
                window_samples,
            )
        )
        expected.extend(
            (reference.label, "reference", channel, count) for channel in range(channel_count)
        )
    program_count = len(_window_starts(_program_ranges(scope, references), window_samples))
    expected.extend(
        ("program", "program", channel, program_count) for channel in range(channel_count)
    )
    hum_geometry = [
        (item.region_label, item.region_role, item.channel_index, item.window_count)
        for item in hum.evidence
    ]
    rumble_geometry = [
        (item.region_label, item.region_role, item.channel_index, item.window_count)
        for item in rumble.evidence
    ]
    if hum_geometry != expected:
        raise ProjectValidationError(
            "Hum evidence must cover each declared reference/channel and the "
            "program/channel aggregate exactly once with exact window counts."
        )
    if rumble_geometry != expected:
        raise ProjectValidationError(
            "Rumble evidence must cover each declared reference/channel and the "
            "program/channel aggregate exactly once with exact window counts."
        )


def _validate_proposed_evidence(
    *,
    config: ContinuousNoiseConfig,
    hum: HumProposal,
    rumble: RumbleProposal,
) -> None:
    """Reject targets or bands not supported by their serialized summaries."""

    if hum.status == "proposed":
        fundamental = hum.fundamental_hz
        if fundamental is None:  # Defensive: HumProposal already enforces this.
            raise ProjectValidationError("Proposed hum evidence has no fundamental.")
        hum_references = [item for item in hum.evidence if item.region_role == "reference"]
        hum_program = [item for item in hum.evidence if item.region_role == "program"]
        harmonic_sets: list[set[int]] = []
        reference_persistence: list[float] = []
        program_persistence: list[float] = []
        margins: list[float] = []
        for hum_item in hum_references:
            hum_persistence = (
                hum_item.candidate_50_persistence
                if fundamental == 50
                else hum_item.candidate_60_persistence
            )
            peak_spread = (
                hum_item.candidate_50_peak_spread_hz
                if fundamental == 50
                else hum_item.candidate_60_peak_spread_hz
            )
            harmonics = (
                hum_item.candidate_50_harmonics
                if fundamental == 50
                else hum_item.candidate_60_harmonics
            )
            winner_score = (
                hum_item.candidate_50_score_db
                if fundamental == 50
                else hum_item.candidate_60_score_db
            )
            other_score = (
                hum_item.candidate_60_score_db
                if fundamental == 50
                else hum_item.candidate_50_score_db
            )
            if (
                hum_persistence < config.hum_reference_persistence
                or peak_spread > config.hum_maximum_peak_spread_hz
                or winner_score - other_score < config.hum_identity_margin_db
                or 1 not in harmonics
                or len(harmonics) < 2
            ):
                raise ProjectValidationError(
                    "Proposed hum is not supported by every reference/channel summary."
                )
            reference_persistence.append(hum_persistence)
            margins.append(winner_score - other_score)
            harmonic_sets.append(set(harmonics))
        for hum_item in hum_program:
            hum_persistence = (
                hum_item.candidate_50_persistence
                if fundamental == 50
                else hum_item.candidate_60_persistence
            )
            if hum_persistence < config.hum_program_persistence:
                raise ProjectValidationError(
                    "Proposed hum is not supported by every program/channel summary."
                )
            program_persistence.append(hum_persistence)
        common = tuple(sorted(set.intersection(*harmonic_sets)))
        if common != hum.detected_harmonics:
            raise ProjectValidationError(
                "Proposed hum harmonics do not equal the reference/channel consensus."
            )
        expected_confidence = _quantize(
            min(
                0.95,
                0.55
                + 0.20 * min(reference_persistence)
                + 0.10 * min(program_persistence)
                + 0.10 * min(1.0, min(margins) / 12.0),
            )
        )
        if hum.confidence != expected_confidence:
            raise ProjectValidationError(
                "Hum evidence-consistency value does not match its summaries."
            )
        if hum.reasons != (
            "stationary_line_and_harmonics_agree_across_noise_references_channels_and_program",
        ):
            raise ProjectValidationError("Proposed hum reason is inconsistent.")

    if rumble.status == "proposed":
        if rumble.observed_lower_hz != _quantize(
            config.rumble_lower_hz
        ) or rumble.observed_upper_hz != _quantize(config.rumble_upper_hz):
            raise ProjectValidationError(
                "Proposed rumble band does not match the serialized configuration."
            )
        rumble_references = [item for item in rumble.evidence if item.region_role == "reference"]
        rumble_program = [item for item in rumble.evidence if item.region_role == "program"]
        for rumble_item in rumble_references:
            if (
                rumble_item.qualifying_persistence < config.rumble_reference_persistence
                or rumble_item.low_to_bass_spread_db > config.rumble_maximum_ratio_spread_db
            ):
                raise ProjectValidationError(
                    "Proposed rumble is not supported by every reference/channel summary."
                )
        for rumble_item in rumble_program:
            if rumble_item.qualifying_persistence < config.rumble_program_persistence:
                raise ProjectValidationError(
                    "Proposed rumble is not supported by every program/channel summary."
                )
        rumble_persistence = [
            item.qualifying_persistence for item in rumble_references + rumble_program
        ]
        expected_confidence = _quantize(min(0.95, 0.60 + 0.30 * min(rumble_persistence)))
        if rumble.confidence != expected_confidence:
            raise ProjectValidationError(
                "Rumble evidence-consistency value does not match its summaries."
            )
        if rumble.reasons != (
            "diffuse_stationary_low_frequency_energy_agrees_across_"
            "noise_references_channels_and_program",
        ):
            raise ProjectValidationError("Proposed rumble reason is inconsistent.")


def _proposal_policy() -> dict[str, Any]:
    return {
        "mode": "proposal_only",
        "automatic_application_forbidden": True,
        "confidence_semantics": "evidence_consistency_not_filter_safety",
        "filter_design_included": False,
        "owner_review_required": True,
        "rendering_included": False,
        "source_audio_modified": False,
    }


def _module_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _validate_geometry(
    scope: NoiseAnalysisScope,
    references: Sequence[NoiseReferenceRegion],
) -> None:
    if not 2 <= len(references) <= 64:
        raise ProjectValidationError(
            "Continuous-noise analysis requires between 2 and 64 noise references."
        )
    if any(type(item) is not NoiseReferenceRegion for item in references):
        raise ProjectValidationError("Noise references must be NoiseReferenceRegion values.")
    labels: set[str] = set()
    previous_end = scope.start_sample
    for reference in sorted(
        references, key=lambda item: (item.start_sample, item.end_sample_exclusive)
    ):
        reference.validate(scope)
        key = reference.label.casefold()
        if key == "program":
            raise ProjectValidationError("The noise-reference label 'program' is reserved.")
        if key in labels:
            raise ProjectValidationError("Noise-reference labels must be unique.")
        labels.add(key)
        if reference.start_sample < previous_end:
            raise ProjectValidationError("Noise-reference regions must not overlap.")
        previous_end = reference.end_sample_exclusive
    covered = sum(item.end_sample_exclusive - item.start_sample for item in references)
    if covered >= scope.end_sample_exclusive - scope.start_sample:
        raise ProjectValidationError(
            "Noise references must leave a non-reference program interval."
        )


def _normalize_pcm(samples: np.ndarray) -> np.ndarray:
    if type(samples) is not np.ndarray:
        raise ProjectValidationError("Continuous-noise PCM must be a NumPy array.")
    if samples.ndim not in (1, 2):
        raise ProjectValidationError("Continuous-noise PCM must have one or two dimensions.")
    if samples.dtype.kind != "f":
        raise ProjectValidationError("Continuous-noise PCM must use a floating-point dtype.")
    if samples.shape[0] < 1:
        raise ProjectValidationError("Continuous-noise PCM must contain at least one frame.")
    channel_count = 1 if samples.ndim == 1 else samples.shape[1]
    if not 1 <= channel_count <= _MAX_CHANNELS:
        raise ProjectValidationError(
            f"Continuous-noise PCM must contain between 1 and {_MAX_CHANNELS} channels."
        )
    try:
        finite = bool(np.all(np.isfinite(samples)))
    except TypeError as exc:
        raise ProjectValidationError("Continuous-noise PCM values are malformed.") from exc
    if not finite:
        raise ProjectValidationError("Continuous-noise PCM must contain only finite values.")
    if bool(np.any(np.abs(samples) > 1.0)):
        raise ProjectValidationError("Continuous-noise PCM must be normalized to [-1, 1].")
    frames = samples[:, np.newaxis] if samples.ndim == 1 else samples
    return np.ascontiguousarray(frames, dtype="<f8")


def _program_ranges(
    scope: NoiseAnalysisScope,
    references: Sequence[NoiseReferenceRegion],
) -> tuple[tuple[int, int], ...]:
    cursor = scope.start_sample
    ranges: list[tuple[int, int]] = []
    for reference in sorted(references, key=lambda item: item.start_sample):
        if cursor < reference.start_sample:
            ranges.append((cursor, reference.start_sample))
        cursor = reference.end_sample_exclusive
    if cursor < scope.end_sample_exclusive:
        ranges.append((cursor, scope.end_sample_exclusive))
    return tuple(ranges)


def _window_starts(
    ranges: Sequence[tuple[int, int]],
    window_samples: int,
) -> tuple[int, ...]:
    starts: list[int] = []
    for start, end in ranges:
        starts.extend(range(start, end - window_samples + 1, window_samples))
    return tuple(starts)


def _db_rms(window: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(window, dtype=np.float64))))
    return max(-400.0, 20.0 * math.log10(max(rms, _EPSILON)))


def _spectrum(window: np.ndarray, taper: np.ndarray) -> np.ndarray:
    centered = window - float(np.mean(window))
    transformed = np.fft.rfft(centered * taper)
    return np.square(np.abs(transformed), dtype=np.float64)


def _line_excess(
    power: np.ndarray,
    frequencies: np.ndarray,
    target_hz: float,
    config: ContinuousNoiseConfig,
) -> tuple[float, float]:
    line = np.abs(frequencies - target_hz) <= config.hum_line_half_width_hz
    neighborhood = (np.abs(frequencies - target_hz) <= config.hum_neighborhood_half_width_hz) & (
        np.abs(frequencies - target_hz) > config.hum_line_half_width_hz * 2.0
    )
    if not bool(np.any(line)) or int(np.count_nonzero(neighborhood)) < 2:
        return -200.0, target_hz
    line_indices = np.flatnonzero(line)
    local_index = int(line_indices[int(np.argmax(power[line]))])
    line_power = float(power[local_index])
    baseline = float(np.median(power[neighborhood]))
    excess = 10.0 * math.log10(max(line_power, _EPSILON) / max(baseline, _EPSILON))
    return min(400.0, max(-200.0, excess)), float(frequencies[local_index])


def _hum_window(
    window: np.ndarray,
    frequencies: np.ndarray,
    taper: np.ndarray,
    config: ContinuousNoiseConfig,
) -> _HumWindow:
    power = _spectrum(window, taper)
    candidate_scores: dict[int, float] = {}
    candidate_peaks: dict[int, float] = {}
    clear: dict[int, tuple[int, ...]] = {}
    for fundamental in (50, 60):
        excesses: list[float] = []
        peaks: list[float] = []
        for harmonic in range(1, 5):
            excess, peak = _line_excess(
                power,
                frequencies,
                fundamental * harmonic,
                config,
            )
            excesses.append(excess)
            peaks.append(peak)
        detected = tuple(
            harmonic
            for harmonic, excess in enumerate(excesses, start=1)
            if excess
            >= (config.hum_line_excess_db if harmonic == 1 else config.hum_harmonic_excess_db)
        )
        strongest_overtone = max(excesses[1:])
        candidate_scores[fundamental] = (excesses[0] + strongest_overtone) / 2.0
        candidate_peaks[fundamental] = peaks[0]
        clear[fundamental] = detected
    return _HumWindow(
        rms_dbfs=_db_rms(window),
        scores=candidate_scores,
        peak_hz=candidate_peaks,
        clear_harmonics=clear,
    )


def _hum_evidence(
    pcm: np.ndarray,
    starts: Sequence[int],
    window_samples: int,
    sample_rate: int,
    label: str,
    role: Literal["reference", "program"],
    config: ContinuousNoiseConfig,
) -> tuple[HumRegionChannelEvidence, ...]:
    taper = np.hanning(window_samples).astype(np.float64)
    frequencies = np.fft.rfftfreq(window_samples, d=1.0 / sample_rate)
    result: list[HumRegionChannelEvidence] = []
    for channel in range(pcm.shape[1]):
        windows = [
            _hum_window(
                pcm[start : start + window_samples, channel],
                frequencies,
                taper,
                config,
            )
            for start in starts
        ]
        values: dict[int, dict[str, Any]] = {}
        for fundamental in (50, 60):
            qualifying = [
                item
                for item in windows
                if 1 in item.clear_harmonics[fundamental]
                and len(item.clear_harmonics[fundamental]) >= 2
            ]
            harmonic_ids = tuple(
                harmonic
                for harmonic in range(1, 5)
                if windows
                and sum(harmonic in item.clear_harmonics[fundamental] for item in windows)
                / len(windows)
                >= config.hum_reference_persistence
            )
            values[fundamental] = {
                "score": _median([item.scores[fundamental] for item in windows]),
                "persistence": len(qualifying) / len(windows) if windows else 0.0,
                "spread": _spread([item.peak_hz[fundamental] for item in qualifying]),
                "harmonics": harmonic_ids,
            }
        result.append(
            HumRegionChannelEvidence(
                region_label=label,
                region_role=role,
                channel_index=channel,
                window_count=len(windows),
                median_rms_dbfs=_quantize(_median([item.rms_dbfs for item in windows])),
                rms_spread_db=_quantize(_spread([item.rms_dbfs for item in windows])),
                candidate_50_score_db=_quantize(float(values[50]["score"])),
                candidate_50_persistence=_quantize(float(values[50]["persistence"])),
                candidate_50_peak_spread_hz=_quantize(float(values[50]["spread"])),
                candidate_50_harmonics=cast(tuple[int, ...], values[50]["harmonics"]),
                candidate_60_score_db=_quantize(float(values[60]["score"])),
                candidate_60_persistence=_quantize(float(values[60]["persistence"])),
                candidate_60_peak_spread_hz=_quantize(float(values[60]["spread"])),
                candidate_60_harmonics=cast(tuple[int, ...], values[60]["harmonics"]),
            )
        )
    return tuple(result)


def _rumble_window(
    window: np.ndarray,
    frequencies: np.ndarray,
    taper: np.ndarray,
    config: ContinuousNoiseConfig,
) -> _RumbleWindow:
    power = _spectrum(window, taper)
    low_mask = (frequencies >= config.rumble_lower_hz) & (frequencies <= config.rumble_upper_hz)
    comparison_mask = (frequencies >= config.rumble_comparison_lower_hz) & (
        frequencies <= config.rumble_comparison_upper_hz
    )
    combined_mask = low_mask | comparison_mask
    low = power[low_mask]
    comparison = power[comparison_mask]
    low_density = float(np.mean(low)) if low.size else 0.0
    comparison_density = float(np.mean(comparison)) if comparison.size else 0.0
    ratio = 10.0 * math.log10(max(low_density, _EPSILON) / max(comparison_density, _EPSILON))
    ratio = min(400.0, max(-400.0, ratio))
    low_sum = float(np.sum(low))
    combined_sum = float(np.sum(power[combined_mask]))
    fraction = low_sum / max(combined_sum, _EPSILON)
    positive_low = np.maximum(low, _EPSILON)
    flatness = (
        float(np.exp(np.mean(np.log(positive_low))) / np.mean(positive_low))
        if positive_low.size
        else 0.0
    )
    concentration = float(np.max(low) / max(low_sum, _EPSILON)) if low.size else 1.0
    qualifies = (
        ratio >= config.rumble_low_to_bass_db
        and fraction >= config.rumble_minimum_low_fraction
        and flatness >= config.rumble_minimum_flatness
        and concentration <= config.rumble_maximum_peak_concentration
    )
    return _RumbleWindow(
        rms_dbfs=_db_rms(window),
        low_to_bass_db=ratio,
        low_fraction=fraction,
        low_flatness=flatness,
        peak_concentration=concentration,
        qualifies=qualifies,
    )


def _rumble_evidence(
    pcm: np.ndarray,
    starts: Sequence[int],
    window_samples: int,
    sample_rate: int,
    label: str,
    role: Literal["reference", "program"],
    config: ContinuousNoiseConfig,
) -> tuple[RumbleRegionChannelEvidence, ...]:
    taper = np.hanning(window_samples).astype(np.float64)
    frequencies = np.fft.rfftfreq(window_samples, d=1.0 / sample_rate)
    result: list[RumbleRegionChannelEvidence] = []
    for channel in range(pcm.shape[1]):
        windows = [
            _rumble_window(
                pcm[start : start + window_samples, channel],
                frequencies,
                taper,
                config,
            )
            for start in starts
        ]
        result.append(
            RumbleRegionChannelEvidence(
                region_label=label,
                region_role=role,
                channel_index=channel,
                window_count=len(windows),
                median_rms_dbfs=_quantize(_median([item.rms_dbfs for item in windows])),
                rms_spread_db=_quantize(_spread([item.rms_dbfs for item in windows])),
                median_low_to_bass_db=_quantize(_median([item.low_to_bass_db for item in windows])),
                low_to_bass_spread_db=_quantize(_spread([item.low_to_bass_db for item in windows])),
                median_low_fraction=_quantize(_median([item.low_fraction for item in windows])),
                median_low_flatness=_quantize(_median([item.low_flatness for item in windows])),
                median_peak_concentration=_quantize(
                    _median([item.peak_concentration for item in windows])
                ),
                qualifying_persistence=_quantize(
                    sum(item.qualifies for item in windows) / len(windows) if windows else 0.0
                ),
            )
        )
    return tuple(result)


def _abstained_hum(
    reason: str,
    evidence: tuple[HumRegionChannelEvidence, ...],
) -> HumProposal:
    return HumProposal(
        schema=HUM_PROPOSAL_SCHEMA,
        status="abstained",
        confidence=0.0,
        fundamental_hz=None,
        detected_harmonics=(),
        reasons=(reason,),
        requires_review=True,
        evidence=evidence,
    )


def _abstained_rumble(
    reason: str,
    evidence: tuple[RumbleRegionChannelEvidence, ...],
) -> RumbleProposal:
    return RumbleProposal(
        schema=RUMBLE_PROPOSAL_SCHEMA,
        status="abstained",
        confidence=0.0,
        observed_lower_hz=None,
        observed_upper_hz=None,
        reasons=(reason,),
        requires_review=True,
        evidence=evidence,
    )


def _decide_hum(
    evidence: tuple[HumRegionChannelEvidence, ...],
    config: ContinuousNoiseConfig,
    *,
    global_reason: str | None,
) -> HumProposal:
    if global_reason is not None:
        return _abstained_hum(global_reason, evidence)
    references = [item for item in evidence if item.region_role == "reference"]
    program = [item for item in evidence if item.region_role == "program"]
    if any(item.window_count < config.minimum_reference_windows for item in references):
        return _abstained_hum("insufficient_reference_windows", evidence)
    if any(item.window_count < config.minimum_program_windows for item in program):
        return _abstained_hum("insufficient_program_windows", evidence)
    if any(item.median_rms_dbfs > config.maximum_reference_rms_dbfs for item in references):
        return _abstained_hum("reference_level_suggests_program_audio", evidence)
    if any(item.rms_spread_db > config.maximum_reference_rms_spread_db for item in references):
        return _abstained_hum("reference_signal_is_temporally_unstable", evidence)

    choices: list[int] = []
    for item in references:
        qualify_50 = (
            item.candidate_50_persistence >= config.hum_reference_persistence
            and item.candidate_50_peak_spread_hz <= config.hum_maximum_peak_spread_hz
            and 1 in item.candidate_50_harmonics
            and len(item.candidate_50_harmonics) >= 2
        )
        qualify_60 = (
            item.candidate_60_persistence >= config.hum_reference_persistence
            and item.candidate_60_peak_spread_hz <= config.hum_maximum_peak_spread_hz
            and 1 in item.candidate_60_harmonics
            and len(item.candidate_60_harmonics) >= 2
        )
        if (
            qualify_50
            and item.candidate_50_score_db - item.candidate_60_score_db
            >= config.hum_identity_margin_db
        ):
            choices.append(50)
        elif (
            qualify_60
            and item.candidate_60_score_db - item.candidate_50_score_db
            >= config.hum_identity_margin_db
        ):
            choices.append(60)
        elif qualify_50 or qualify_60:
            return _abstained_hum("50_60_identity_ambiguous", evidence)
        else:
            isolated = 1 in item.candidate_50_harmonics or 1 in item.candidate_60_harmonics
            reason = (
                "isolated_tone_or_musical_bass_ambiguous"
                if isolated
                else "line_not_persistent_across_references"
            )
            return _abstained_hum(reason, evidence)
    if not choices or len(set(choices)) != 1:
        return _abstained_hum("channels_or_references_disagree", evidence)
    fundamental = choices[0]
    program_persistence = [
        (item.candidate_50_persistence if fundamental == 50 else item.candidate_60_persistence)
        for item in program
    ]
    if any(value < config.hum_program_persistence for value in program_persistence):
        return _abstained_hum("line_not_confirmed_in_program_windows", evidence)
    harmonic_sets = [
        set(item.candidate_50_harmonics if fundamental == 50 else item.candidate_60_harmonics)
        for item in references
    ]
    common = tuple(sorted(set.intersection(*harmonic_sets)))
    if 1 not in common or len(common) < 2:
        return _abstained_hum("harmonics_do_not_agree_across_channels", evidence)
    reference_persistence = [
        item.candidate_50_persistence if fundamental == 50 else item.candidate_60_persistence
        for item in references
    ]
    margins = [abs(item.candidate_50_score_db - item.candidate_60_score_db) for item in references]
    confidence = min(
        0.95,
        0.55
        + 0.20 * min(reference_persistence)
        + 0.10 * min(program_persistence)
        + 0.10 * min(1.0, min(margins) / 12.0),
    )
    return HumProposal(
        schema=HUM_PROPOSAL_SCHEMA,
        status="proposed",
        confidence=_quantize(confidence),
        fundamental_hz=fundamental,
        detected_harmonics=common,
        reasons=(
            "stationary_line_and_harmonics_agree_across_noise_references_channels_and_program",
        ),
        requires_review=True,
        evidence=evidence,
    )


def _decide_rumble(
    evidence: tuple[RumbleRegionChannelEvidence, ...],
    config: ContinuousNoiseConfig,
    *,
    global_reason: str | None,
) -> RumbleProposal:
    if global_reason is not None:
        return _abstained_rumble(global_reason, evidence)
    references = [item for item in evidence if item.region_role == "reference"]
    program = [item for item in evidence if item.region_role == "program"]
    if any(item.window_count < config.minimum_reference_windows for item in references):
        return _abstained_rumble("insufficient_reference_windows", evidence)
    if any(item.window_count < config.minimum_program_windows for item in program):
        return _abstained_rumble("insufficient_program_windows", evidence)
    if any(item.median_rms_dbfs > config.maximum_reference_rms_dbfs for item in references):
        return _abstained_rumble("reference_level_suggests_program_audio", evidence)
    if any(item.rms_spread_db > config.maximum_reference_rms_spread_db for item in references):
        return _abstained_rumble("reference_signal_is_temporally_unstable", evidence)
    reference_flags = [
        item.qualifying_persistence >= config.rumble_reference_persistence
        and item.low_to_bass_spread_db <= config.rumble_maximum_ratio_spread_db
        for item in references
    ]
    if any(reference_flags) and not all(reference_flags):
        return _abstained_rumble("channels_or_references_disagree", evidence)
    if not all(reference_flags):
        musical_ambiguity = any(
            item.median_low_to_bass_db >= config.rumble_low_to_bass_db
            and (
                item.median_low_flatness < config.rumble_minimum_flatness
                or item.median_peak_concentration > config.rumble_maximum_peak_concentration
            )
            for item in references
        )
        reason = (
            "plausible_low_frequency_music_or_tone"
            if musical_ambiguity
            else "diffuse_rumble_not_persistent_across_references"
        )
        return _abstained_rumble(reason, evidence)
    if any(item.qualifying_persistence < config.rumble_program_persistence for item in program):
        return _abstained_rumble("rumble_not_confirmed_in_program_windows", evidence)
    persistence = [item.qualifying_persistence for item in references + program]
    confidence = min(0.95, 0.60 + 0.30 * min(persistence))
    return RumbleProposal(
        schema=RUMBLE_PROPOSAL_SCHEMA,
        status="proposed",
        confidence=_quantize(confidence),
        observed_lower_hz=_quantize(config.rumble_lower_hz),
        observed_upper_hz=_quantize(config.rumble_upper_hz),
        reasons=(
            "diffuse_stationary_low_frequency_energy_agrees_across_"
            "noise_references_channels_and_program",
        ),
        requires_review=True,
        evidence=evidence,
    )


def analyze_continuous_noise(
    samples: np.ndarray,
    *,
    sample_rate: int,
    scope: NoiseAnalysisScope,
    noise_references: Sequence[NoiseReferenceRegion],
    config: ContinuousNoiseConfig | None = None,
) -> ContinuousNoiseProposalDocument:
    """Return deterministic hum and rumble evidence without changing ``samples``.

    ``samples`` must be normalized floating-point PCM with shape ``(frames,)``
    or ``(frames, channels)``.  Noise-reference intervals are assertions made
    by the caller; the analyzer records their exact geometry but cannot prove
    that they contain no music.
    """

    pcm = _normalize_pcm(samples)
    rate = _integer(sample_rate, "Continuous-noise sample rate", 8_000, 768_000)
    if type(scope) is not NoiseAnalysisScope:
        raise ProjectValidationError("Analysis scope must be a NoiseAnalysisScope value.")
    scope.validate(pcm.shape[0])
    try:
        references = tuple(noise_references)
    except TypeError as exc:
        raise ProjectValidationError(
            "Noise references must be a finite sequence of regions."
        ) from exc
    if any(type(item) is not NoiseReferenceRegion for item in references):
        raise ProjectValidationError("Noise references must be NoiseReferenceRegion values.")
    _validate_geometry(scope, references)
    settings = config or ContinuousNoiseConfig()
    settings.validate()
    window_samples = round(rate * settings.window_ms / 1_000)
    if window_samples < 2:
        raise ProjectValidationError("Continuous-noise window geometry is invalid.")

    reference_starts: list[tuple[NoiseReferenceRegion, tuple[int, ...]]] = []
    for reference in sorted(references, key=lambda item: item.start_sample):
        starts = _window_starts(
            ((reference.start_sample, reference.end_sample_exclusive),),
            window_samples,
        )
        reference_starts.append((reference, starts))
    program_starts = _window_starts(
        _program_ranges(scope, references),
        window_samples,
    )

    hum_items: list[HumRegionChannelEvidence] = []
    rumble_items: list[RumbleRegionChannelEvidence] = []
    for reference, starts in reference_starts:
        hum_items.extend(
            _hum_evidence(
                pcm,
                starts,
                window_samples,
                rate,
                reference.label,
                "reference",
                settings,
            )
        )
        rumble_items.extend(
            _rumble_evidence(
                pcm,
                starts,
                window_samples,
                rate,
                reference.label,
                "reference",
                settings,
            )
        )
    hum_items.extend(
        _hum_evidence(
            pcm,
            program_starts,
            window_samples,
            rate,
            "program",
            "program",
            settings,
        )
    )
    rumble_items.extend(
        _rumble_evidence(
            pcm,
            program_starts,
            window_samples,
            rate,
            "program",
            "program",
            settings,
        )
    )

    scoped = pcm[scope.start_sample : scope.end_sample_exclusive]
    global_reason: str | None = None
    if bool(np.any(np.abs(scoped) >= settings.clipping_amplitude)):
        global_reason = "clipping_invalidates_continuous_noise_evidence"
    elif _db_rms(scoped.reshape(-1)) <= settings.silence_rms_dbfs:
        global_reason = "silence_or_signal_below_analysis_floor"

    hum_evidence = tuple(hum_items)
    rumble_evidence = tuple(rumble_items)
    config_dict = settings.to_dict()
    algorithm = {
        "config_sha256": canonical_json_sha256(config_dict),
        "float_contract": FLOAT_CONTRACT,
        "id": CONTINUOUS_NOISE_ALGORITHM_ID,
        "module": CONTINUOUS_NOISE_MODULE_ID,
        "module_sha256": _module_sha256(),
        "numpy_version": np.__version__,
    }
    digest = hashlib.sha256()
    digest.update(pcm.tobytes(order="C"))
    document = ContinuousNoiseProposalDocument(
        schema=CONTINUOUS_NOISE_DOCUMENT_SCHEMA,
        proposal_body_sha256="",
        algorithm=algorithm,
        policy=_proposal_policy(),
        config=settings,
        sample_rate=rate,
        sample_count=pcm.shape[0],
        channel_count=pcm.shape[1],
        normalized_pcm_sha256=digest.hexdigest(),
        scope=scope,
        noise_references=tuple(
            sorted(references, key=lambda item: (item.start_sample, item.end_sample_exclusive))
        ),
        hum=_decide_hum(hum_evidence, settings, global_reason=global_reason),
        rumble=_decide_rumble(rumble_evidence, settings, global_reason=global_reason),
    )
    document = replace(
        document,
        proposal_body_sha256=canonical_json_sha256(document.body_dict()),
    )
    # Round-trip through the strict schema before exposing a proposal.
    return ContinuousNoiseProposalDocument.from_dict(document.to_dict())


def validate_continuous_noise_proposal_document(value: Any) -> dict[str, Any]:
    """Validate and return one canonical JSON-compatible proposal document."""

    return ContinuousNoiseProposalDocument.from_dict(value).to_dict()
