"""Evidence-first stationary broadband hiss proposal and audition preview.

This module is deliberately isolated from the hum/rumble proposal schema.  It
operates only on caller-supplied floating-point PCM arrays and explicitly
reviewed noise-reference regions.  Detection requires persistent broadband
high-frequency energy, cross-window and cross-channel spectral agreement, and
guards against tones, bright music, and transients.  A positive result is only
a proposal: these tests cannot prove that broadband energy is unwanted noise.

Rendering additionally requires an exact caller attestation.  The conservative
spectral estimate is derived only from the proposal-bound reference regions.
The three returned arrays are immutable Original, Proposed, and Removed views;
declared audition gains are separate from the raw arrays.  Receipts bind the
PCM, proposal, configuration, module files, runtime, reference estimate, and
array hashes.  No API in this module edits a project, writes owner media,
approves restoration, or claims zero sonic impact.

The thresholds and synthetic tests establish deterministic safety properties,
not accuracy on real records.  Hiss that is spectrally indistinguishable from
recorded air, tape noise, effects, or sustained cymbal wash remains ambiguous
and must be decided by the owner's ears.
"""

from __future__ import annotations

import hashlib
import math
import platform
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

import numpy as np

from .continuous_noise import FLOAT_CONTRACT, NoiseAnalysisScope, NoiseReferenceRegion
from .errors import ProjectValidationError
from .publication import canonical_json_sha256
from .validation import strict_finite_number

HISS_PROPOSAL_SCHEMA = "groove-serpent.hiss-proposal/1"
HISS_REVIEW_ATTESTATION_SCHEMA = "groove-serpent.hiss-preview-review-attestation/1"
HISS_PREVIEW_RECIPE_SCHEMA = "groove-serpent.hiss-preview-recipe/1"
HISS_PREVIEW_RENDER_SCHEMA = "groove-serpent.hiss-preview-render/1"
HISS_PREVIEW_RECEIPT_SCHEMA = "groove-serpent.hiss-preview-receipt/1"
HISS_ANALYSIS_ALGORITHM_ID = "groove-serpent.stationary-broadband-hiss-evidence/1"
HISS_PREVIEW_ALGORITHM_ID = "groove-serpent.stationary-broadband-hiss-preview/1"
HISS_MODULE_ID = "groove_serpent.hiss_preview"
REVIEW_DECISION = "request_owner_audition_preview"
REVIEW_ACKNOWLEDGEMENT = "caller_attestation_is_not_proof_of_human_audition_or_restoration_approval"

_EPSILON = np.finfo(np.float64).tiny
_MAX_CHANNELS = 32
_DIGEST_CHARACTERS = frozenset("0123456789abcdef")
_PROPOSAL_REASONS = frozenset(
    {
        "channels_or_references_disagree",
        "clipping_invalidates_hiss_evidence",
        "insufficient_reference_regions",
        "insufficient_reference_windows",
        "music_like_tonality_or_bright_content",
        "reference_level_suggests_program_audio",
        "reference_spectra_not_stationary",
        "silence_or_signal_below_analysis_floor",
        "stationary_broadband_high_frequency_noise_agrees_across_reviewed_references_and_channels",
        "transient_or_temporally_unstable_reference",
    }
)


def _object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProjectValidationError(f"{label} must be a JSON object.")
    return cast(dict[str, Any], value)


def _array(value: Any, label: str) -> list[Any]:
    if type(value) is not list:
        raise ProjectValidationError(f"{label} must be a JSON array.")
    return value


def _strict_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ProjectValidationError(
            f"{label} fields are invalid (missing={missing}, extra={extra})."
        )


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


def _text(value: Any, label: str, maximum: int = 256) -> str:
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
    result = round(float(value), 12)
    return 0.0 if result == 0.0 else result


def _module_sha256() -> str:
    if not __file__:
        raise ProjectValidationError("Hiss module has no filesystem identity.")
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError as exc:
        raise ProjectValidationError("Hiss module identity could not be read.") from exc


def _runtime_identity() -> dict[str, str]:
    return {
        "numpy_version": np.__version__,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
    }


def _pcm_sha256(pcm: np.ndarray) -> str:
    return hashlib.sha256(pcm.tobytes(order="C")).hexdigest()


def _normalize_pcm(samples: np.ndarray) -> tuple[np.ndarray, bool]:
    if type(samples) is not np.ndarray:
        raise ProjectValidationError("Hiss PCM must be a NumPy array.")
    if samples.ndim not in (1, 2):
        raise ProjectValidationError("Hiss PCM must have one or two dimensions.")
    if samples.dtype.kind != "f":
        raise ProjectValidationError("Hiss PCM must use a floating-point dtype.")
    if samples.shape[0] < 1:
        raise ProjectValidationError("Hiss PCM must contain at least one frame.")
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    if not 1 <= channels <= _MAX_CHANNELS:
        raise ProjectValidationError(
            f"Hiss PCM must contain between 1 and {_MAX_CHANNELS} channels."
        )
    if not bool(np.all(np.isfinite(samples))):
        raise ProjectValidationError("Hiss PCM must contain only finite values.")
    if bool(np.any(np.abs(samples) > 1.0)):
        raise ProjectValidationError("Hiss PCM must be normalized to [-1, 1].")
    framed = samples[:, np.newaxis] if samples.ndim == 1 else samples
    return np.ascontiguousarray(framed, dtype="<f8"), samples.ndim == 1


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(np.median(np.asarray(values, dtype=np.float64)))


def _spread(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    array = np.asarray(values, dtype=np.float64)
    return float(np.max(array) - np.min(array))


def _db_rms(values: np.ndarray) -> float:
    return max(-400.0, 20.0 * math.log10(max(float(np.sqrt(np.mean(values**2))), _EPSILON)))


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= _EPSILON:
        return 0.0
    return float(np.dot(left, right) / denominator)


def _proposal_policy() -> dict[str, Any]:
    return {
        "automatic_application_forbidden": True,
        "automatic_publication_forbidden": True,
        "mode": "evidence_only_review_required",
        "noise_only_references_are_caller_assertions": True,
        "physical_source_noise_not_proven": True,
        "quality_neutrality_claimed": False,
        "requires_owner_audition": True,
        "source_audio_modified": False,
    }


def _recipe_policy() -> dict[str, Any]:
    return {
        "attestation_is_not_human_audition_proof": True,
        "automatic_application_forbidden": True,
        "automatic_publication_forbidden": True,
        "mode": "owner_audition_preview_only",
        "quality_neutrality_claimed": False,
        "source_audio_modified": False,
    }


def _render_policy() -> dict[str, Any]:
    return {
        "audition_gains_are_separate_from_raw_arrays": True,
        "automatic_application_forbidden": True,
        "mode": "owner_audition_preview_only",
        "quality_neutrality_claimed": False,
        "source_audio_modified": False,
    }


def _receipt_policy() -> dict[str, Any]:
    return {
        "attestation_is_not_human_audition_proof": True,
        "automatic_application_forbidden": True,
        "mode": "owner_audition_preview_only",
        "quality_neutrality_claimed": False,
        "raw_arrays_are_not_publication_outputs": True,
        "zero_sonic_impact_not_claimed": True,
    }


@dataclass(frozen=True, slots=True)
class HissAnalysisConfig:
    """Bounded v1 evidence thresholds for stationary high-frequency noise."""

    window_ms: int = 1_000
    minimum_reference_regions: int = 2
    minimum_windows_per_reference: int = 2
    lower_hz: float = 6_000.0
    upper_hz: float = 18_000.0
    nyquist_margin: float = 0.90
    clipping_amplitude: float = 0.999
    minimum_high_band_rms_dbfs: float = -82.0
    maximum_reference_rms_dbfs: float = -24.0
    minimum_spectral_flatness: float = 0.35
    maximum_peak_concentration: float = 0.04
    maximum_high_band_crest_factor: float = 7.0
    maximum_subframe_rms_spread_db: float = 5.0
    minimum_qualifying_persistence: float = 0.75
    maximum_high_band_rms_spread_db: float = 6.0
    maximum_flatness_spread: float = 0.25
    minimum_profile_similarity: float = 0.92
    profile_band_count: int = 32

    def validate(self) -> None:
        _integer(self.window_ms, "Hiss analysis window", 250, 5_000)
        _integer(self.minimum_reference_regions, "Minimum hiss references", 2, 16)
        _integer(self.minimum_windows_per_reference, "Minimum hiss windows", 2, 100)
        lower = _number(self.lower_hz, "Hiss lower frequency", 2_000.0, 24_000.0)
        upper = _number(self.upper_hz, "Hiss upper frequency", 4_000.0, 96_000.0)
        if upper <= lower + 1_000.0:
            raise ProjectValidationError("Hiss analysis band must span more than 1 kHz.")
        _number(self.nyquist_margin, "Hiss Nyquist margin", 0.70, 0.98)
        _number(self.clipping_amplitude, "Hiss clipping amplitude", 0.90, 1.0)
        _number(self.minimum_high_band_rms_dbfs, "Hiss analysis floor", -140.0, -30.0)
        _number(self.maximum_reference_rms_dbfs, "Maximum reference RMS", -80.0, -6.0)
        _number(self.minimum_spectral_flatness, "Minimum hiss flatness", 0.05, 0.95)
        _number(self.maximum_peak_concentration, "Maximum hiss peak concentration", 0.001, 0.5)
        _number(self.maximum_high_band_crest_factor, "Maximum hiss crest factor", 2.0, 20.0)
        _number(
            self.maximum_subframe_rms_spread_db,
            "Maximum hiss subframe RMS spread",
            0.1,
            30.0,
        )
        _number(self.minimum_qualifying_persistence, "Minimum hiss persistence", 0.5, 1.0)
        _number(
            self.maximum_high_band_rms_spread_db,
            "Maximum hiss level spread",
            0.1,
            30.0,
        )
        _number(self.maximum_flatness_spread, "Maximum hiss flatness spread", 0.01, 0.9)
        _number(self.minimum_profile_similarity, "Minimum hiss profile similarity", 0.5, 1.0)
        _integer(self.profile_band_count, "Hiss profile band count", 8, 128)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> HissAnalysisConfig:
        data = _object(value, "Hiss analysis configuration")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hiss analysis configuration")
        result = cls(**data)
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class HissRegionChannelEvidence:
    region_label: str
    channel_index: int
    window_count: int
    median_rms_dbfs: float
    median_high_band_rms_dbfs: float
    high_band_rms_spread_db: float
    median_spectral_flatness: float
    spectral_flatness_spread: float
    median_peak_concentration: float
    median_high_band_crest_factor: float
    median_subframe_rms_spread_db: float
    median_profile_similarity: float
    qualifying_persistence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any, channel_count: int) -> HissRegionChannelEvidence:
        data = _object(value, "Hiss region/channel evidence")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hiss region/channel evidence")
        return cls(
            region_label=_text(data["region_label"], "Hiss evidence region"),
            channel_index=_integer(
                data["channel_index"], "Hiss evidence channel", 0, channel_count - 1
            ),
            window_count=_integer(data["window_count"], "Hiss evidence windows", 0, 1_000_000),
            median_rms_dbfs=_number(data["median_rms_dbfs"], "Hiss evidence RMS", -400.0, 1.0),
            median_high_band_rms_dbfs=_number(
                data["median_high_band_rms_dbfs"], "Hiss evidence band RMS", -400.0, 1.0
            ),
            high_band_rms_spread_db=_number(
                data["high_band_rms_spread_db"], "Hiss evidence level spread", 0.0, 400.0
            ),
            median_spectral_flatness=_number(
                data["median_spectral_flatness"], "Hiss evidence flatness", 0.0, 1.0
            ),
            spectral_flatness_spread=_number(
                data["spectral_flatness_spread"], "Hiss evidence flatness spread", 0.0, 1.0
            ),
            median_peak_concentration=_number(
                data["median_peak_concentration"], "Hiss evidence peak concentration", 0.0, 1.0
            ),
            median_high_band_crest_factor=_number(
                data["median_high_band_crest_factor"], "Hiss evidence crest factor", 0.0, 1_000.0
            ),
            median_subframe_rms_spread_db=_number(
                data["median_subframe_rms_spread_db"], "Hiss evidence subframe spread", 0.0, 400.0
            ),
            median_profile_similarity=_number(
                data["median_profile_similarity"], "Hiss evidence profile similarity", 0.0, 1.0
            ),
            qualifying_persistence=_number(
                data["qualifying_persistence"], "Hiss evidence persistence", 0.0, 1.0
            ),
        )


@dataclass(frozen=True, slots=True)
class HissProposal:
    schema: str
    proposal_body_sha256: str
    algorithm: dict[str, str]
    policy: dict[str, Any]
    config: HissAnalysisConfig
    sample_rate: int
    sample_count: int
    channel_count: int
    normalized_pcm_sha256: str
    scope: NoiseAnalysisScope
    noise_references: tuple[NoiseReferenceRegion, ...]
    effective_lower_hz: float
    effective_upper_hz: float
    status: Literal["proposed", "abstained"]
    confidence: float
    reasons: tuple[str, ...]
    evidence: tuple[HissRegionChannelEvidence, ...]

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
            "effective_lower_hz": self.effective_lower_hz,
            "effective_upper_hz": self.effective_upper_hz,
            "status": self.status,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "evidence": [item.to_dict() for item in self.evidence],
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.body_dict()
        value["proposal_body_sha256"] = self.proposal_body_sha256
        return value

    @classmethod
    def from_dict(cls, value: Any) -> HissProposal:
        data = _object(value, "Hiss proposal")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hiss proposal")
        if data["schema"] != HISS_PROPOSAL_SCHEMA:
            raise ProjectValidationError("Hiss proposal schema is unsupported.")
        body_sha = _digest(data["proposal_body_sha256"], "Hiss proposal body SHA-256")
        body = dict(data)
        del body["proposal_body_sha256"]
        if canonical_json_sha256(body) != body_sha:
            raise ProjectValidationError("Hiss proposal body identity is stale.")
        sample_rate = _integer(data["sample_rate"], "Hiss sample rate", 16_000, 768_000)
        sample_count = _integer(data["sample_count"], "Hiss sample count", 1, 2**63 - 1)
        channel_count = _integer(data["channel_count"], "Hiss channel count", 1, _MAX_CHANNELS)
        scope = NoiseAnalysisScope.from_dict(data["scope"], sample_count)
        references = tuple(
            NoiseReferenceRegion.from_dict(item, scope)
            for item in _array(data["noise_references"], "Hiss noise references")
        )
        _validate_reference_geometry(scope, references, require_minimum=False)
        config = HissAnalysisConfig.from_dict(data["config"])
        lower = _number(
            data["effective_lower_hz"], "Effective hiss lower frequency", 1.0, 200_000.0
        )
        upper = _number(
            data["effective_upper_hz"], "Effective hiss upper frequency", 1.0, 400_000.0
        )
        expected_upper = min(config.upper_hz, sample_rate * 0.5 * config.nyquist_margin)
        if lower != config.lower_hz or upper != expected_upper or upper <= lower + 1_000.0:
            raise ProjectValidationError("Hiss proposal effective band is inconsistent.")
        algorithm = _object(data["algorithm"], "Hiss proposal algorithm")
        algorithm_keys = {
            "config_sha256",
            "float_contract",
            "id",
            "module",
            "module_sha256",
            "numpy_version",
        }
        _strict_keys(algorithm, algorithm_keys, "Hiss proposal algorithm")
        if algorithm["id"] != HISS_ANALYSIS_ALGORITHM_ID or algorithm["module"] != HISS_MODULE_ID:
            raise ProjectValidationError("Hiss proposal algorithm identity is unsupported.")
        if algorithm["float_contract"] != FLOAT_CONTRACT:
            raise ProjectValidationError("Hiss proposal float contract is unsupported.")
        _digest(algorithm["module_sha256"], "Hiss proposal module SHA-256")
        _digest(algorithm["config_sha256"], "Hiss proposal config SHA-256")
        _text(algorithm["numpy_version"], "Hiss proposal NumPy version", 64)
        if algorithm["config_sha256"] != canonical_json_sha256(config.to_dict()):
            raise ProjectValidationError("Hiss proposal config identity is inconsistent.")
        policy = _object(data["policy"], "Hiss proposal policy")
        expected_policy = _proposal_policy()
        _strict_keys(policy, set(expected_policy), "Hiss proposal policy")
        if policy != expected_policy:
            raise ProjectValidationError("Hiss proposal protections are mandatory.")
        status = data["status"]
        if status not in {"proposed", "abstained"}:
            raise ProjectValidationError("Hiss proposal status is unsupported.")
        reasons_raw = _array(data["reasons"], "Hiss proposal reasons")
        reasons = tuple(_text(item, "Hiss proposal reason") for item in reasons_raw)
        if not reasons or reasons != tuple(dict.fromkeys(reasons)):
            raise ProjectValidationError("Hiss proposal reasons must be nonempty and unique.")
        if any(reason not in _PROPOSAL_REASONS for reason in reasons):
            raise ProjectValidationError("Hiss proposal reason is unsupported.")
        expected_positive = (
            "stationary_broadband_high_frequency_noise_agrees_across_reviewed_"
            "references_and_channels"
        )
        if (status == "proposed") != (reasons == (expected_positive,)):
            raise ProjectValidationError("Hiss proposal status and reasons disagree.")
        confidence = _number(data["confidence"], "Hiss proposal confidence", 0.0, 1.0)
        if status == "abstained" and confidence != 0.0:
            raise ProjectValidationError("Abstained hiss proposals must have zero confidence.")
        if status == "proposed" and confidence <= 0.0:
            raise ProjectValidationError("Proposed hiss evidence must have positive confidence.")
        evidence = tuple(
            HissRegionChannelEvidence.from_dict(item, channel_count)
            for item in _array(data["evidence"], "Hiss proposal evidence")
        )
        _validate_evidence_geometry(references, channel_count, evidence)
        return cls(
            schema=HISS_PROPOSAL_SCHEMA,
            proposal_body_sha256=body_sha,
            algorithm={key: cast(str, algorithm[key]) for key in sorted(algorithm)},
            policy=expected_policy,
            config=config,
            sample_rate=sample_rate,
            sample_count=sample_count,
            channel_count=channel_count,
            normalized_pcm_sha256=_digest(data["normalized_pcm_sha256"], "Hiss PCM SHA-256"),
            scope=scope,
            noise_references=references,
            effective_lower_hz=lower,
            effective_upper_hz=upper,
            status=cast(Literal["proposed", "abstained"], status),
            confidence=confidence,
            reasons=reasons,
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class _WindowEvidence:
    rms_dbfs: float
    high_rms_dbfs: float
    flatness: float
    peak_concentration: float
    crest_factor: float
    subframe_spread_db: float
    profile: np.ndarray
    qualifies: bool


def _validate_reference_geometry(
    scope: NoiseAnalysisScope,
    references: Sequence[NoiseReferenceRegion],
    *,
    require_minimum: bool,
) -> None:
    minimum = 2 if require_minimum else 1
    if not minimum <= len(references) <= 64:
        raise ProjectValidationError(f"Hiss analysis requires between {minimum} and 64 references.")
    previous_end = scope.start_sample
    labels: set[str] = set()
    for item in references:
        item.validate(scope)
        folded = item.label.casefold()
        if folded in labels or folded == "program":
            raise ProjectValidationError("Hiss reference labels must be unique and non-reserved.")
        labels.add(folded)
        if item.start_sample < previous_end:
            raise ProjectValidationError("Hiss references must be ordered and non-overlapping.")
        previous_end = item.end_sample_exclusive
    total = sum(item.end_sample_exclusive - item.start_sample for item in references)
    if total >= scope.end_sample_exclusive - scope.start_sample:
        raise ProjectValidationError("Hiss references must leave at least one program interval.")


def _validate_evidence_geometry(
    references: Sequence[NoiseReferenceRegion],
    channels: int,
    evidence: Sequence[HissRegionChannelEvidence],
) -> None:
    expected = [(item.label, channel) for item in references for channel in range(channels)]
    actual = [(item.region_label, item.channel_index) for item in evidence]
    if actual != expected:
        raise ProjectValidationError("Hiss proposal evidence geometry is incomplete or unordered.")


def _window_starts(start: int, end: int, length: int) -> tuple[int, ...]:
    if end - start < length:
        return ()
    return tuple(range(start, end - length + 1, length))


def _band_rms_from_spectrum(spectrum: np.ndarray, mask: np.ndarray, length: int) -> float:
    selected = np.where(mask, spectrum, 0.0)
    signal = np.fft.irfft(selected, n=length)
    return float(np.sqrt(np.mean(signal**2)))


def _profile(power: np.ndarray, band_count: int) -> np.ndarray:
    chunks = np.array_split(power, band_count)
    result = np.asarray([float(np.mean(chunk)) for chunk in chunks], dtype=np.float64)
    total = float(np.sum(result))
    return result / total if total > _EPSILON else np.zeros_like(result)


def _window_evidence(
    values: np.ndarray,
    sample_rate: int,
    config: HissAnalysisConfig,
    effective_upper: float,
) -> _WindowEvidence:
    taper = np.hanning(values.size)
    spectrum = np.fft.rfft(values * taper)
    untapered_spectrum = np.fft.rfft(values)
    frequencies = np.fft.rfftfreq(values.size, d=1.0 / sample_rate)
    mask = (frequencies >= config.lower_hz) & (frequencies <= effective_upper)
    band_spectrum = spectrum[mask]
    power = np.abs(band_spectrum) ** 2
    mean_power = float(np.mean(power)) if power.size else 0.0
    flatness = (
        float(np.exp(np.mean(np.log(np.maximum(power, _EPSILON)))) / mean_power)
        if mean_power > _EPSILON
        else 0.0
    )
    total_power = float(np.sum(power))
    concentration = float(np.max(power) / total_power) if total_power > _EPSILON else 1.0
    # Transient guards use an untapered reconstruction.  Measuring temporal
    # spread on the Hann-tapered signal would mistake the analysis window's
    # intentional fade-in/out for a physical transient.
    filtered = np.fft.irfft(
        np.where(mask, untapered_spectrum, 0.0),
        n=values.size,
    )
    filtered_rms = max(float(np.sqrt(np.mean(filtered**2))), _EPSILON)
    crest = float(np.max(np.abs(filtered)) / filtered_rms)
    subframes = np.array_split(filtered, 8)
    subframe_levels = [_db_rms(item) for item in subframes if item.size]
    subframe_spread = _spread(subframe_levels)
    high_rms = _band_rms_from_spectrum(untapered_spectrum, mask, values.size)
    rms_dbfs = _db_rms(values)
    high_dbfs = max(-400.0, 20.0 * math.log10(max(high_rms, _EPSILON)))
    qualifies = (
        high_dbfs >= config.minimum_high_band_rms_dbfs
        and rms_dbfs <= config.maximum_reference_rms_dbfs
        and flatness >= config.minimum_spectral_flatness
        and concentration <= config.maximum_peak_concentration
        and crest <= config.maximum_high_band_crest_factor
        and subframe_spread <= config.maximum_subframe_rms_spread_db
    )
    return _WindowEvidence(
        rms_dbfs=rms_dbfs,
        high_rms_dbfs=high_dbfs,
        flatness=flatness,
        peak_concentration=concentration,
        crest_factor=crest,
        subframe_spread_db=subframe_spread,
        profile=_profile(power, config.profile_band_count),
        qualifies=qualifies,
    )


def _region_evidence(
    pcm: np.ndarray,
    sample_rate: int,
    reference: NoiseReferenceRegion,
    channel: int,
    config: HissAnalysisConfig,
    effective_upper: float,
) -> tuple[HissRegionChannelEvidence, np.ndarray]:
    length = round(sample_rate * config.window_ms / 1_000)
    windows = [
        _window_evidence(pcm[start : start + length, channel], sample_rate, config, effective_upper)
        for start in _window_starts(reference.start_sample, reference.end_sample_exclusive, length)
    ]
    if not windows:
        empty_profile = np.zeros(config.profile_band_count, dtype=np.float64)
        return (
            HissRegionChannelEvidence(
                reference.label,
                channel,
                0,
                -400.0,
                -400.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
            ),
            empty_profile,
        )
    profiles = np.stack([item.profile for item in windows])
    median_profile = np.median(profiles, axis=0)
    profile_total = float(np.sum(median_profile))
    if profile_total > _EPSILON:
        median_profile /= profile_total
    similarities = [_cosine(item.profile, median_profile) for item in windows]
    evidence = HissRegionChannelEvidence(
        region_label=reference.label,
        channel_index=channel,
        window_count=len(windows),
        median_rms_dbfs=_quantize(_median([item.rms_dbfs for item in windows])),
        median_high_band_rms_dbfs=_quantize(_median([item.high_rms_dbfs for item in windows])),
        high_band_rms_spread_db=_quantize(_spread([item.high_rms_dbfs for item in windows])),
        median_spectral_flatness=_quantize(_median([item.flatness for item in windows])),
        spectral_flatness_spread=_quantize(_spread([item.flatness for item in windows])),
        median_peak_concentration=_quantize(_median([item.peak_concentration for item in windows])),
        median_high_band_crest_factor=_quantize(_median([item.crest_factor for item in windows])),
        median_subframe_rms_spread_db=_quantize(
            _median([item.subframe_spread_db for item in windows])
        ),
        median_profile_similarity=_quantize(_median(similarities)),
        qualifying_persistence=_quantize(sum(item.qualifies for item in windows) / len(windows)),
    )
    return evidence, median_profile


def _decide(
    pcm: np.ndarray,
    evidence: Sequence[HissRegionChannelEvidence],
    profiles: Sequence[np.ndarray],
    config: HissAnalysisConfig,
    reference_count: int,
) -> tuple[Literal["proposed", "abstained"], float, tuple[str, ...]]:
    if bool(np.any(np.abs(pcm) >= config.clipping_amplitude)):
        return "abstained", 0.0, ("clipping_invalidates_hiss_evidence",)
    if reference_count < config.minimum_reference_regions:
        return "abstained", 0.0, ("insufficient_reference_regions",)
    if any(item.window_count < config.minimum_windows_per_reference for item in evidence):
        return "abstained", 0.0, ("insufficient_reference_windows",)
    if any(item.median_high_band_rms_dbfs < config.minimum_high_band_rms_dbfs for item in evidence):
        return "abstained", 0.0, ("silence_or_signal_below_analysis_floor",)
    if any(item.median_rms_dbfs > config.maximum_reference_rms_dbfs for item in evidence):
        return "abstained", 0.0, ("reference_level_suggests_program_audio",)
    if any(
        item.median_spectral_flatness < config.minimum_spectral_flatness
        or item.median_peak_concentration > config.maximum_peak_concentration
        for item in evidence
    ):
        return "abstained", 0.0, ("music_like_tonality_or_bright_content",)
    if any(
        item.median_high_band_crest_factor > config.maximum_high_band_crest_factor
        or item.median_subframe_rms_spread_db > config.maximum_subframe_rms_spread_db
        for item in evidence
    ):
        return "abstained", 0.0, ("transient_or_temporally_unstable_reference",)
    if any(
        item.qualifying_persistence < config.minimum_qualifying_persistence
        or item.high_band_rms_spread_db > config.maximum_high_band_rms_spread_db
        or item.spectral_flatness_spread > config.maximum_flatness_spread
        or item.median_profile_similarity < config.minimum_profile_similarity
        for item in evidence
    ):
        return "abstained", 0.0, ("reference_spectra_not_stationary",)
    high_levels = [item.median_high_band_rms_dbfs for item in evidence]
    flatness_values = [item.median_spectral_flatness for item in evidence]
    profile_similarities = [
        _cosine(left, right)
        for index, left in enumerate(profiles)
        for right in profiles[index + 1 :]
    ]
    if (
        _spread(high_levels) > config.maximum_high_band_rms_spread_db
        or _spread(flatness_values) > config.maximum_flatness_spread
        or any(value < config.minimum_profile_similarity for value in profile_similarities)
    ):
        return "abstained", 0.0, ("channels_or_references_disagree",)
    margins = [
        min(item.median_spectral_flatness / config.minimum_spectral_flatness - 1.0, 1.0)
        for item in evidence
    ]
    margins.extend(
        min(item.qualifying_persistence / config.minimum_qualifying_persistence - 1.0, 1.0)
        for item in evidence
    )
    stationarity_margin = min(profile_similarities, default=1.0) - config.minimum_profile_similarity
    normalized_stationarity = stationarity_margin / max(
        1.0 - config.minimum_profile_similarity, 0.001
    )
    confidence = _quantize(
        max(0.05, min(0.95, 0.55 + 0.20 * min(margins) + 0.20 * normalized_stationarity))
    )
    reason = (
        "stationary_broadband_high_frequency_noise_agrees_across_reviewed_references_and_channels"
    )
    return "proposed", confidence, (reason,)


def analyze_hiss(
    samples: np.ndarray,
    *,
    sample_rate: int,
    scope: NoiseAnalysisScope,
    noise_references: Sequence[NoiseReferenceRegion],
    config: HissAnalysisConfig | None = None,
) -> HissProposal:
    """Return deterministic evidence or a conservative abstention."""

    pcm, _was_mono = _normalize_pcm(samples)
    _integer(sample_rate, "Hiss sample rate", 16_000, 768_000)
    scope.validate(pcm.shape[0])
    references = tuple(noise_references)
    _validate_reference_geometry(scope, references, require_minimum=False)
    settings = config or HissAnalysisConfig()
    settings.validate()
    effective_upper = min(settings.upper_hz, sample_rate * 0.5 * settings.nyquist_margin)
    if effective_upper <= settings.lower_hz + 1_000.0:
        raise ProjectValidationError("Sample rate leaves no supported broadband hiss band.")
    evidence: list[HissRegionChannelEvidence] = []
    profiles: list[np.ndarray] = []
    for reference in references:
        for channel in range(pcm.shape[1]):
            item, profile = _region_evidence(
                pcm,
                sample_rate,
                reference,
                channel,
                settings,
                effective_upper,
            )
            evidence.append(item)
            profiles.append(profile)
    status, confidence, reasons = _decide(
        pcm,
        evidence,
        profiles,
        settings,
        len(references),
    )
    config_body = settings.to_dict()
    proposal = HissProposal(
        schema=HISS_PROPOSAL_SCHEMA,
        proposal_body_sha256="",
        algorithm={
            "config_sha256": canonical_json_sha256(config_body),
            "float_contract": FLOAT_CONTRACT,
            "id": HISS_ANALYSIS_ALGORITHM_ID,
            "module": HISS_MODULE_ID,
            "module_sha256": _module_sha256(),
            "numpy_version": np.__version__,
        },
        policy=_proposal_policy(),
        config=settings,
        sample_rate=sample_rate,
        sample_count=pcm.shape[0],
        channel_count=pcm.shape[1],
        normalized_pcm_sha256=_pcm_sha256(pcm),
        scope=scope,
        noise_references=references,
        effective_lower_hz=settings.lower_hz,
        effective_upper_hz=_quantize(effective_upper),
        status=status,
        confidence=confidence,
        reasons=reasons,
        evidence=tuple(evidence),
    )
    proposal = replace(
        proposal,
        proposal_body_sha256=canonical_json_sha256(proposal.body_dict()),
    )
    return HissProposal.from_dict(proposal.to_dict())


def validate_hiss_proposal(value: Any) -> dict[str, Any]:
    """Strictly parse and return a canonical hiss proposal mapping."""

    return HissProposal.from_dict(value).to_dict()


@dataclass(frozen=True, slots=True)
class HissPreviewConfig:
    """Conservative v1 spectral-estimate, scope, and audition bounds."""

    frame_length: int = 2_048
    hop_length: int = 512
    subtraction_strength: float = 0.20
    maximum_attenuation_db: float = 1.5
    edge_fade_ms: int = 100
    maximum_removed_peak: float = 0.02
    maximum_removed_energy_ratio: float = 0.01
    minimum_reference_high_band_reduction_db: float = 0.01
    maximum_high_band_reduction_db: float = 1.5
    minimum_removed_high_band_fraction: float = 0.80
    maximum_scope_rms_change_db: float = 0.25
    maximum_channel_removed_rms_spread_db: float = 6.0
    minimum_loudness_windows: int = 2
    loudness_window_floor_dbfs: float = -55.0
    maximum_match_gain_db: float = 0.25
    maximum_match_mad_db: float = 0.10
    original_audition_gain: float = 1.0
    residue_monitor_gain: float = 4.0

    def validate(self) -> None:
        frame = _integer(self.frame_length, "Hiss preview frame length", 512, 16_384)
        hop = _integer(self.hop_length, "Hiss preview hop length", 64, frame)
        if frame & (frame - 1):
            raise ProjectValidationError("Hiss preview frame length must be a power of two.")
        if frame % hop != 0 or frame // hop not in {2, 4, 8}:
            raise ProjectValidationError("Hiss preview hop must divide the frame by 2, 4, or 8.")
        _number(self.subtraction_strength, "Hiss subtraction strength", 0.01, 0.50)
        _number(self.maximum_attenuation_db, "Maximum hiss attenuation", 0.05, 3.0)
        _integer(self.edge_fade_ms, "Hiss preview edge fade", 20, 1_000)
        _number(self.maximum_removed_peak, "Maximum hiss removed peak", 0.000001, 0.05)
        _number(
            self.maximum_removed_energy_ratio,
            "Maximum hiss removed energy ratio",
            0.000001,
            0.05,
        )
        _number(
            self.minimum_reference_high_band_reduction_db,
            "Minimum reference hiss reduction",
            0.001,
            1.0,
        )
        _number(self.maximum_high_band_reduction_db, "Maximum hiss band reduction", 0.01, 3.0)
        _number(
            self.minimum_removed_high_band_fraction,
            "Minimum removed high-band fraction",
            0.5,
            1.0,
        )
        _number(self.maximum_scope_rms_change_db, "Maximum hiss scope RMS change", 0.001, 1.0)
        _number(
            self.maximum_channel_removed_rms_spread_db,
            "Maximum hiss channel removed-RMS spread",
            0.01,
            20.0,
        )
        _integer(self.minimum_loudness_windows, "Minimum hiss loudness windows", 2, 1_000)
        _number(self.loudness_window_floor_dbfs, "Hiss loudness window floor", -120.0, -20.0)
        _number(self.maximum_match_gain_db, "Maximum hiss matching gain", 0.001, 1.0)
        _number(self.maximum_match_mad_db, "Maximum hiss matching MAD", 0.0, 0.5)
        if self.original_audition_gain != 1.0:
            raise ProjectValidationError("Original hiss audition gain must remain exactly 1.0.")
        _number(self.residue_monitor_gain, "Hiss residue monitor gain", 1.0, 32.0)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> HissPreviewConfig:
        data = _object(value, "Hiss preview configuration")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hiss preview configuration")
        result = cls(**data)
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class HissReviewAttestation:
    """Exact request for a preview, never proof of completed listening."""

    schema: str
    attestation_token: str
    decision: str
    proposal_body_sha256: str
    selected_scope: NoiseAnalysisScope
    acknowledgement: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "attestation_token": self.attestation_token,
            "decision": self.decision,
            "proposal_body_sha256": self.proposal_body_sha256,
            "selected_scope": self.selected_scope.to_dict(),
            "acknowledgement": self.acknowledgement,
        }

    @classmethod
    def from_dict(cls, value: Any, sample_count: int) -> HissReviewAttestation:
        data = _object(value, "Hiss review attestation")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hiss review attestation")
        if data["schema"] != HISS_REVIEW_ATTESTATION_SCHEMA:
            raise ProjectValidationError("Hiss review attestation schema is unsupported.")
        token = _digest(data["attestation_token"], "Hiss review attestation token")
        if len(set(token)) == 1:
            raise ProjectValidationError(
                "Hiss review attestation token is structurally non-distinct."
            )
        if data["decision"] != REVIEW_DECISION:
            raise ProjectValidationError("Hiss review attestation decision is unsupported.")
        if data["acknowledgement"] != REVIEW_ACKNOWLEDGEMENT:
            raise ProjectValidationError(
                "Hiss review attestation must acknowledge its limited authority."
            )
        return cls(
            schema=HISS_REVIEW_ATTESTATION_SCHEMA,
            attestation_token=token,
            decision=REVIEW_DECISION,
            proposal_body_sha256=_digest(
                data["proposal_body_sha256"], "Attested hiss proposal SHA-256"
            ),
            selected_scope=NoiseAnalysisScope.from_dict(data["selected_scope"], sample_count),
            acknowledgement=REVIEW_ACKNOWLEDGEMENT,
        )


@dataclass(frozen=True, slots=True)
class HissPreviewRecipe:
    schema: str
    recipe_body_sha256: str
    proposal_identity: dict[str, str]
    input_identity: dict[str, Any]
    selected_scope: NoiseAnalysisScope
    noise_references: tuple[NoiseReferenceRegion, ...]
    effective_lower_hz: float
    effective_upper_hz: float
    analysis_config: HissAnalysisConfig
    review_attestation: HissReviewAttestation
    algorithm: dict[str, str]
    config: HissPreviewConfig
    policy: dict[str, Any]

    def body_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "proposal_identity": dict(self.proposal_identity),
            "input_identity": dict(self.input_identity),
            "selected_scope": self.selected_scope.to_dict(),
            "noise_references": [item.to_dict() for item in self.noise_references],
            "effective_lower_hz": self.effective_lower_hz,
            "effective_upper_hz": self.effective_upper_hz,
            "analysis_config": self.analysis_config.to_dict(),
            "review_attestation": self.review_attestation.to_dict(),
            "algorithm": dict(self.algorithm),
            "config": self.config.to_dict(),
            "policy": dict(self.policy),
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.body_dict()
        value["recipe_body_sha256"] = self.recipe_body_sha256
        return value

    @classmethod
    def from_dict(cls, value: Any) -> HissPreviewRecipe:
        data = _object(value, "Hiss preview recipe")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hiss preview recipe")
        if data["schema"] != HISS_PREVIEW_RECIPE_SCHEMA:
            raise ProjectValidationError("Hiss preview recipe schema is unsupported.")
        body_sha = _digest(data["recipe_body_sha256"], "Hiss recipe body SHA-256")
        body = dict(data)
        del body["recipe_body_sha256"]
        if canonical_json_sha256(body) != body_sha:
            raise ProjectValidationError("Hiss preview recipe identity is stale.")
        input_identity = _object(data["input_identity"], "Hiss recipe input identity")
        _strict_keys(
            input_identity,
            {"channel_count", "sample_count", "sample_rate"},
            "Hiss recipe input identity",
        )
        sample_rate = _integer(
            input_identity["sample_rate"], "Hiss recipe sample rate", 16_000, 768_000
        )
        sample_count = _integer(
            input_identity["sample_count"], "Hiss recipe sample count", 1, 2**63 - 1
        )
        channel_count = _integer(
            input_identity["channel_count"], "Hiss recipe channels", 1, _MAX_CHANNELS
        )
        scope = NoiseAnalysisScope.from_dict(data["selected_scope"], sample_count)
        references = tuple(
            NoiseReferenceRegion.from_dict(item, scope)
            for item in _array(data["noise_references"], "Hiss recipe noise references")
        )
        _validate_reference_geometry(scope, references, require_minimum=True)
        analysis_config = HissAnalysisConfig.from_dict(data["analysis_config"])
        lower = _number(data["effective_lower_hz"], "Hiss recipe lower frequency", 1.0, 200_000.0)
        upper = _number(data["effective_upper_hz"], "Hiss recipe upper frequency", 1.0, 400_000.0)
        if lower != analysis_config.lower_hz or upper != min(
            analysis_config.upper_hz, sample_rate * 0.5 * analysis_config.nyquist_margin
        ):
            raise ProjectValidationError("Hiss recipe frequency band is inconsistent.")
        proposal_identity = _object(data["proposal_identity"], "Hiss recipe proposal identity")
        proposal_keys = {
            "analysis_config_sha256",
            "analysis_module_sha256",
            "analysis_numpy_version",
            "normalized_pcm_sha256",
            "noise_references_sha256",
            "proposal_body_sha256",
            "scope_sha256",
        }
        _strict_keys(proposal_identity, proposal_keys, "Hiss recipe proposal identity")
        for key in proposal_keys - {"analysis_numpy_version"}:
            _digest(proposal_identity[key], f"Hiss recipe {key}")
        _text(proposal_identity["analysis_numpy_version"], "Hiss analysis NumPy version", 64)
        if proposal_identity["scope_sha256"] != canonical_json_sha256(scope.to_dict()):
            raise ProjectValidationError("Hiss recipe scope identity is inconsistent.")
        if proposal_identity["noise_references_sha256"] != canonical_json_sha256(
            [item.to_dict() for item in references]
        ):
            raise ProjectValidationError("Hiss recipe reference identity is inconsistent.")
        if proposal_identity["analysis_config_sha256"] != canonical_json_sha256(
            analysis_config.to_dict()
        ):
            raise ProjectValidationError("Hiss recipe analysis config identity is inconsistent.")
        review = HissReviewAttestation.from_dict(data["review_attestation"], sample_count)
        if review.proposal_body_sha256 != proposal_identity["proposal_body_sha256"]:
            raise ProjectValidationError("Hiss recipe review attests a different proposal.")
        if review.selected_scope != scope:
            raise ProjectValidationError("Hiss recipe review attests a different scope.")
        config = HissPreviewConfig.from_dict(data["config"])
        shortest_reference = min(
            item.end_sample_exclusive - item.start_sample for item in references
        )
        fade_samples = round(sample_rate * config.edge_fade_ms / 1_000)
        if shortest_reference < config.frame_length:
            raise ProjectValidationError(
                "Hiss preview references are shorter than one preview frame."
            )
        if scope.end_sample_exclusive - scope.start_sample <= max(
            config.frame_length, 2 * fade_samples
        ):
            raise ProjectValidationError(
                "Hiss preview scope is too short for its frame and edge treatment."
            )
        algorithm = _object(data["algorithm"], "Hiss preview algorithm identity")
        algorithm_keys = {
            "config_sha256",
            "id",
            "module",
            "module_sha256",
            "numpy_version",
            "python_implementation",
            "python_version",
        }
        _strict_keys(algorithm, algorithm_keys, "Hiss preview algorithm identity")
        if algorithm["id"] != HISS_PREVIEW_ALGORITHM_ID or algorithm["module"] != HISS_MODULE_ID:
            raise ProjectValidationError("Hiss preview algorithm identity is unsupported.")
        _digest(algorithm["module_sha256"], "Hiss preview module SHA-256")
        _digest(algorithm["config_sha256"], "Hiss preview config SHA-256")
        if algorithm["config_sha256"] != canonical_json_sha256(config.to_dict()):
            raise ProjectValidationError("Hiss preview config identity is inconsistent.")
        for key in ("numpy_version", "python_implementation", "python_version"):
            _text(algorithm[key], f"Hiss preview {key}", 64)
        policy = _object(data["policy"], "Hiss preview recipe policy")
        expected_policy = _recipe_policy()
        _strict_keys(policy, set(expected_policy), "Hiss preview recipe policy")
        if policy != expected_policy:
            raise ProjectValidationError("Hiss preview recipe protections are mandatory.")
        return cls(
            schema=HISS_PREVIEW_RECIPE_SCHEMA,
            recipe_body_sha256=body_sha,
            proposal_identity={
                key: cast(str, proposal_identity[key]) for key in sorted(proposal_identity)
            },
            input_identity={
                "sample_rate": sample_rate,
                "sample_count": sample_count,
                "channel_count": channel_count,
            },
            selected_scope=scope,
            noise_references=references,
            effective_lower_hz=lower,
            effective_upper_hz=upper,
            analysis_config=analysis_config,
            review_attestation=review,
            algorithm={key: cast(str, algorithm[key]) for key in sorted(algorithm)},
            config=config,
            policy=expected_policy,
        )


@dataclass(frozen=True, slots=True)
class HissPreviewResult:
    original: np.ndarray
    proposed: np.ndarray
    removed: np.ndarray
    render_manifest: dict[str, Any]
    receipt: dict[str, Any]


def _strict_current_proposal(value: HissProposal | Mapping[str, Any]) -> HissProposal:
    raw = value.to_dict() if isinstance(value, HissProposal) else dict(value)
    proposal = HissProposal.from_dict(raw)
    if proposal.algorithm["module_sha256"] != _module_sha256():
        raise ProjectValidationError("Hiss proposal analysis module identity is stale.")
    if proposal.algorithm["numpy_version"] != np.__version__:
        raise ProjectValidationError("Hiss proposal NumPy identity is stale.")
    if proposal.status != "proposed":
        raise ProjectValidationError("Hiss preview cannot render an abstained proposal.")
    return proposal


def create_hiss_preview_recipe(
    proposal_value: HissProposal | Mapping[str, Any],
    review_attestation_value: HissReviewAttestation | Mapping[str, Any],
    *,
    config: HissPreviewConfig | None = None,
) -> HissPreviewRecipe:
    """Create an exact audition-only recipe from reviewed proposal evidence."""

    proposal = _strict_current_proposal(proposal_value)
    raw_attestation = (
        review_attestation_value.to_dict()
        if isinstance(review_attestation_value, HissReviewAttestation)
        else dict(review_attestation_value)
    )
    attestation = HissReviewAttestation.from_dict(raw_attestation, proposal.sample_count)
    if attestation.proposal_body_sha256 != proposal.proposal_body_sha256:
        raise ProjectValidationError("Hiss preview attestation is stale for this proposal.")
    if attestation.selected_scope != proposal.scope:
        raise ProjectValidationError(
            "Hiss preview v1 requires the exactly reviewed proposal scope."
        )
    settings = config or HissPreviewConfig()
    settings.validate()
    references_body = [item.to_dict() for item in proposal.noise_references]
    proposal_identity = {
        "analysis_config_sha256": proposal.algorithm["config_sha256"],
        "analysis_module_sha256": proposal.algorithm["module_sha256"],
        "analysis_numpy_version": proposal.algorithm["numpy_version"],
        "normalized_pcm_sha256": proposal.normalized_pcm_sha256,
        "noise_references_sha256": canonical_json_sha256(references_body),
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "scope_sha256": canonical_json_sha256(proposal.scope.to_dict()),
    }
    recipe = HissPreviewRecipe(
        schema=HISS_PREVIEW_RECIPE_SCHEMA,
        recipe_body_sha256="",
        proposal_identity=proposal_identity,
        input_identity={
            "sample_rate": proposal.sample_rate,
            "sample_count": proposal.sample_count,
            "channel_count": proposal.channel_count,
        },
        selected_scope=proposal.scope,
        noise_references=proposal.noise_references,
        effective_lower_hz=proposal.effective_lower_hz,
        effective_upper_hz=proposal.effective_upper_hz,
        analysis_config=proposal.config,
        review_attestation=attestation,
        algorithm={
            "config_sha256": canonical_json_sha256(settings.to_dict()),
            "id": HISS_PREVIEW_ALGORITHM_ID,
            "module": HISS_MODULE_ID,
            "module_sha256": _module_sha256(),
            **_runtime_identity(),
        },
        config=settings,
        policy=_recipe_policy(),
    )
    recipe = replace(recipe, recipe_body_sha256=canonical_json_sha256(recipe.body_dict()))
    return HissPreviewRecipe.from_dict(recipe.to_dict())


def _overlap_starts(start: int, end: int, length: int, hop: int) -> tuple[int, ...]:
    if end - start < length:
        return ()
    values = list(range(start, end - length + 1, hop))
    final = end - length
    if not values or values[-1] != final:
        values.append(final)
    return tuple(values)


def _frequency_mask(recipe: HissPreviewRecipe) -> np.ndarray:
    frequencies = np.fft.rfftfreq(
        recipe.config.frame_length,
        d=1.0 / int(recipe.input_identity["sample_rate"]),
    )
    return (frequencies >= recipe.effective_lower_hz) & (frequencies <= recipe.effective_upper_hz)


def _reference_noise_psd(pcm: np.ndarray, recipe: HissPreviewRecipe) -> np.ndarray:
    frame = recipe.config.frame_length
    hop = recipe.config.hop_length
    taper = np.sqrt(np.hanning(frame))
    channel_psds: list[np.ndarray] = []
    for channel in range(pcm.shape[1]):
        frames: list[np.ndarray] = []
        for reference in recipe.noise_references:
            for start in _overlap_starts(
                reference.start_sample,
                reference.end_sample_exclusive,
                frame,
                hop,
            ):
                spectrum = np.fft.rfft(pcm[start : start + frame, channel] * taper)
                frames.append(np.abs(spectrum) ** 2)
        if not frames:
            raise ProjectValidationError("Hiss preview has no complete reference frames.")
        channel_psds.append(np.median(np.stack(frames), axis=0))
    result = np.ascontiguousarray(np.stack(channel_psds), dtype="<f8")
    if not bool(np.all(np.isfinite(result))) or bool(np.any(result < 0.0)):
        raise ProjectValidationError("Hiss reference estimate is nonfinite or negative.")
    return result


def _edge_envelope(length: int, fade_samples: int) -> np.ndarray:
    envelope = np.ones(length, dtype=np.float64)
    if fade_samples > 0:
        phase = np.linspace(0.0, np.pi, fade_samples, endpoint=True)
        fade = 0.5 - 0.5 * np.cos(phase)
        envelope[:fade_samples] = fade
        envelope[-fade_samples:] = fade[::-1]
    envelope[0] = 0.0
    envelope[-1] = 0.0
    return envelope


def _preview_frame_is_safe(
    spectrum: np.ndarray,
    mask: np.ndarray,
    frame_values: np.ndarray,
    recipe: HissPreviewRecipe,
) -> bool:
    power = np.abs(spectrum[mask]) ** 2
    total = float(np.sum(power))
    if power.size == 0 or total <= _EPSILON:
        return False
    mean_power = float(np.mean(power))
    flatness = float(
        np.exp(np.mean(np.log(np.maximum(power, _EPSILON)))) / max(mean_power, _EPSILON)
    )
    concentration = float(np.max(power) / total)
    filtered = np.fft.irfft(np.where(mask, spectrum, 0.0), n=frame_values.size)
    filtered_rms = max(float(np.sqrt(np.mean(filtered**2))), _EPSILON)
    crest = float(np.max(np.abs(filtered)) / filtered_rms)
    analysis = recipe.analysis_config
    return (
        flatness >= analysis.minimum_spectral_flatness * 0.75
        and concentration <= analysis.maximum_peak_concentration * 2.0
        and crest <= analysis.maximum_high_band_crest_factor * 1.25
    )


def _render_removed(
    pcm: np.ndarray,
    recipe: HissPreviewRecipe,
    noise_psd: np.ndarray,
) -> np.ndarray:
    scope = recipe.selected_scope
    frame = recipe.config.frame_length
    hop = recipe.config.hop_length
    taper = np.sqrt(np.hanning(frame))
    mask = _frequency_mask(recipe)
    minimum_gain = 10.0 ** (-recipe.config.maximum_attenuation_db / 20.0)
    scope_length = scope.end_sample_exclusive - scope.start_sample
    removed_scope = np.zeros((scope_length, pcm.shape[1]), dtype=np.float64)
    normalization = np.zeros(scope_length, dtype=np.float64)
    for start in _overlap_starts(
        scope.start_sample,
        scope.end_sample_exclusive,
        frame,
        hop,
    ):
        relative = start - scope.start_sample
        normalization[relative : relative + frame] += taper**2
        for channel in range(pcm.shape[1]):
            values = pcm[start : start + frame, channel]
            spectrum = np.fft.rfft(values * taper)
            if not _preview_frame_is_safe(spectrum, mask, values, recipe):
                continue
            power = np.abs(spectrum) ** 2
            ratio = (
                recipe.config.subtraction_strength
                * noise_psd[channel]
                / np.maximum(
                    power,
                    _EPSILON,
                )
            )
            gains = np.ones_like(power)
            gains[mask] = np.maximum(
                np.sqrt(np.maximum(1.0 - ratio[mask], 0.0)),
                minimum_gain,
            )
            removed_spectrum = spectrum * (1.0 - gains)
            removed_frame = np.fft.irfft(removed_spectrum, n=frame) * taper
            removed_scope[relative : relative + frame, channel] += removed_frame
    valid = normalization > _EPSILON
    removed_scope[valid] /= normalization[valid, np.newaxis]
    fade_samples = round(
        int(recipe.input_identity["sample_rate"]) * recipe.config.edge_fade_ms / 1_000
    )
    removed_scope *= _edge_envelope(scope_length, fade_samples)[:, np.newaxis]
    removed = np.zeros_like(pcm)
    removed[scope.start_sample : scope.end_sample_exclusive] = removed_scope
    if not bool(np.all(np.isfinite(removed))):
        raise ProjectValidationError("Hiss preview produced nonfinite residue.")
    return removed


def _program_ranges(recipe: HissPreviewRecipe) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    cursor = recipe.selected_scope.start_sample
    for reference in recipe.noise_references:
        if cursor < reference.start_sample:
            ranges.append((cursor, reference.start_sample))
        cursor = reference.end_sample_exclusive
    if cursor < recipe.selected_scope.end_sample_exclusive:
        ranges.append((cursor, recipe.selected_scope.end_sample_exclusive))
    return tuple(ranges)


def _band_power(values: np.ndarray, sample_rate: int, lower: float, upper: float) -> float:
    if values.size < 2:
        return 0.0
    spectrum = np.fft.rfft(values * np.hanning(values.size))
    frequencies = np.fft.rfftfreq(values.size, d=1.0 / sample_rate)
    mask = (frequencies >= lower) & (frequencies <= upper)
    return float(np.sum(np.abs(spectrum[mask]) ** 2))


def _channel_metrics(
    original: np.ndarray,
    proposed: np.ndarray,
    removed: np.ndarray,
    recipe: HissPreviewRecipe,
) -> list[dict[str, Any]]:
    scope = recipe.selected_scope
    selected = slice(scope.start_sample, scope.end_sample_exclusive)
    sample_rate = int(recipe.input_identity["sample_rate"])
    metrics: list[dict[str, Any]] = []
    for channel in range(original.shape[1]):
        original_scope = original[selected, channel]
        proposed_scope = proposed[selected, channel]
        removed_scope = removed[selected, channel]
        original_energy = max(float(np.sum(original_scope**2)), _EPSILON)
        removed_energy = float(np.sum(removed_scope**2))
        original_band = _band_power(
            original_scope,
            sample_rate,
            recipe.effective_lower_hz,
            recipe.effective_upper_hz,
        )
        proposed_band = _band_power(
            proposed_scope,
            sample_rate,
            recipe.effective_lower_hz,
            recipe.effective_upper_hz,
        )
        removed_band = _band_power(
            removed_scope,
            sample_rate,
            recipe.effective_lower_hz,
            recipe.effective_upper_hz,
        )
        removed_total_band = _band_power(removed_scope, sample_rate, 1.0, sample_rate * 0.5)
        reference_original = np.concatenate(
            [
                original[item.start_sample : item.end_sample_exclusive, channel]
                for item in recipe.noise_references
            ]
        )
        reference_proposed = np.concatenate(
            [
                proposed[item.start_sample : item.end_sample_exclusive, channel]
                for item in recipe.noise_references
            ]
        )
        reference_original_band = _band_power(
            reference_original,
            sample_rate,
            recipe.effective_lower_hz,
            recipe.effective_upper_hz,
        )
        reference_proposed_band = _band_power(
            reference_proposed,
            sample_rate,
            recipe.effective_lower_hz,
            recipe.effective_upper_hz,
        )
        metrics.append(
            {
                "channel_index": channel,
                "original_scope_rms_dbfs": _quantize(_db_rms(original_scope)),
                "proposed_scope_rms_dbfs": _quantize(_db_rms(proposed_scope)),
                "removed_rms_dbfs": _quantize(_db_rms(removed_scope)),
                "removed_peak": _quantize(float(np.max(np.abs(removed_scope)))),
                "removed_energy_ratio": _quantize(removed_energy / original_energy),
                "scope_high_band_reduction_db": _quantize(
                    10.0 * math.log10(max(original_band, _EPSILON) / max(proposed_band, _EPSILON))
                ),
                "reference_high_band_reduction_db": _quantize(
                    10.0
                    * math.log10(
                        max(reference_original_band, _EPSILON)
                        / max(reference_proposed_band, _EPSILON)
                    )
                ),
                "removed_high_band_fraction": _quantize(
                    removed_band / max(removed_total_band, _EPSILON)
                ),
                "scope_rms_change_db": _quantize(
                    abs(_db_rms(proposed_scope) - _db_rms(original_scope))
                ),
            }
        )
    return metrics


def _validate_metric_caps(metrics: Sequence[Mapping[str, Any]], recipe: HissPreviewRecipe) -> None:
    settings = recipe.config
    removed_levels = [float(item["removed_rms_dbfs"]) for item in metrics]
    if any(float(item["removed_peak"]) > settings.maximum_removed_peak for item in metrics):
        raise ProjectValidationError("Hiss preview exceeds the removed-peak cap.")
    if any(
        float(item["removed_energy_ratio"]) > settings.maximum_removed_energy_ratio
        for item in metrics
    ):
        raise ProjectValidationError("Hiss preview exceeds the removed-energy cap.")
    if any(
        float(item["reference_high_band_reduction_db"])
        < settings.minimum_reference_high_band_reduction_db
        for item in metrics
    ):
        raise ProjectValidationError("Hiss preview does not measurably reduce reference hiss.")
    if any(
        not 0.0
        <= float(item["scope_high_band_reduction_db"])
        <= settings.maximum_high_band_reduction_db
        for item in metrics
    ):
        raise ProjectValidationError("Hiss preview exceeds its high-band reduction cap.")
    if any(
        float(item["removed_high_band_fraction"]) < settings.minimum_removed_high_band_fraction
        for item in metrics
    ):
        raise ProjectValidationError("Hiss preview residue is not sufficiently high-frequency.")
    if any(
        float(item["scope_rms_change_db"]) > settings.maximum_scope_rms_change_db
        for item in metrics
    ):
        raise ProjectValidationError("Hiss preview exceeds its scope RMS-change cap.")
    if _spread(removed_levels) > settings.maximum_channel_removed_rms_spread_db:
        raise ProjectValidationError("Hiss preview residue levels disagree across channels.")


def _audition_gains(
    original: np.ndarray,
    proposed: np.ndarray,
    recipe: HissPreviewRecipe,
) -> dict[str, Any]:
    sample_rate = int(recipe.input_identity["sample_rate"])
    window = round(sample_rate * recipe.analysis_config.window_ms / 1_000)
    deltas: list[float] = []
    for start, end in _program_ranges(recipe):
        for position in _window_starts(start, end, window):
            original_window = original[position : position + window]
            proposed_window = proposed[position : position + window]
            original_level = _db_rms(original_window)
            proposed_level = _db_rms(proposed_window)
            if original_level >= recipe.config.loudness_window_floor_dbfs:
                deltas.append(original_level - proposed_level)
    if len(deltas) < recipe.config.minimum_loudness_windows:
        raise ProjectValidationError("Hiss preview has insufficient program windows for matching.")
    gain_db = _median(deltas)
    mad = _median([abs(value - gain_db) for value in deltas])
    if abs(gain_db) > recipe.config.maximum_match_gain_db:
        raise ProjectValidationError("Hiss preview matched-loudness gain exceeds its cap.")
    if mad > recipe.config.maximum_match_mad_db:
        raise ProjectValidationError("Hiss preview loudness matching is not stable.")
    linear = 10.0 ** (gain_db / 20.0)
    return {
        "method": "median_program_window_rms_delta/1",
        "window_count": len(deltas),
        "original_linear_gain": recipe.config.original_audition_gain,
        "proposed_gain_db": _quantize(gain_db),
        "proposed_linear_gain": _quantize(linear),
        "match_mad_db": _quantize(mad),
        "residue_monitor_linear_gain": recipe.config.residue_monitor_gain,
        "raw_arrays_are_gain_neutral": True,
    }


def _aggregate(metrics: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "maximum_removed_peak": max(float(item["removed_peak"]) for item in metrics),
        "maximum_removed_energy_ratio": max(
            float(item["removed_energy_ratio"]) for item in metrics
        ),
        "maximum_scope_high_band_reduction_db": max(
            float(item["scope_high_band_reduction_db"]) for item in metrics
        ),
        "minimum_reference_high_band_reduction_db": min(
            float(item["reference_high_band_reduction_db"]) for item in metrics
        ),
        "minimum_removed_high_band_fraction": min(
            float(item["removed_high_band_fraction"]) for item in metrics
        ),
        "maximum_scope_rms_change_db": max(float(item["scope_rms_change_db"]) for item in metrics),
    }


def _validate_algorithm(value: Any, label: str) -> dict[str, str]:
    data = _object(value, label)
    keys = {
        "config_sha256",
        "id",
        "module",
        "module_sha256",
        "numpy_version",
        "python_implementation",
        "python_version",
    }
    _strict_keys(data, keys, label)
    if data["id"] != HISS_PREVIEW_ALGORITHM_ID or data["module"] != HISS_MODULE_ID:
        raise ProjectValidationError(f"{label} identity is unsupported.")
    _digest(data["config_sha256"], f"{label} config SHA-256")
    _digest(data["module_sha256"], f"{label} module SHA-256")
    for key in ("numpy_version", "python_implementation", "python_version"):
        _text(data[key], f"{label} {key}", 64)
    return {key: cast(str, data[key]) for key in sorted(data)}


def _validate_input(value: Any, label: str) -> dict[str, Any]:
    data = _object(value, label)
    _strict_keys(
        data,
        {"channel_count", "normalized_pcm_sha256", "sample_count", "sample_rate"},
        label,
    )
    return {
        "sample_rate": _integer(data["sample_rate"], f"{label} sample rate", 16_000, 768_000),
        "sample_count": _integer(data["sample_count"], f"{label} sample count", 1, 2**63 - 1),
        "channel_count": _integer(data["channel_count"], f"{label} channels", 1, _MAX_CHANNELS),
        "normalized_pcm_sha256": _digest(data["normalized_pcm_sha256"], f"{label} PCM SHA-256"),
    }


def _validate_noise_estimate(value: Any) -> dict[str, Any]:
    data = _object(value, "Hiss reference-noise estimate")
    _strict_keys(
        data,
        {"method", "noise_psd_sha256", "reference_frame_count", "references_sha256"},
        "Hiss reference-noise estimate",
    )
    if data["method"] != "median_reference_only_power_spectrum/1":
        raise ProjectValidationError("Hiss reference-noise estimate method is unsupported.")
    return {
        "method": data["method"],
        "noise_psd_sha256": _digest(data["noise_psd_sha256"], "Hiss reference PSD SHA-256"),
        "reference_frame_count": _integer(
            data["reference_frame_count"], "Hiss reference frame count", 1, 10_000_000
        ),
        "references_sha256": _digest(data["references_sha256"], "Hiss references SHA-256"),
    }


def _validate_raw_arrays(value: Any) -> dict[str, Any]:
    data = _object(value, "Hiss raw-array identity")
    _strict_keys(
        data,
        {
            "algebra",
            "maximum_reconstruction_error",
            "original_sha256",
            "proposed_sha256",
            "removed_sha256",
        },
        "Hiss raw-array identity",
    )
    if data["algebra"] != "original = proposed + removed":
        raise ProjectValidationError("Hiss raw-array algebra is unsupported.")
    return {
        "original_sha256": _digest(data["original_sha256"], "Hiss original SHA-256"),
        "proposed_sha256": _digest(data["proposed_sha256"], "Hiss proposed SHA-256"),
        "removed_sha256": _digest(data["removed_sha256"], "Hiss removed SHA-256"),
        "algebra": data["algebra"],
        "maximum_reconstruction_error": _number(
            data["maximum_reconstruction_error"],
            "Hiss reconstruction error",
            0.0,
            1.0,
        ),
    }


def _validate_audition(value: Any) -> dict[str, Any]:
    data = _object(value, "Hiss audition identity")
    keys = {
        "match_mad_db",
        "method",
        "original_linear_gain",
        "proposed_gain_db",
        "proposed_linear_gain",
        "raw_arrays_are_gain_neutral",
        "residue_monitor_linear_gain",
        "window_count",
    }
    _strict_keys(data, keys, "Hiss audition identity")
    if data["method"] != "median_program_window_rms_delta/1":
        raise ProjectValidationError("Hiss audition matching method is unsupported.")
    if data["raw_arrays_are_gain_neutral"] is not True:
        raise ProjectValidationError("Hiss audition raw arrays must remain gain-neutral.")
    original_gain = _number(data["original_linear_gain"], "Hiss original gain", 0.0, 32.0)
    proposed_db = _number(data["proposed_gain_db"], "Hiss proposed gain dB", -20.0, 20.0)
    proposed_linear = _number(data["proposed_linear_gain"], "Hiss proposed gain", 0.0, 32.0)
    if original_gain != 1.0:
        raise ProjectValidationError("Hiss original audition gain must be exactly 1.0.")
    if not math.isclose(
        proposed_linear,
        10.0 ** (proposed_db / 20.0),
        rel_tol=1e-10,
        abs_tol=1e-12,
    ):
        raise ProjectValidationError("Hiss audition gain dB and linear value disagree.")
    return {
        "method": data["method"],
        "window_count": _integer(data["window_count"], "Hiss match windows", 1, 10_000_000),
        "original_linear_gain": original_gain,
        "proposed_gain_db": proposed_db,
        "proposed_linear_gain": proposed_linear,
        "match_mad_db": _number(data["match_mad_db"], "Hiss match MAD", 0.0, 20.0),
        "residue_monitor_linear_gain": _number(
            data["residue_monitor_linear_gain"], "Hiss residue gain", 1.0, 32.0
        ),
        "raw_arrays_are_gain_neutral": True,
    }


def validate_hiss_preview_render_manifest(
    value: Any,
    *,
    recipe: HissPreviewRecipe | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    data = _object(value, "Hiss preview render manifest")
    keys = {
        "algorithm",
        "audition",
        "input",
        "noise_estimate",
        "policy",
        "proposal_body_sha256",
        "raw_arrays",
        "recipe_body_sha256",
        "render_body_sha256",
        "schema",
        "selected_scope",
    }
    _strict_keys(data, keys, "Hiss preview render manifest")
    if data["schema"] != HISS_PREVIEW_RENDER_SCHEMA:
        raise ProjectValidationError("Hiss preview render schema is unsupported.")
    body_sha = _digest(data["render_body_sha256"], "Hiss render body SHA-256")
    body = dict(data)
    del body["render_body_sha256"]
    if canonical_json_sha256(body) != body_sha:
        raise ProjectValidationError("Hiss preview render identity is stale.")
    algorithm = _validate_algorithm(data["algorithm"], "Hiss render algorithm")
    input_identity = _validate_input(data["input"], "Hiss render input")
    scope = NoiseAnalysisScope.from_dict(data["selected_scope"], input_identity["sample_count"])
    noise_estimate = _validate_noise_estimate(data["noise_estimate"])
    raw_arrays = _validate_raw_arrays(data["raw_arrays"])
    audition = _validate_audition(data["audition"])
    policy = _object(data["policy"], "Hiss render policy")
    expected_policy = _render_policy()
    _strict_keys(policy, set(expected_policy), "Hiss render policy")
    if policy != expected_policy:
        raise ProjectValidationError("Hiss preview render protections are mandatory.")
    parsed_recipe: HissPreviewRecipe | None = None
    if recipe is not None:
        parsed_recipe = (
            recipe
            if isinstance(recipe, HissPreviewRecipe)
            else HissPreviewRecipe.from_dict(dict(recipe))
        )
        if data["recipe_body_sha256"] != parsed_recipe.recipe_body_sha256:
            raise ProjectValidationError("Hiss render belongs to a different recipe.")
        if data["proposal_body_sha256"] != parsed_recipe.proposal_identity["proposal_body_sha256"]:
            raise ProjectValidationError("Hiss render belongs to a different proposal.")
        if input_identity != {
            **parsed_recipe.input_identity,
            "normalized_pcm_sha256": parsed_recipe.proposal_identity["normalized_pcm_sha256"],
        }:
            raise ProjectValidationError("Hiss render input differs from its recipe.")
        if scope != parsed_recipe.selected_scope:
            raise ProjectValidationError("Hiss render scope differs from its recipe.")
        if algorithm != parsed_recipe.algorithm:
            raise ProjectValidationError("Hiss render algorithm differs from its recipe.")
        if audition["residue_monitor_linear_gain"] != parsed_recipe.config.residue_monitor_gain:
            raise ProjectValidationError("Hiss render residue gain differs from its recipe.")
        if abs(float(audition["proposed_gain_db"])) > parsed_recipe.config.maximum_match_gain_db:
            raise ProjectValidationError("Hiss render gain exceeds its recipe cap.")
        if float(audition["match_mad_db"]) > parsed_recipe.config.maximum_match_mad_db:
            raise ProjectValidationError("Hiss render matching MAD exceeds its recipe cap.")
        references_sha = canonical_json_sha256(
            [item.to_dict() for item in parsed_recipe.noise_references]
        )
        if noise_estimate["references_sha256"] != references_sha:
            raise ProjectValidationError("Hiss render reference estimate is stale.")
    return {
        **body,
        "algorithm": algorithm,
        "input": input_identity,
        "selected_scope": scope.to_dict(),
        "noise_estimate": noise_estimate,
        "raw_arrays": raw_arrays,
        "audition": audition,
        "policy": expected_policy,
        "render_body_sha256": body_sha,
    }


_METRIC_KEYS = {
    "channel_index",
    "original_scope_rms_dbfs",
    "proposed_scope_rms_dbfs",
    "reference_high_band_reduction_db",
    "removed_energy_ratio",
    "removed_high_band_fraction",
    "removed_peak",
    "removed_rms_dbfs",
    "scope_high_band_reduction_db",
    "scope_rms_change_db",
}


def _parse_metrics(value: Any, channels: int) -> list[dict[str, Any]]:
    raw = _array(value, "Hiss receipt channel metrics")
    if len(raw) != channels:
        raise ProjectValidationError("Hiss receipt metric channel count is inconsistent.")
    parsed: list[dict[str, Any]] = []
    for channel, item in enumerate(raw):
        metric = _object(item, "Hiss receipt channel metric")
        _strict_keys(metric, _METRIC_KEYS, "Hiss receipt channel metric")
        if metric["channel_index"] != channel:
            raise ProjectValidationError("Hiss receipt metrics are unordered.")
        parsed.append(
            {
                "channel_index": channel,
                "original_scope_rms_dbfs": _number(
                    metric["original_scope_rms_dbfs"], "Hiss original scope RMS", -400.0, 1.0
                ),
                "proposed_scope_rms_dbfs": _number(
                    metric["proposed_scope_rms_dbfs"], "Hiss proposed scope RMS", -400.0, 1.0
                ),
                "removed_rms_dbfs": _number(
                    metric["removed_rms_dbfs"], "Hiss removed RMS", -400.0, 1.0
                ),
                "removed_peak": _number(metric["removed_peak"], "Hiss removed peak", 0.0, 1.0),
                "removed_energy_ratio": _number(
                    metric["removed_energy_ratio"], "Hiss removed energy ratio", 0.0, 1.0
                ),
                "scope_high_band_reduction_db": _number(
                    metric["scope_high_band_reduction_db"],
                    "Hiss scope band reduction",
                    -100.0,
                    100.0,
                ),
                "reference_high_band_reduction_db": _number(
                    metric["reference_high_band_reduction_db"],
                    "Hiss reference band reduction",
                    -100.0,
                    100.0,
                ),
                "removed_high_band_fraction": _number(
                    metric["removed_high_band_fraction"], "Hiss removed band fraction", 0.0, 1.0
                ),
                "scope_rms_change_db": _number(
                    metric["scope_rms_change_db"], "Hiss scope RMS change", 0.0, 100.0
                ),
            }
        )
    return parsed


def _validate_aggregate(value: Any, metrics: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    data = _object(value, "Hiss receipt aggregate")
    expected = _aggregate(metrics)
    _strict_keys(data, set(expected), "Hiss receipt aggregate")
    for key in expected:
        _number(data[key], f"Hiss aggregate {key}", 0.0, 100.0)
    if data != expected:
        raise ProjectValidationError("Hiss receipt aggregate does not match its metrics.")
    return expected


def validate_hiss_preview_receipt(
    value: Any,
    *,
    recipe: HissPreviewRecipe | Mapping[str, Any] | None = None,
    render_manifest: Mapping[str, Any] | None = None,
    arrays: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> dict[str, Any]:
    data = _object(value, "Hiss preview receipt")
    keys = {
        "aggregate",
        "algorithm",
        "audition",
        "channel_metrics",
        "input",
        "noise_estimate",
        "policy",
        "proof",
        "proposal_body_sha256",
        "raw_arrays",
        "receipt_body_sha256",
        "recipe_body_sha256",
        "render_body_sha256",
        "schema",
        "selected_scope",
    }
    _strict_keys(data, keys, "Hiss preview receipt")
    if data["schema"] != HISS_PREVIEW_RECEIPT_SCHEMA:
        raise ProjectValidationError("Hiss preview receipt schema is unsupported.")
    body_sha = _digest(data["receipt_body_sha256"], "Hiss receipt body SHA-256")
    body = dict(data)
    del body["receipt_body_sha256"]
    if canonical_json_sha256(body) != body_sha:
        raise ProjectValidationError("Hiss preview receipt identity is stale.")
    algorithm = _validate_algorithm(data["algorithm"], "Hiss receipt algorithm")
    input_identity = _validate_input(data["input"], "Hiss receipt input")
    scope = NoiseAnalysisScope.from_dict(data["selected_scope"], input_identity["sample_count"])
    noise_estimate = _validate_noise_estimate(data["noise_estimate"])
    raw_arrays = _validate_raw_arrays(data["raw_arrays"])
    audition = _validate_audition(data["audition"])
    metrics = _parse_metrics(data["channel_metrics"], input_identity["channel_count"])
    aggregate = _validate_aggregate(data["aggregate"], metrics)
    proof = _object(data["proof"], "Hiss receipt proof")
    proof_keys = {
        "audition_gains_do_not_clip",
        "edge_residue_starts_and_ends_at_zero",
        "original_matches_input",
        "outside_scope_proposed_bit_identical",
        "outside_scope_removed_zero",
        "proposed_does_not_clip",
        "raw_algebra_float64_bounded",
        "source_array_immutable",
    }
    _strict_keys(proof, proof_keys, "Hiss receipt proof")
    if any(proof[key] is not True for key in proof_keys):
        raise ProjectValidationError("Hiss preview receipt proof must be wholly true.")
    policy = _object(data["policy"], "Hiss receipt policy")
    expected_policy = _receipt_policy()
    _strict_keys(policy, set(expected_policy), "Hiss receipt policy")
    if policy != expected_policy:
        raise ProjectValidationError("Hiss preview receipt protections are mandatory.")
    parsed_recipe: HissPreviewRecipe | None = None
    if recipe is not None:
        parsed_recipe = (
            recipe
            if isinstance(recipe, HissPreviewRecipe)
            else HissPreviewRecipe.from_dict(dict(recipe))
        )
        if data["recipe_body_sha256"] != parsed_recipe.recipe_body_sha256:
            raise ProjectValidationError("Hiss receipt belongs to a different recipe.")
        if data["proposal_body_sha256"] != parsed_recipe.proposal_identity["proposal_body_sha256"]:
            raise ProjectValidationError("Hiss receipt belongs to a different proposal.")
        if input_identity != {
            **parsed_recipe.input_identity,
            "normalized_pcm_sha256": parsed_recipe.proposal_identity["normalized_pcm_sha256"],
        }:
            raise ProjectValidationError("Hiss receipt input differs from its recipe.")
        if scope != parsed_recipe.selected_scope or algorithm != parsed_recipe.algorithm:
            raise ProjectValidationError("Hiss receipt scope or algorithm differs from its recipe.")
        _validate_metric_caps(metrics, parsed_recipe)
    if render_manifest is not None:
        parsed_render = validate_hiss_preview_render_manifest(
            dict(render_manifest),
            recipe=parsed_recipe,
        )
        if data["render_body_sha256"] != parsed_render["render_body_sha256"]:
            raise ProjectValidationError("Hiss receipt belongs to a different render.")
        for field in (
            "algorithm",
            "audition",
            "input",
            "noise_estimate",
            "proposal_body_sha256",
            "raw_arrays",
            "recipe_body_sha256",
            "selected_scope",
        ):
            if data[field] != parsed_render[field]:
                raise ProjectValidationError(f"Hiss receipt {field} differs from its render.")
    if arrays is not None:
        if parsed_recipe is None:
            raise ProjectValidationError("Hiss receipt array validation requires its recipe.")
        original, original_mono = _normalize_pcm(arrays[0])
        proposed, proposed_mono = _normalize_pcm(arrays[1])
        removed, removed_mono = _normalize_pcm(arrays[2])
        expected_shape = (input_identity["sample_count"], input_identity["channel_count"])
        if (
            original_mono != proposed_mono
            or original_mono != removed_mono
            or original.shape != expected_shape
            or proposed.shape != expected_shape
            or removed.shape != expected_shape
        ):
            raise ProjectValidationError("Hiss receipt arrays have inconsistent geometry.")
        if (
            _pcm_sha256(original) != raw_arrays["original_sha256"]
            or _pcm_sha256(proposed) != raw_arrays["proposed_sha256"]
            or _pcm_sha256(removed) != raw_arrays["removed_sha256"]
            or _pcm_sha256(original) != input_identity["normalized_pcm_sha256"]
        ):
            raise ProjectValidationError("Hiss receipt arrays do not match their identities.")
        reconstruction = float(np.max(np.abs(original - (proposed + removed))))
        if _quantize(reconstruction) != raw_arrays["maximum_reconstruction_error"]:
            raise ProjectValidationError("Hiss receipt array algebra differs from its report.")
        before = slice(0, scope.start_sample)
        after = slice(scope.end_sample_exclusive, original.shape[0])
        if (
            not np.array_equal(proposed[before], original[before])
            or not np.array_equal(proposed[after], original[after])
            or bool(np.any(removed[before]))
            or bool(np.any(removed[after]))
            or bool(np.any(removed[scope.start_sample]))
            or bool(np.any(removed[scope.end_sample_exclusive - 1]))
        ):
            raise ProjectValidationError("Hiss receipt arrays violate scope or edge isolation.")
        recalculated_metrics = _channel_metrics(original, proposed, removed, parsed_recipe)
        if recalculated_metrics != metrics:
            raise ProjectValidationError("Hiss receipt metrics do not match the supplied arrays.")
        if _aggregate(recalculated_metrics) != aggregate:
            raise ProjectValidationError("Hiss receipt aggregate does not match supplied arrays.")
        proposed_peak = float(np.max(np.abs(proposed))) * float(audition["proposed_linear_gain"])
        residue_peak = float(np.max(np.abs(removed))) * float(
            audition["residue_monitor_linear_gain"]
        )
        if proposed_peak > 1.0 or residue_peak > 1.0:
            raise ProjectValidationError("Hiss receipt arrays violate audition gain bounds.")
    return {
        **body,
        "algorithm": algorithm,
        "input": input_identity,
        "selected_scope": scope.to_dict(),
        "noise_estimate": noise_estimate,
        "raw_arrays": raw_arrays,
        "audition": audition,
        "channel_metrics": metrics,
        "aggregate": aggregate,
        "proof": {key: True for key in sorted(proof_keys)},
        "policy": expected_policy,
        "receipt_body_sha256": body_sha,
    }


def render_hiss_preview(
    samples: np.ndarray,
    proposal_value: HissProposal | Mapping[str, Any],
    recipe_value: HissPreviewRecipe | Mapping[str, Any],
) -> HissPreviewResult:
    """Render immutable audition arrays after independently rechecking evidence."""

    if type(samples) is not np.ndarray:
        raise ProjectValidationError("Hiss preview PCM must be a NumPy array.")
    source_before = hashlib.sha256(samples.tobytes(order="A")).hexdigest()
    pcm, was_mono = _normalize_pcm(samples)
    proposal = _strict_current_proposal(proposal_value)
    recipe = (
        recipe_value
        if isinstance(recipe_value, HissPreviewRecipe)
        else HissPreviewRecipe.from_dict(dict(recipe_value))
    )
    recipe = HissPreviewRecipe.from_dict(recipe.to_dict())
    if recipe.algorithm["module_sha256"] != _module_sha256():
        raise ProjectValidationError("Hiss preview recipe module identity is stale.")
    if recipe.algorithm["numpy_version"] != np.__version__:
        raise ProjectValidationError("Hiss preview recipe NumPy identity is stale.")
    current_runtime = _runtime_identity()
    for key, expected in current_runtime.items():
        if recipe.algorithm[key] != expected:
            raise ProjectValidationError(f"Hiss preview recipe {key} identity is stale.")
    if recipe.proposal_identity["proposal_body_sha256"] != proposal.proposal_body_sha256:
        raise ProjectValidationError("Hiss preview recipe belongs to a different proposal.")
    if recipe.proposal_identity["analysis_module_sha256"] != proposal.algorithm["module_sha256"]:
        raise ProjectValidationError("Hiss preview analysis module identity is stale.")
    if recipe.proposal_identity["analysis_config_sha256"] != proposal.algorithm["config_sha256"]:
        raise ProjectValidationError("Hiss preview analysis config identity is stale.")
    if (
        recipe.selected_scope != proposal.scope
        or recipe.noise_references != proposal.noise_references
    ):
        raise ProjectValidationError("Hiss preview reviewed scope or references are stale.")
    if recipe.analysis_config != proposal.config:
        raise ProjectValidationError("Hiss preview analysis thresholds are stale.")
    if recipe.input_identity != {
        "sample_rate": proposal.sample_rate,
        "sample_count": proposal.sample_count,
        "channel_count": proposal.channel_count,
    } or pcm.shape != (proposal.sample_count, proposal.channel_count):
        raise ProjectValidationError("Hiss preview input geometry is stale.")
    input_sha = _pcm_sha256(pcm)
    if (
        input_sha != proposal.normalized_pcm_sha256
        or input_sha != recipe.proposal_identity["normalized_pcm_sha256"]
    ):
        raise ProjectValidationError("Hiss preview PCM differs from the reviewed proposal.")
    if bool(np.any(np.abs(pcm) >= proposal.config.clipping_amplitude)):
        raise ProjectValidationError("Hiss preview refuses clipped source PCM.")

    # Re-run the exact current analyzer.  This prevents a self-consistently
    # rehashed but semantically forged evidence document from becoming render
    # authority.
    reproduced = analyze_hiss(
        pcm,
        sample_rate=proposal.sample_rate,
        scope=proposal.scope,
        noise_references=proposal.noise_references,
        config=proposal.config,
    )
    if reproduced.to_dict() != proposal.to_dict():
        raise ProjectValidationError("Hiss proposal evidence cannot be reproduced from the PCM.")

    noise_psd = _reference_noise_psd(pcm, recipe)
    noise_psd_sha = hashlib.sha256(noise_psd.tobytes(order="C")).hexdigest()
    reference_frame_count = sum(
        len(
            _overlap_starts(
                item.start_sample,
                item.end_sample_exclusive,
                recipe.config.frame_length,
                recipe.config.hop_length,
            )
        )
        for item in recipe.noise_references
    )
    raw_original = pcm.copy()
    estimated_removed = _render_removed(pcm, recipe, noise_psd)
    raw_proposed = raw_original - estimated_removed
    # The public residue array is the exact float64 difference that the owner
    # hears between Original and Proposed.  This makes its semantics directly
    # checkable instead of exposing the pre-rounding estimator workspace.
    raw_removed = raw_original - raw_proposed
    if not bool(np.all(np.isfinite(raw_proposed))):
        raise ProjectValidationError("Hiss preview produced nonfinite proposed PCM.")
    if bool(np.any(np.abs(raw_proposed) > 1.0)):
        raise ProjectValidationError("Hiss preview proposed PCM would clip.")
    metrics = _channel_metrics(raw_original, raw_proposed, raw_removed, recipe)
    _validate_metric_caps(metrics, recipe)
    audition = _audition_gains(raw_original, raw_proposed, recipe)
    proposed_monitor_peak = float(np.max(np.abs(raw_proposed))) * float(
        audition["proposed_linear_gain"]
    )
    residue_monitor_peak = float(np.max(np.abs(raw_removed))) * float(
        audition["residue_monitor_linear_gain"]
    )
    if proposed_monitor_peak > 1.0 or residue_monitor_peak > 1.0:
        raise ProjectValidationError("Hiss preview audition gain would clip.")

    scope = recipe.selected_scope
    before = slice(0, scope.start_sample)
    after = slice(scope.end_sample_exclusive, pcm.shape[0])
    reconstruction_error = float(np.max(np.abs(raw_original - (raw_proposed + raw_removed))))
    algebra_tolerance = float(
        np.finfo(np.float64).eps * 2.0 * max(1.0, float(np.max(np.abs(raw_original))))
    )
    source_after = hashlib.sha256(samples.tobytes(order="A")).hexdigest()
    proof = {
        "source_array_immutable": source_before == source_after,
        "original_matches_input": _pcm_sha256(raw_original) == input_sha,
        "outside_scope_proposed_bit_identical": np.array_equal(
            raw_proposed[before], raw_original[before]
        )
        and np.array_equal(raw_proposed[after], raw_original[after]),
        "outside_scope_removed_zero": not bool(np.any(raw_removed[before]))
        and not bool(np.any(raw_removed[after])),
        "raw_algebra_float64_bounded": reconstruction_error <= algebra_tolerance,
        "edge_residue_starts_and_ends_at_zero": not bool(np.any(raw_removed[scope.start_sample]))
        and not bool(np.any(raw_removed[scope.end_sample_exclusive - 1])),
        "proposed_does_not_clip": not bool(np.any(np.abs(raw_proposed) > 1.0)),
        "audition_gains_do_not_clip": proposed_monitor_peak <= 1.0 and residue_monitor_peak <= 1.0,
    }
    if any(value is not True for value in proof.values()):
        raise ProjectValidationError("Hiss preview could not prove immutable bounded algebra.")

    algorithm = dict(recipe.algorithm)
    input_identity = {
        **recipe.input_identity,
        "normalized_pcm_sha256": input_sha,
    }
    references_sha = canonical_json_sha256([item.to_dict() for item in recipe.noise_references])
    noise_estimate = {
        "method": "median_reference_only_power_spectrum/1",
        "noise_psd_sha256": noise_psd_sha,
        "reference_frame_count": reference_frame_count,
        "references_sha256": references_sha,
    }
    raw_arrays = {
        "original_sha256": _pcm_sha256(raw_original),
        "proposed_sha256": _pcm_sha256(raw_proposed),
        "removed_sha256": _pcm_sha256(raw_removed),
        "algebra": "original = proposed + removed",
        "maximum_reconstruction_error": _quantize(reconstruction_error),
    }
    render_body: dict[str, Any] = {
        "schema": HISS_PREVIEW_RENDER_SCHEMA,
        "recipe_body_sha256": recipe.recipe_body_sha256,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "algorithm": algorithm,
        "input": input_identity,
        "selected_scope": scope.to_dict(),
        "noise_estimate": noise_estimate,
        "raw_arrays": raw_arrays,
        "audition": audition,
        "policy": _render_policy(),
    }
    render_manifest = dict(render_body)
    render_manifest["render_body_sha256"] = canonical_json_sha256(render_body)
    render_manifest = validate_hiss_preview_render_manifest(
        render_manifest,
        recipe=recipe,
    )
    receipt_body: dict[str, Any] = {
        "schema": HISS_PREVIEW_RECEIPT_SCHEMA,
        "render_body_sha256": render_manifest["render_body_sha256"],
        "recipe_body_sha256": recipe.recipe_body_sha256,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "algorithm": algorithm,
        "input": input_identity,
        "selected_scope": scope.to_dict(),
        "noise_estimate": noise_estimate,
        "raw_arrays": raw_arrays,
        "audition": audition,
        "channel_metrics": metrics,
        "aggregate": _aggregate(metrics),
        "proof": proof,
        "policy": _receipt_policy(),
    }
    receipt = dict(receipt_body)
    receipt["receipt_body_sha256"] = canonical_json_sha256(receipt_body)
    original_output = raw_original[:, 0].copy() if was_mono else raw_original.copy()
    proposed_output = raw_proposed[:, 0].copy() if was_mono else raw_proposed.copy()
    removed_output = raw_removed[:, 0].copy() if was_mono else raw_removed.copy()
    receipt = validate_hiss_preview_receipt(
        receipt,
        recipe=recipe,
        render_manifest=render_manifest,
        arrays=(original_output, proposed_output, removed_output),
    )
    original_output.setflags(write=False)
    proposed_output.setflags(write=False)
    removed_output.setflags(write=False)
    return HissPreviewResult(
        original=original_output,
        proposed=proposed_output,
        removed=removed_output,
        render_manifest=render_manifest,
        receipt=receipt,
    )
