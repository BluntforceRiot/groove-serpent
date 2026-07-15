"""Deterministic, owner-audition-only preview of proposed stationary rumble.

The renderer applies one conservative zero-phase subsonic high-pass response
only inside the exact scope of a current, strictly validated continuous-noise
proposal.  It never edits a project, overwrites source PCM, publishes audio,
or claims that the result is approved or free of audible impact.

A caller attestation is required to create a recipe.  That attestation proves
only that the caller made the exact preview request; it is not evidence that a
human listened.  Original, Proposed, and Removed are returned as immutable raw
float64 arrays.  Matched audition gain and the deliberately louder residue
monitor gain are declared separately and are bounded against clipping.

This array foundation materializes the complete supplied capture.  It is not
a streaming decoder or a production export path.
"""

from __future__ import annotations

import hashlib
import math
import platform
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

import numpy as np

import groove_serpent.continuous_noise as continuous_noise_module
from .continuous_noise import (
    CONTINUOUS_NOISE_ALGORITHM_ID,
    CONTINUOUS_NOISE_DOCUMENT_SCHEMA,
    ContinuousNoiseProposalDocument,
    NoiseAnalysisScope,
    NoiseReferenceRegion,
)
from .errors import ProjectValidationError
from .publication import canonical_json_sha256
from .validation import strict_finite_number

RUMBLE_REVIEW_ATTESTATION_SCHEMA = (
    "groove-serpent.rumble-preview-review-attestation/1"
)
RUMBLE_PREVIEW_RECIPE_SCHEMA = "groove-serpent.rumble-preview-recipe/1"
RUMBLE_PREVIEW_RENDER_SCHEMA = "groove-serpent.rumble-preview-render/1"
RUMBLE_PREVIEW_RECEIPT_SCHEMA = "groove-serpent.rumble-preview-receipt/1"
RUMBLE_PREVIEW_ALGORITHM_ID = "groove-serpent.stationary-rumble-preview/1"
RUMBLE_PREVIEW_MODULE_ID = "groove_serpent.rumble_preview"
REVIEW_DECISION = "request_owner_audition_preview"
REVIEW_ACKNOWLEDGEMENT = (
    "caller_attestation_is_not_proof_of_human_audition_or_restoration_approval"
)
FILTER_METHOD = "reflected_scope_zero_phase_butterworth_magnitude/1"

_DIGEST_CHARACTERS = frozenset("0123456789abcdef")
_EPSILON = np.finfo(np.float64).tiny
_MAX_CHANNELS = 32


def _object(value: Any, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProjectValidationError(f"{label} must be a JSON object.")
    return value


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
        raise ProjectValidationError(
            f"{label} must be bounded, trimmed, nonempty printable text."
        )
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


def _module_sha256(module_path: str | None) -> str:
    if not module_path:
        raise ProjectValidationError("Required rumble module has no filesystem identity.")
    try:
        return hashlib.sha256(Path(module_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ProjectValidationError("Required rumble module identity could not be read.") from exc


def _current_analysis_module_sha256() -> str:
    return _module_sha256(continuous_noise_module.__file__)


def _current_preview_module_sha256() -> str:
    return _module_sha256(__file__)


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
        raise ProjectValidationError("Rumble preview PCM must be a NumPy array.")
    if samples.ndim not in (1, 2):
        raise ProjectValidationError("Rumble preview PCM must have one or two dimensions.")
    if samples.dtype.kind != "f":
        raise ProjectValidationError("Rumble preview PCM must use a floating-point dtype.")
    if samples.shape[0] < 1:
        raise ProjectValidationError("Rumble preview PCM must contain at least one frame.")
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    if not 1 <= channels <= _MAX_CHANNELS:
        raise ProjectValidationError(
            f"Rumble preview PCM must contain between 1 and {_MAX_CHANNELS} channels."
        )
    if not bool(np.all(np.isfinite(samples))):
        raise ProjectValidationError("Rumble preview PCM must contain only finite values.")
    if bool(np.any(np.abs(samples) > 1.0)):
        raise ProjectValidationError("Rumble preview PCM must be normalized to [-1, 1].")
    framed = samples[:, np.newaxis] if samples.ndim == 1 else samples
    return np.ascontiguousarray(framed, dtype="<f8"), samples.ndim == 1


def _recipe_policy() -> dict[str, Any]:
    return {
        "attestation_is_not_human_audition_proof": True,
        "automatic_application_forbidden": True,
        "automatic_publication_forbidden": True,
        "hum_rendering_included": False,
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
    }


@dataclass(frozen=True, slots=True)
class RumblePreviewConfig:
    """Conservative v1 subsonic response, scope-edge, and audition bounds."""

    cutoff_hz: float = 12.0
    filter_order: int = 2
    reflection_padding_ms: int = 2_000
    edge_fade_ms: int = 250
    maximum_removed_rms_dbfs: float = -28.0
    maximum_removed_peak: float = 0.02
    maximum_removed_energy_ratio: float = 0.02
    minimum_reference_low_band_reduction_db: float = 0.20
    maximum_reference_comparison_change_db: float = 0.25
    minimum_removed_energy_below_comparison_fraction: float = 0.85
    maximum_channel_removed_rms_spread_db: float = 6.0
    maximum_theoretical_comparison_loss_db: float = 0.25
    minimum_loudness_windows: int = 2
    loudness_window_floor_dbfs: float = -55.0
    maximum_match_gain_db: float = 0.25
    maximum_match_mad_db: float = 0.10
    original_audition_gain: float = 1.0
    residue_monitor_gain: float = 8.0

    def validate(self) -> None:
        _number(self.cutoff_hz, "Rumble cutoff", 5.0, 16.0)
        if self.filter_order != 2:
            raise ProjectValidationError("Rumble preview v1 filter order must remain exactly 2.")
        _integer(self.reflection_padding_ms, "Rumble reflection padding", 500, 5_000)
        _integer(self.edge_fade_ms, "Rumble edge fade", 50, 1_000)
        _number(self.maximum_removed_rms_dbfs, "Maximum removed RMS", -100.0, -6.0)
        _number(self.maximum_removed_peak, "Maximum removed peak", 0.000001, 0.10)
        _number(
            self.maximum_removed_energy_ratio,
            "Maximum removed energy ratio",
            0.000001,
            0.10,
        )
        _number(
            self.minimum_reference_low_band_reduction_db,
            "Minimum reference low-band reduction",
            0.01,
            12.0,
        )
        _number(
            self.maximum_reference_comparison_change_db,
            "Maximum reference comparison-band change",
            0.001,
            1.0,
        )
        _number(
            self.minimum_removed_energy_below_comparison_fraction,
            "Minimum removed low-frequency fraction",
            0.50,
            1.0,
        )
        _number(
            self.maximum_channel_removed_rms_spread_db,
            "Maximum channel removed-RMS spread",
            0.01,
            20.0,
        )
        _number(
            self.maximum_theoretical_comparison_loss_db,
            "Maximum theoretical comparison-band loss",
            0.001,
            1.0,
        )
        _integer(self.minimum_loudness_windows, "Minimum loudness windows", 2, 1_000)
        _number(self.loudness_window_floor_dbfs, "Loudness window floor", -120.0, -20.0)
        _number(self.maximum_match_gain_db, "Maximum matching gain", 0.001, 1.0)
        _number(self.maximum_match_mad_db, "Maximum matching MAD", 0.0, 0.5)
        if self.original_audition_gain != 1.0:
            raise ProjectValidationError("Original audition gain must remain exactly 1.0.")
        _number(self.residue_monitor_gain, "Residue monitor gain", 1.0, 32.0)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> RumblePreviewConfig:
        data = _object(value, "Rumble preview configuration")
        _strict_keys(data, set(cls.__dataclass_fields__), "Rumble preview configuration")
        result = cls(**data)
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class RumbleReviewAttestation:
    """Exact caller request, deliberately not proof of completed listening."""

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
    def from_dict(cls, value: Any, sample_count: int) -> RumbleReviewAttestation:
        data = _object(value, "Rumble preview review attestation")
        _strict_keys(data, set(cls.__dataclass_fields__), "Rumble preview review attestation")
        if data["schema"] != RUMBLE_REVIEW_ATTESTATION_SCHEMA:
            raise ProjectValidationError("Rumble review attestation schema is unsupported.")
        token = _digest(data["attestation_token"], "Rumble review attestation token")
        if len(set(token)) == 1:
            raise ProjectValidationError(
                "Rumble review attestation token is structurally non-distinct."
            )
        if data["decision"] != REVIEW_DECISION:
            raise ProjectValidationError("Rumble review attestation decision is unsupported.")
        if data["acknowledgement"] != REVIEW_ACKNOWLEDGEMENT:
            raise ProjectValidationError(
                "Rumble review attestation must acknowledge its limited authority."
            )
        return cls(
            schema=RUMBLE_REVIEW_ATTESTATION_SCHEMA,
            attestation_token=token,
            decision=REVIEW_DECISION,
            proposal_body_sha256=_digest(
                data["proposal_body_sha256"], "Attested proposal body SHA-256"
            ),
            selected_scope=NoiseAnalysisScope.from_dict(
                data["selected_scope"], sample_count
            ),
            acknowledgement=REVIEW_ACKNOWLEDGEMENT,
        )


@dataclass(frozen=True, slots=True)
class RumblePreviewRecipe:
    schema: str
    recipe_body_sha256: str
    proposal_identity: dict[str, str]
    input_identity: dict[str, Any]
    selected_scope: NoiseAnalysisScope
    noise_references: tuple[NoiseReferenceRegion, ...]
    observed_lower_hz: float
    observed_upper_hz: float
    comparison_lower_hz: float
    comparison_upper_hz: float
    analysis_window_ms: int
    review_attestation: RumbleReviewAttestation
    algorithm: dict[str, str]
    config: RumblePreviewConfig
    policy: dict[str, Any]

    def body_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "proposal_identity": dict(self.proposal_identity),
            "input_identity": dict(self.input_identity),
            "selected_scope": self.selected_scope.to_dict(),
            "noise_references": [item.to_dict() for item in self.noise_references],
            "observed_lower_hz": self.observed_lower_hz,
            "observed_upper_hz": self.observed_upper_hz,
            "comparison_lower_hz": self.comparison_lower_hz,
            "comparison_upper_hz": self.comparison_upper_hz,
            "analysis_window_ms": self.analysis_window_ms,
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
    def from_dict(cls, value: Any) -> RumblePreviewRecipe:
        data = _object(value, "Rumble preview recipe")
        _strict_keys(data, set(cls.__dataclass_fields__), "Rumble preview recipe")
        if data["schema"] != RUMBLE_PREVIEW_RECIPE_SCHEMA:
            raise ProjectValidationError("Rumble preview recipe schema is unsupported.")
        body_sha = _digest(data["recipe_body_sha256"], "Rumble recipe body SHA-256")
        body = dict(data)
        del body["recipe_body_sha256"]
        if canonical_json_sha256(body) != body_sha:
            raise ProjectValidationError("Rumble preview recipe body identity is stale.")
        input_identity = _object(data["input_identity"], "Rumble recipe input identity")
        _strict_keys(
            input_identity,
            {"sample_rate", "sample_count", "channel_count"},
            "Rumble recipe input identity",
        )
        sample_rate = _integer(
            input_identity["sample_rate"], "Rumble recipe sample rate", 8_000, 768_000
        )
        sample_count = _integer(
            input_identity["sample_count"], "Rumble recipe sample count", 1, 2**63 - 1
        )
        channel_count = _integer(
            input_identity["channel_count"], "Rumble recipe channel count", 1, _MAX_CHANNELS
        )
        scope = NoiseAnalysisScope.from_dict(data["selected_scope"], sample_count)
        references = tuple(
            NoiseReferenceRegion.from_dict(item, scope)
            for item in _array(data["noise_references"], "Rumble recipe noise references")
        )
        _validate_reference_geometry(scope, references)
        proposal_identity = _object(
            data["proposal_identity"], "Rumble recipe proposal identity"
        )
        expected_proposal_keys = {
            "analysis_algorithm_id",
            "analysis_config_sha256",
            "analysis_module_sha256",
            "analysis_numpy_version",
            "noise_references_sha256",
            "normalized_pcm_sha256",
            "proposal_body_sha256",
            "rumble_body_sha256",
            "scope_sha256",
        }
        _strict_keys(
            proposal_identity,
            expected_proposal_keys,
            "Rumble recipe proposal identity",
        )
        if proposal_identity["analysis_algorithm_id"] != CONTINUOUS_NOISE_ALGORITHM_ID:
            raise ProjectValidationError("Rumble recipe analysis algorithm is unsupported.")
        for key in expected_proposal_keys - {
            "analysis_algorithm_id",
            "analysis_numpy_version",
        }:
            _digest(proposal_identity[key], f"Rumble recipe {key}")
        _text(
            proposal_identity["analysis_numpy_version"],
            "Rumble recipe analysis NumPy version",
            maximum=64,
        )
        if proposal_identity["scope_sha256"] != canonical_json_sha256(scope.to_dict()):
            raise ProjectValidationError("Rumble recipe scope identity is inconsistent.")
        if proposal_identity["noise_references_sha256"] != canonical_json_sha256(
            [item.to_dict() for item in references]
        ):
            raise ProjectValidationError(
                "Rumble recipe noise-reference identity is inconsistent."
            )
        lower = _number(data["observed_lower_hz"], "Observed rumble lower frequency", 1.0, 40.0)
        upper = _number(data["observed_upper_hz"], "Observed rumble upper frequency", 1.0, 80.0)
        comparison_lower = _number(
            data["comparison_lower_hz"], "Rumble comparison lower frequency", 20.0, 200.0
        )
        comparison_upper = _number(
            data["comparison_upper_hz"], "Rumble comparison upper frequency", 30.0, 500.0
        )
        if not lower < upper < comparison_lower < comparison_upper:
            raise ProjectValidationError("Rumble recipe frequency bands are inconsistent.")
        analysis_window_ms = _integer(
            data["analysis_window_ms"],
            "Rumble recipe analysis window",
            250,
            10_000,
        )
        review = RumbleReviewAttestation.from_dict(
            data["review_attestation"], sample_count
        )
        if review.proposal_body_sha256 != proposal_identity["proposal_body_sha256"]:
            raise ProjectValidationError("Rumble recipe review attests a different proposal.")
        if review.selected_scope != scope:
            raise ProjectValidationError("Rumble recipe review attests a different scope.")
        config = RumblePreviewConfig.from_dict(data["config"])
        if not lower <= config.cutoff_hz <= upper:
            raise ProjectValidationError(
                "Rumble cutoff must remain inside the observed rumble band."
            )
        comparison_loss = -_response_db(
            comparison_lower,
            config.cutoff_hz,
            config.filter_order,
        )
        if comparison_loss > config.maximum_theoretical_comparison_loss_db:
            raise ProjectValidationError(
                "Rumble recipe exceeds the comparison-band loss cap."
            )
        padding_samples = round(
            sample_rate * config.reflection_padding_ms / 1_000
        )
        fade_samples = round(sample_rate * config.edge_fade_ms / 1_000)
        scope_length = scope.end_sample_exclusive - scope.start_sample
        if scope_length <= 2 * (padding_samples + fade_samples):
            raise ProjectValidationError(
                "Rumble recipe scope is too short for its edge treatment."
            )
        algorithm = _object(data["algorithm"], "Rumble preview algorithm identity")
        algorithm_keys = {
            "config_sha256",
            "id",
            "module",
            "module_sha256",
            "numpy_version",
            "python_implementation",
            "python_version",
        }
        _strict_keys(algorithm, algorithm_keys, "Rumble preview algorithm identity")
        if algorithm["id"] != RUMBLE_PREVIEW_ALGORITHM_ID:
            raise ProjectValidationError("Rumble preview algorithm is unsupported.")
        if algorithm["module"] != RUMBLE_PREVIEW_MODULE_ID:
            raise ProjectValidationError("Rumble preview module is unsupported.")
        _digest(algorithm["module_sha256"], "Rumble preview module SHA-256")
        _digest(algorithm["config_sha256"], "Rumble preview config SHA-256")
        if algorithm["config_sha256"] != canonical_json_sha256(config.to_dict()):
            raise ProjectValidationError("Rumble preview config identity is inconsistent.")
        for key in ("numpy_version", "python_implementation", "python_version"):
            _text(algorithm[key], f"Rumble preview {key}", maximum=64)
        policy = _object(data["policy"], "Rumble preview recipe policy")
        expected_policy = _recipe_policy()
        _strict_keys(policy, set(expected_policy), "Rumble preview recipe policy")
        if policy != expected_policy:
            raise ProjectValidationError("Rumble preview recipe protections are mandatory.")
        return cls(
            schema=RUMBLE_PREVIEW_RECIPE_SCHEMA,
            recipe_body_sha256=body_sha,
            proposal_identity={
                key: cast(str, proposal_identity[key]) for key in proposal_identity
            },
            input_identity={
                "sample_rate": sample_rate,
                "sample_count": sample_count,
                "channel_count": channel_count,
            },
            selected_scope=scope,
            noise_references=references,
            observed_lower_hz=lower,
            observed_upper_hz=upper,
            comparison_lower_hz=comparison_lower,
            comparison_upper_hz=comparison_upper,
            analysis_window_ms=analysis_window_ms,
            review_attestation=review,
            algorithm={key: cast(str, algorithm[key]) for key in algorithm},
            config=config,
            policy=expected_policy,
        )


@dataclass(frozen=True, slots=True)
class RumblePreviewResult:
    original: np.ndarray
    proposed: np.ndarray
    removed: np.ndarray
    render_manifest: dict[str, Any]
    receipt: dict[str, Any]


def _validate_reference_geometry(
    scope: NoiseAnalysisScope,
    references: Sequence[NoiseReferenceRegion],
) -> None:
    if not 2 <= len(references) <= 64:
        raise ProjectValidationError(
            "Rumble preview requires between 2 and 64 noise references."
        )
    previous_end = scope.start_sample
    labels: set[str] = set()
    for item in references:
        item.validate(scope)
        key = item.label.casefold()
        if key in labels or key == "program":
            raise ProjectValidationError("Rumble preview noise-reference labels are invalid.")
        labels.add(key)
        if item.start_sample < previous_end:
            raise ProjectValidationError(
                "Rumble preview noise references overlap or are unordered."
            )
        previous_end = item.end_sample_exclusive
    if sum(item.end_sample_exclusive - item.start_sample for item in references) >= (
        scope.end_sample_exclusive - scope.start_sample
    ):
        raise ProjectValidationError("Rumble preview references leave no program interval.")


def _strict_current_proposal(
    value: ContinuousNoiseProposalDocument | Mapping[str, Any],
) -> ContinuousNoiseProposalDocument:
    raw = value.to_dict() if isinstance(value, ContinuousNoiseProposalDocument) else dict(value)
    proposal = ContinuousNoiseProposalDocument.from_dict(raw)
    if proposal.schema != CONTINUOUS_NOISE_DOCUMENT_SCHEMA:
        raise ProjectValidationError("Rumble preview requires a v1 continuous-noise proposal.")
    if proposal.algorithm["module_sha256"] != _current_analysis_module_sha256():
        raise ProjectValidationError("Rumble preview proposal analysis module is stale.")
    if proposal.algorithm["numpy_version"] != np.__version__:
        raise ProjectValidationError("Rumble preview proposal NumPy identity is stale.")
    if proposal.rumble.status != "proposed":
        raise ProjectValidationError("Rumble preview cannot render an abstained proposal.")
    if (
        proposal.rumble.observed_lower_hz is None
        or proposal.rumble.observed_upper_hz is None
    ):
        raise ProjectValidationError("Rumble preview proposal has no supported observed band.")
    return proposal


def _response_gain(frequency_hz: float, cutoff_hz: float, order: int) -> float:
    if frequency_hz <= 0.0:
        return 0.0
    ratio = frequency_hz / cutoff_hz
    powered = ratio ** (2 * order)
    return powered / (1.0 + powered)


def _response_db(frequency_hz: float, cutoff_hz: float, order: int) -> float:
    gain = _response_gain(frequency_hz, cutoff_hz, order)
    return max(-400.0, 20.0 * math.log10(max(gain, _EPSILON)))


def create_rumble_preview_recipe(
    proposal_value: ContinuousNoiseProposalDocument | Mapping[str, Any],
    review_attestation_value: RumbleReviewAttestation | Mapping[str, Any],
    *,
    config: RumblePreviewConfig | None = None,
) -> RumblePreviewRecipe:
    """Create one exact, non-authoritative preview recipe or fail closed."""

    proposal = _strict_current_proposal(proposal_value)
    raw_review = (
        review_attestation_value.to_dict()
        if isinstance(review_attestation_value, RumbleReviewAttestation)
        else dict(review_attestation_value)
    )
    review = RumbleReviewAttestation.from_dict(raw_review, proposal.sample_count)
    if review.proposal_body_sha256 != proposal.proposal_body_sha256:
        raise ProjectValidationError("Rumble review attestation is stale for this proposal.")
    if review.selected_scope != proposal.scope:
        raise ProjectValidationError(
            "Rumble preview v1 requires the exactly reviewed proposal scope."
        )
    settings = config or RumblePreviewConfig()
    settings.validate()
    lower = proposal.rumble.observed_lower_hz
    upper = proposal.rumble.observed_upper_hz
    if lower is None or upper is None:
        raise ProjectValidationError("Rumble preview proposal has no observed band.")
    if not lower <= settings.cutoff_hz <= upper:
        raise ProjectValidationError("Rumble cutoff is outside the observed rumble band.")
    comparison_loss = -_response_db(
        proposal.config.rumble_comparison_lower_hz,
        settings.cutoff_hz,
        settings.filter_order,
    )
    if comparison_loss > settings.maximum_theoretical_comparison_loss_db:
        raise ProjectValidationError(
            "Rumble high-pass response exceeds the comparison-band loss cap."
        )
    scope_length = proposal.scope.end_sample_exclusive - proposal.scope.start_sample
    padding_samples = round(proposal.sample_rate * settings.reflection_padding_ms / 1_000)
    fade_samples = round(proposal.sample_rate * settings.edge_fade_ms / 1_000)
    if scope_length <= 2 * (padding_samples + fade_samples):
        raise ProjectValidationError(
            "Rumble preview scope is too short for declared reflection and edge treatment."
        )
    config_dict = settings.to_dict()
    runtime = _runtime_identity()
    references_body = [item.to_dict() for item in proposal.noise_references]
    proposal_identity = {
        "analysis_algorithm_id": proposal.algorithm["id"],
        "analysis_config_sha256": proposal.algorithm["config_sha256"],
        "analysis_module_sha256": proposal.algorithm["module_sha256"],
        "analysis_numpy_version": proposal.algorithm["numpy_version"],
        "noise_references_sha256": canonical_json_sha256(references_body),
        "normalized_pcm_sha256": proposal.normalized_pcm_sha256,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "rumble_body_sha256": canonical_json_sha256(proposal.rumble.to_dict()),
        "scope_sha256": canonical_json_sha256(proposal.scope.to_dict()),
    }
    recipe = RumblePreviewRecipe(
        schema=RUMBLE_PREVIEW_RECIPE_SCHEMA,
        recipe_body_sha256="",
        proposal_identity=proposal_identity,
        input_identity={
            "sample_rate": proposal.sample_rate,
            "sample_count": proposal.sample_count,
            "channel_count": proposal.channel_count,
        },
        selected_scope=proposal.scope,
        noise_references=proposal.noise_references,
        observed_lower_hz=lower,
        observed_upper_hz=upper,
        comparison_lower_hz=proposal.config.rumble_comparison_lower_hz,
        comparison_upper_hz=proposal.config.rumble_comparison_upper_hz,
        analysis_window_ms=proposal.config.window_ms,
        review_attestation=review,
        algorithm={
            "config_sha256": canonical_json_sha256(config_dict),
            "id": RUMBLE_PREVIEW_ALGORITHM_ID,
            "module": RUMBLE_PREVIEW_MODULE_ID,
            "module_sha256": _current_preview_module_sha256(),
            **runtime,
        },
        config=settings,
        policy=_recipe_policy(),
    )
    recipe = replace(
        recipe,
        recipe_body_sha256=canonical_json_sha256(recipe.body_dict()),
    )
    return RumblePreviewRecipe.from_dict(recipe.to_dict())


def _program_ranges(recipe: RumblePreviewRecipe) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    cursor = recipe.selected_scope.start_sample
    for reference in recipe.noise_references:
        if cursor < reference.start_sample:
            ranges.append((cursor, reference.start_sample))
        cursor = reference.end_sample_exclusive
    if cursor < recipe.selected_scope.end_sample_exclusive:
        ranges.append((cursor, recipe.selected_scope.end_sample_exclusive))
    return tuple(ranges)


def _window_starts(start: int, end: int, length: int) -> tuple[int, ...]:
    return tuple(range(start, end - length + 1, length))


def _edge_envelope(length: int, fade_samples: int) -> np.ndarray:
    if fade_samples < 2 or length <= fade_samples * 2:
        raise ProjectValidationError("Rumble preview scope is too short for edge treatment.")
    envelope = np.ones(length, dtype=np.float64)
    fade = 0.5 - 0.5 * np.cos(
        np.linspace(0.0, np.pi, fade_samples, endpoint=True, dtype=np.float64)
    )
    envelope[:fade_samples] = fade
    envelope[-fade_samples:] = fade[::-1]
    return envelope


def _high_pass_scope(
    scoped: np.ndarray,
    *,
    sample_rate: int,
    cutoff_hz: float,
    order: int,
    padding_samples: int,
) -> np.ndarray:
    if scoped.shape[0] <= padding_samples * 2:
        raise ProjectValidationError("Rumble preview scope is too short for reflection padding.")
    padded = np.pad(
        scoped,
        ((padding_samples, padding_samples), (0, 0)),
        mode="reflect",
    )
    frequencies = np.fft.rfftfreq(padded.shape[0], d=1.0 / sample_rate)
    ratio = frequencies / cutoff_hz
    powered = np.power(ratio, 2 * order)
    response = powered / (1.0 + powered)
    response[0] = 0.0
    transformed = np.fft.rfft(padded, axis=0)
    filtered = np.fft.irfft(
        transformed * response[:, np.newaxis],
        n=padded.shape[0],
        axis=0,
    )
    result = filtered[padding_samples:-padding_samples]
    if result.shape != scoped.shape or not bool(np.all(np.isfinite(result))):
        raise ProjectValidationError("Rumble high-pass produced invalid PCM geometry.")
    return np.ascontiguousarray(result, dtype=np.float64)


def _db_rms(values: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(values, dtype=np.float64))))
    return max(-400.0, 20.0 * math.log10(max(rms, _EPSILON)))


def _band_power(
    values: np.ndarray,
    *,
    sample_rate: int,
    lower_hz: float,
    upper_hz: float,
) -> float:
    taper = np.hanning(values.shape[0]).astype(np.float64)
    frequencies = np.fft.rfftfreq(values.shape[0], d=1.0 / sample_rate)
    mask = (frequencies >= lower_hz) & (frequencies <= upper_hz)
    transformed = np.fft.rfft(values * taper)
    return float(np.sum(np.square(np.abs(transformed[mask]), dtype=np.float64)))


def _reference_band_powers(
    values: np.ndarray,
    recipe: RumblePreviewRecipe,
    channel: int,
    lower_hz: float,
    upper_hz: float,
) -> float:
    window_samples = round(
        int(recipe.input_identity["sample_rate"])
        * _analysis_window_ms(recipe)
        / 1_000
    )
    result = 0.0
    count = 0
    for reference in recipe.noise_references:
        for start in _window_starts(
            reference.start_sample,
            reference.end_sample_exclusive,
            window_samples,
        ):
            result += _band_power(
                values[start : start + window_samples, channel],
                sample_rate=int(recipe.input_identity["sample_rate"]),
                lower_hz=lower_hz,
                upper_hz=upper_hz,
            )
            count += 1
    if count < 2:
        raise ProjectValidationError("Rumble preview has insufficient reference metric windows.")
    return result


def _analysis_window_ms(recipe: RumblePreviewRecipe) -> int:
    return recipe.analysis_window_ms


def _channel_metrics(
    original: np.ndarray,
    proposed: np.ndarray,
    removed: np.ndarray,
    recipe: RumblePreviewRecipe,
) -> list[dict[str, Any]]:
    scope = recipe.selected_scope
    selected = slice(scope.start_sample, scope.end_sample_exclusive)
    metrics: list[dict[str, Any]] = []
    sample_rate = int(recipe.input_identity["sample_rate"])
    for channel in range(original.shape[1]):
        original_scope = original[selected, channel]
        removed_scope = removed[selected, channel]
        original_energy = float(np.sum(np.square(original_scope, dtype=np.float64)))
        removed_energy = float(np.sum(np.square(removed_scope, dtype=np.float64)))
        original_low = _reference_band_powers(
            original,
            recipe,
            channel,
            recipe.observed_lower_hz,
            recipe.observed_upper_hz,
        )
        proposed_low = _reference_band_powers(
            proposed,
            recipe,
            channel,
            recipe.observed_lower_hz,
            recipe.observed_upper_hz,
        )
        original_comparison = _reference_band_powers(
            original,
            recipe,
            channel,
            recipe.comparison_lower_hz,
            recipe.comparison_upper_hz,
        )
        proposed_comparison = _reference_band_powers(
            proposed,
            recipe,
            channel,
            recipe.comparison_lower_hz,
            recipe.comparison_upper_hz,
        )
        full_taper = np.hanning(removed_scope.shape[0]).astype(np.float64)
        frequencies = np.fft.rfftfreq(removed_scope.shape[0], d=1.0 / sample_rate)
        removed_spectrum = np.square(
            np.abs(np.fft.rfft(removed_scope * full_taper)), dtype=np.float64
        )
        below = float(np.sum(removed_spectrum[frequencies < recipe.comparison_lower_hz]))
        total = float(np.sum(removed_spectrum))
        metrics.append(
            {
                "channel_index": channel,
                "removed_rms_dbfs": _quantize(_db_rms(removed_scope)),
                "removed_peak": _quantize(float(np.max(np.abs(removed_scope)))),
                "removed_energy_ratio": _quantize(
                    removed_energy / max(original_energy, _EPSILON)
                ),
                "reference_low_band_reduction_db": _quantize(
                    10.0
                    * math.log10(
                        max(original_low, _EPSILON) / max(proposed_low, _EPSILON)
                    )
                ),
                "reference_comparison_band_change_db": _quantize(
                    10.0
                    * math.log10(
                        max(proposed_comparison, _EPSILON)
                        / max(original_comparison, _EPSILON)
                    )
                ),
                "removed_energy_below_comparison_fraction": _quantize(
                    below / max(total, _EPSILON)
                ),
            }
        )
    return metrics


def _aggregate(metrics: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rms_values = [float(item["removed_rms_dbfs"]) for item in metrics]
    return {
        "maximum_removed_peak": max(float(item["removed_peak"]) for item in metrics),
        "maximum_removed_energy_ratio": max(
            float(item["removed_energy_ratio"]) for item in metrics
        ),
        "minimum_reference_low_band_reduction_db": min(
            float(item["reference_low_band_reduction_db"]) for item in metrics
        ),
        "maximum_absolute_reference_comparison_change_db": max(
            abs(float(item["reference_comparison_band_change_db"]))
            for item in metrics
        ),
        "minimum_removed_energy_below_comparison_fraction": min(
            float(item["removed_energy_below_comparison_fraction"])
            for item in metrics
        ),
        "channel_removed_rms_spread_db": max(rms_values) - min(rms_values),
    }


def _validate_metrics(
    metrics: Sequence[Mapping[str, Any]],
    recipe: RumblePreviewRecipe,
) -> None:
    if len(metrics) != int(recipe.input_identity["channel_count"]):
        raise ProjectValidationError("Rumble preview metrics do not cover every channel.")
    for channel, metric in enumerate(metrics):
        if metric["channel_index"] != channel:
            raise ProjectValidationError("Rumble preview channel metrics are unordered.")
        if float(metric["removed_rms_dbfs"]) > recipe.config.maximum_removed_rms_dbfs:
            raise ProjectValidationError("Rumble preview removed RMS exceeds its safety bound.")
        if float(metric["removed_peak"]) > recipe.config.maximum_removed_peak:
            raise ProjectValidationError("Rumble preview removed peak exceeds its safety bound.")
        if (
            float(metric["removed_energy_ratio"])
            > recipe.config.maximum_removed_energy_ratio
        ):
            raise ProjectValidationError("Rumble preview removed energy exceeds its safety bound.")
        if (
            float(metric["reference_low_band_reduction_db"])
            < recipe.config.minimum_reference_low_band_reduction_db
        ):
            raise ProjectValidationError(
                "Rumble preview does not reduce enough stationary low-band evidence."
            )
        if (
            abs(float(metric["reference_comparison_band_change_db"]))
            > recipe.config.maximum_reference_comparison_change_db
        ):
            raise ProjectValidationError(
                "Rumble preview changes too much comparison-band evidence."
            )
        if (
            float(metric["removed_energy_below_comparison_fraction"])
            < recipe.config.minimum_removed_energy_below_comparison_fraction
        ):
            raise ProjectValidationError(
                "Rumble preview residue is not sufficiently confined below the comparison band."
            )
    aggregate = _aggregate(metrics)
    if (
        float(aggregate["channel_removed_rms_spread_db"])
        > recipe.config.maximum_channel_removed_rms_spread_db
    ):
        raise ProjectValidationError("Rumble preview removal disagrees across channels.")


def _audition_gains(
    original: np.ndarray,
    proposed: np.ndarray,
    recipe: RumblePreviewRecipe,
) -> dict[str, Any]:
    sample_rate = int(recipe.input_identity["sample_rate"])
    window_samples = round(sample_rate * _analysis_window_ms(recipe) / 1_000)
    ratios_db: list[float] = []
    for start, end in _program_ranges(recipe):
        for window_start in _window_starts(start, end, window_samples):
            before = original[window_start : window_start + window_samples]
            after = proposed[window_start : window_start + window_samples]
            if (
                _db_rms(before) >= recipe.config.loudness_window_floor_dbfs
                and _db_rms(after) >= recipe.config.loudness_window_floor_dbfs
            ):
                before_rms = float(np.sqrt(np.mean(np.square(before))))
                after_rms = float(np.sqrt(np.mean(np.square(after))))
                ratios_db.append(
                    20.0
                    * math.log10(
                        max(before_rms, _EPSILON) / max(after_rms, _EPSILON)
                    )
                )
    if len(ratios_db) < recipe.config.minimum_loudness_windows:
        raise ProjectValidationError(
            "Rumble preview cannot establish reliable matched audition loudness."
        )
    values = np.asarray(ratios_db, dtype=np.float64)
    median_db = float(np.median(values))
    mad_db = float(np.median(np.abs(values - median_db)))
    if (
        abs(median_db) > recipe.config.maximum_match_gain_db
        or mad_db > recipe.config.maximum_match_mad_db
    ):
        raise ProjectValidationError(
            "Rumble preview loudness match exceeds conservative gain or stability bounds."
        )
    return {
        "method": "median_program_window_rms_log_ratio/1",
        "window_count": len(ratios_db),
        "original_linear_gain": 1.0,
        "proposed_linear_gain": _quantize(10.0 ** (median_db / 20.0)),
        "proposed_gain_db": _quantize(median_db),
        "match_mad_db": _quantize(mad_db),
        "residue_monitor_linear_gain": _quantize(recipe.config.residue_monitor_gain),
        "raw_arrays_are_gain_neutral": True,
    }


def _filter_identity(recipe: RumblePreviewRecipe) -> dict[str, Any]:
    sample_rate = int(recipe.input_identity["sample_rate"])
    return {
        "method": FILTER_METHOD,
        "cutoff_hz": recipe.config.cutoff_hz,
        "order": recipe.config.filter_order,
        "reflection_padding_samples": round(
            sample_rate * recipe.config.reflection_padding_ms / 1_000
        ),
        "edge_fade_samples": round(sample_rate * recipe.config.edge_fade_ms / 1_000),
        "response_at_observed_lower_db": _quantize(
            _response_db(
                recipe.observed_lower_hz,
                recipe.config.cutoff_hz,
                recipe.config.filter_order,
            )
        ),
        "response_at_observed_upper_db": _quantize(
            _response_db(
                recipe.observed_upper_hz,
                recipe.config.cutoff_hz,
                recipe.config.filter_order,
            )
        ),
        "response_at_comparison_lower_db": _quantize(
            _response_db(
                recipe.comparison_lower_hz,
                recipe.config.cutoff_hz,
                recipe.config.filter_order,
            )
        ),
        "maximum_frequency_gain": 1.0,
        "attenuation_only": True,
    }


def _validate_algorithm(value: Any, label: str) -> dict[str, Any]:
    data = _object(value, label)
    keys = {
        "id",
        "module",
        "module_sha256",
        "numpy_version",
        "python_implementation",
        "python_version",
    }
    _strict_keys(data, keys, label)
    if data["id"] != RUMBLE_PREVIEW_ALGORITHM_ID or data["module"] != RUMBLE_PREVIEW_MODULE_ID:
        raise ProjectValidationError(f"{label} is unsupported.")
    _digest(data["module_sha256"], f"{label} module SHA-256")
    for key in ("numpy_version", "python_implementation", "python_version"):
        _text(data[key], f"{label} {key}", maximum=64)
    return data


def _validate_input_identity(value: Any, label: str) -> dict[str, Any]:
    data = _object(value, label)
    _strict_keys(
        data,
        {"sample_rate", "sample_count", "channel_count", "normalized_pcm_sha256"},
        label,
    )
    _integer(data["sample_rate"], f"{label} sample rate", 8_000, 768_000)
    _integer(data["sample_count"], f"{label} sample count", 1, 2**63 - 1)
    _integer(data["channel_count"], f"{label} channel count", 1, _MAX_CHANNELS)
    _digest(data["normalized_pcm_sha256"], f"{label} PCM SHA-256")
    return data


def _validate_raw_arrays(value: Any) -> dict[str, Any]:
    data = _object(value, "Rumble raw arrays")
    _strict_keys(
        data,
        {
            "original_sha256",
            "proposed_sha256",
            "removed_sha256",
            "algebra",
            "maximum_reconstruction_error",
        },
        "Rumble raw arrays",
    )
    for key in ("original_sha256", "proposed_sha256", "removed_sha256"):
        _digest(data[key], f"Rumble {key}")
    if data["algebra"] != "original = proposed + removed":
        raise ProjectValidationError("Rumble raw-array algebra is unsupported.")
    _number(
        data["maximum_reconstruction_error"],
        "Rumble reconstruction error",
        0.0,
        0.000001,
    )
    return data


def _validate_audition(value: Any) -> dict[str, Any]:
    data = _object(value, "Rumble audition gains")
    _strict_keys(
        data,
        {
            "method",
            "window_count",
            "original_linear_gain",
            "proposed_linear_gain",
            "proposed_gain_db",
            "match_mad_db",
            "residue_monitor_linear_gain",
            "raw_arrays_are_gain_neutral",
        },
        "Rumble audition gains",
    )
    if data["method"] != "median_program_window_rms_log_ratio/1":
        raise ProjectValidationError("Rumble audition matching method is unsupported.")
    _integer(data["window_count"], "Rumble audition windows", 2, 1_000_000)
    _number(data["original_linear_gain"], "Original audition gain", 0.01, 100.0)
    _number(data["proposed_linear_gain"], "Proposed audition gain", 0.01, 100.0)
    _number(data["proposed_gain_db"], "Proposed audition gain dB", -40.0, 40.0)
    _number(data["match_mad_db"], "Rumble audition match MAD", 0.0, 40.0)
    _number(data["residue_monitor_linear_gain"], "Residue monitor gain", 1.0, 32.0)
    if data["raw_arrays_are_gain_neutral"] is not True:
        raise ProjectValidationError("Rumble preview raw arrays must remain gain-neutral.")
    expected_gain = 10.0 ** (float(data["proposed_gain_db"]) / 20.0)
    if not math.isclose(
        float(data["proposed_linear_gain"]),
        expected_gain,
        rel_tol=1e-10,
        abs_tol=1e-12,
    ):
        raise ProjectValidationError("Rumble audition gain dB and linear value disagree.")
    return data


def _validate_filter(value: Any) -> dict[str, Any]:
    data = _object(value, "Rumble filter identity")
    _strict_keys(
        data,
        {
            "method",
            "cutoff_hz",
            "order",
            "reflection_padding_samples",
            "edge_fade_samples",
            "response_at_observed_lower_db",
            "response_at_observed_upper_db",
            "response_at_comparison_lower_db",
            "maximum_frequency_gain",
            "attenuation_only",
        },
        "Rumble filter identity",
    )
    if data["method"] != FILTER_METHOD:
        raise ProjectValidationError("Rumble filter method is unsupported.")
    _number(data["cutoff_hz"], "Rumble filter cutoff", 5.0, 16.0)
    if data["order"] != 2:
        raise ProjectValidationError("Rumble filter order is unsupported.")
    _integer(data["reflection_padding_samples"], "Rumble reflection samples", 1, 2**31)
    _integer(data["edge_fade_samples"], "Rumble edge fade samples", 2, 2**31)
    for key in (
        "response_at_observed_lower_db",
        "response_at_observed_upper_db",
        "response_at_comparison_lower_db",
    ):
        _number(data[key], f"Rumble {key}", -400.0, 0.0)
    if data["maximum_frequency_gain"] != 1.0 or data["attenuation_only"] is not True:
        raise ProjectValidationError("Rumble filter must be attenuation-only.")
    return data


def validate_rumble_preview_render_manifest(
    value: Any,
    *,
    recipe: RumblePreviewRecipe | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Strictly validate the hash-bound description of one preview render."""

    data = _object(value, "Rumble preview render manifest")
    _strict_keys(
        data,
        {
            "schema",
            "render_body_sha256",
            "recipe_body_sha256",
            "proposal_body_sha256",
            "algorithm",
            "input",
            "selected_scope",
            "filter",
            "raw_arrays",
            "audition",
            "policy",
        },
        "Rumble preview render manifest",
    )
    if data["schema"] != RUMBLE_PREVIEW_RENDER_SCHEMA:
        raise ProjectValidationError("Rumble preview render schema is unsupported.")
    root = _digest(data["render_body_sha256"], "Rumble render body SHA-256")
    body = dict(data)
    del body["render_body_sha256"]
    if canonical_json_sha256(body) != root:
        raise ProjectValidationError("Rumble preview render body identity is stale.")
    recipe_sha = _digest(data["recipe_body_sha256"], "Rumble render recipe SHA-256")
    proposal_sha = _digest(data["proposal_body_sha256"], "Rumble render proposal SHA-256")
    algorithm = _validate_algorithm(data["algorithm"], "Rumble render algorithm")
    input_identity = _validate_input_identity(data["input"], "Rumble render input")
    scope = NoiseAnalysisScope.from_dict(
        data["selected_scope"], int(input_identity["sample_count"])
    )
    filter_identity = _validate_filter(data["filter"])
    _validate_raw_arrays(data["raw_arrays"])
    audition = _validate_audition(data["audition"])
    policy = _object(data["policy"], "Rumble render policy")
    expected_policy = _render_policy()
    _strict_keys(policy, set(expected_policy), "Rumble render policy")
    if policy != expected_policy:
        raise ProjectValidationError("Rumble render protections are mandatory.")
    if recipe is not None:
        parsed = (
            recipe
            if isinstance(recipe, RumblePreviewRecipe)
            else RumblePreviewRecipe.from_dict(dict(recipe))
        )
        if recipe_sha != parsed.recipe_body_sha256:
            raise ProjectValidationError("Rumble render belongs to a different recipe.")
        if proposal_sha != parsed.proposal_identity["proposal_body_sha256"]:
            raise ProjectValidationError("Rumble render belongs to a different proposal.")
        if scope != parsed.selected_scope:
            raise ProjectValidationError("Rumble render belongs to a different scope.")
        if input_identity != {
            **parsed.input_identity,
            "normalized_pcm_sha256": parsed.proposal_identity["normalized_pcm_sha256"],
        }:
            raise ProjectValidationError("Rumble render input identity is inconsistent.")
        expected_algorithm = {
            "id": RUMBLE_PREVIEW_ALGORITHM_ID,
            "module": RUMBLE_PREVIEW_MODULE_ID,
            "module_sha256": _current_preview_module_sha256(),
            **_runtime_identity(),
        }
        if algorithm != expected_algorithm or {
            key: parsed.algorithm[key]
            for key in expected_algorithm
        } != expected_algorithm:
            raise ProjectValidationError("Rumble render algorithm identity is not current.")
        if filter_identity != _filter_identity(parsed):
            raise ProjectValidationError("Rumble render filter identity is inconsistent.")
        if audition["original_linear_gain"] != parsed.config.original_audition_gain:
            raise ProjectValidationError("Rumble render original gain is inconsistent.")
        if audition["residue_monitor_linear_gain"] != parsed.config.residue_monitor_gain:
            raise ProjectValidationError("Rumble render residue gain is inconsistent.")
        if (
            abs(float(audition["proposed_gain_db"])) > parsed.config.maximum_match_gain_db
            or float(audition["match_mad_db"]) > parsed.config.maximum_match_mad_db
        ):
            raise ProjectValidationError("Rumble render loudness match exceeds recipe bounds.")
    return data


def _parse_channel_metrics(value: Any, channels: int) -> list[dict[str, Any]]:
    raw_metrics = _array(value, "Rumble receipt channel metrics")
    if len(raw_metrics) != channels:
        raise ProjectValidationError("Rumble receipt metrics do not cover every channel.")
    result: list[dict[str, Any]] = []
    keys = {
        "channel_index",
        "removed_rms_dbfs",
        "removed_peak",
        "removed_energy_ratio",
        "reference_low_band_reduction_db",
        "reference_comparison_band_change_db",
        "removed_energy_below_comparison_fraction",
    }
    for channel, raw in enumerate(raw_metrics):
        metric = _object(raw, "Rumble receipt channel metric")
        _strict_keys(metric, keys, "Rumble receipt channel metric")
        if metric["channel_index"] != channel:
            raise ProjectValidationError("Rumble receipt channels are unordered.")
        _number(metric["removed_rms_dbfs"], "Rumble removed RMS", -400.0, 1.0)
        _number(metric["removed_peak"], "Rumble removed peak", 0.0, 1.0)
        _number(metric["removed_energy_ratio"], "Rumble removed energy ratio", 0.0, 1.0)
        _number(
            metric["reference_low_band_reduction_db"],
            "Rumble low-band reduction",
            -100.0,
            100.0,
        )
        _number(
            metric["reference_comparison_band_change_db"],
            "Rumble comparison-band change",
            -100.0,
            100.0,
        )
        _number(
            metric["removed_energy_below_comparison_fraction"],
            "Rumble removed low-frequency fraction",
            0.0,
            1.0,
        )
        result.append(dict(metric))
    return result


def _validate_aggregate(value: Any, metrics: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    data = _object(value, "Rumble receipt aggregate")
    expected = _aggregate(metrics)
    _strict_keys(data, set(expected), "Rumble receipt aggregate")
    for key in data:
        _number(data[key], f"Rumble receipt aggregate {key}", -100.0, 100.0)
    if data != expected:
        raise ProjectValidationError("Rumble receipt aggregate does not match its metrics.")
    return data


def validate_rumble_preview_receipt(
    value: Any,
    *,
    recipe: RumblePreviewRecipe | Mapping[str, Any] | None = None,
    render_manifest: Mapping[str, Any] | None = None,
    arrays: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> dict[str, Any]:
    """Validate a receipt, optionally recomputing every array-derived metric."""

    data = _object(value, "Rumble preview receipt")
    _strict_keys(
        data,
        {
            "schema",
            "receipt_body_sha256",
            "render_body_sha256",
            "recipe_body_sha256",
            "proposal_body_sha256",
            "algorithm",
            "input",
            "selected_scope",
            "filter",
            "raw_arrays",
            "audition",
            "channel_metrics",
            "aggregate",
            "proof",
            "policy",
        },
        "Rumble preview receipt",
    )
    if data["schema"] != RUMBLE_PREVIEW_RECEIPT_SCHEMA:
        raise ProjectValidationError("Rumble preview receipt schema is unsupported.")
    root = _digest(data["receipt_body_sha256"], "Rumble receipt body SHA-256")
    body = dict(data)
    del body["receipt_body_sha256"]
    if canonical_json_sha256(body) != root:
        raise ProjectValidationError("Rumble preview receipt body identity is stale.")
    render_sha = _digest(data["render_body_sha256"], "Rumble receipt render SHA-256")
    recipe_sha = _digest(data["recipe_body_sha256"], "Rumble receipt recipe SHA-256")
    proposal_sha = _digest(data["proposal_body_sha256"], "Rumble receipt proposal SHA-256")
    algorithm = _validate_algorithm(data["algorithm"], "Rumble receipt algorithm")
    input_identity = _validate_input_identity(data["input"], "Rumble receipt input")
    sample_count = int(input_identity["sample_count"])
    channels = int(input_identity["channel_count"])
    scope = NoiseAnalysisScope.from_dict(data["selected_scope"], sample_count)
    filter_identity = _validate_filter(data["filter"])
    raw_arrays = _validate_raw_arrays(data["raw_arrays"])
    audition = _validate_audition(data["audition"])
    metrics = _parse_channel_metrics(data["channel_metrics"], channels)
    _validate_aggregate(data["aggregate"], metrics)
    proof = _object(data["proof"], "Rumble receipt proof")
    proof_keys = {
        "source_array_immutable",
        "original_matches_input",
        "outside_scope_proposed_bit_identical",
        "outside_scope_removed_zero",
        "raw_algebra_float64_bounded",
        "edge_residue_starts_and_ends_at_zero",
        "filter_response_attenuation_only",
        "proposed_does_not_clip",
        "audition_gains_do_not_clip",
    }
    _strict_keys(proof, proof_keys, "Rumble receipt proof")
    if any(proof[key] is not True for key in proof_keys):
        raise ProjectValidationError("Rumble preview receipt proof must be wholly true.")
    policy = _object(data["policy"], "Rumble receipt policy")
    expected_policy = _receipt_policy()
    _strict_keys(policy, set(expected_policy), "Rumble receipt policy")
    if policy != expected_policy:
        raise ProjectValidationError("Rumble receipt protections are mandatory.")
    parsed_recipe: RumblePreviewRecipe | None = None
    if recipe is not None:
        parsed_recipe = (
            recipe
            if isinstance(recipe, RumblePreviewRecipe)
            else RumblePreviewRecipe.from_dict(dict(recipe))
        )
        if recipe_sha != parsed_recipe.recipe_body_sha256:
            raise ProjectValidationError("Rumble receipt belongs to a different recipe.")
        if proposal_sha != parsed_recipe.proposal_identity["proposal_body_sha256"]:
            raise ProjectValidationError("Rumble receipt belongs to a different proposal.")
        if scope != parsed_recipe.selected_scope:
            raise ProjectValidationError("Rumble receipt belongs to a different scope.")
        if input_identity != {
            **parsed_recipe.input_identity,
            "normalized_pcm_sha256": parsed_recipe.proposal_identity["normalized_pcm_sha256"],
        }:
            raise ProjectValidationError("Rumble receipt input identity is inconsistent.")
        expected_algorithm = {
            "id": RUMBLE_PREVIEW_ALGORITHM_ID,
            "module": RUMBLE_PREVIEW_MODULE_ID,
            "module_sha256": _current_preview_module_sha256(),
            **_runtime_identity(),
        }
        if algorithm != expected_algorithm:
            raise ProjectValidationError("Rumble receipt algorithm identity is not current.")
        if filter_identity != _filter_identity(parsed_recipe):
            raise ProjectValidationError("Rumble receipt filter identity is inconsistent.")
        _validate_metrics(metrics, parsed_recipe)
    if render_manifest is not None:
        parsed_render = validate_rumble_preview_render_manifest(
            dict(render_manifest), recipe=parsed_recipe
        )
        if render_sha != parsed_render["render_body_sha256"]:
            raise ProjectValidationError("Rumble receipt belongs to a different render.")
        for field in ("algorithm", "input", "selected_scope", "filter", "raw_arrays", "audition"):
            if data[field] != parsed_render[field]:
                raise ProjectValidationError(
                    f"Rumble receipt {field} differs from its render manifest."
                )
    if arrays is not None:
        if len(arrays) != 3:
            raise ProjectValidationError("Rumble receipt array proof requires three arrays.")
        original, original_mono = _normalize_pcm(arrays[0])
        proposed, proposed_mono = _normalize_pcm(arrays[1])
        removed, removed_mono = _normalize_pcm(arrays[2])
        if not (
            original_mono == proposed_mono == removed_mono
            and original.shape == proposed.shape == removed.shape == (sample_count, channels)
        ):
            raise ProjectValidationError("Rumble receipt arrays have inconsistent geometry.")
        if (
            _pcm_sha256(original) != raw_arrays["original_sha256"]
            or _pcm_sha256(proposed) != raw_arrays["proposed_sha256"]
            or _pcm_sha256(removed) != raw_arrays["removed_sha256"]
            or _pcm_sha256(original) != input_identity["normalized_pcm_sha256"]
        ):
            raise ProjectValidationError("Rumble receipt arrays do not match their hashes.")
        reconstruction_error = float(np.max(np.abs(original - (proposed + removed))))
        if _quantize(reconstruction_error) != raw_arrays["maximum_reconstruction_error"]:
            raise ProjectValidationError("Rumble receipt array algebra does not match its report.")
        if (
            not np.array_equal(proposed[: scope.start_sample], original[: scope.start_sample])
            or not np.array_equal(
                proposed[scope.end_sample_exclusive :],
                original[scope.end_sample_exclusive :],
            )
            or bool(np.any(removed[: scope.start_sample]))
            or bool(np.any(removed[scope.end_sample_exclusive :]))
            or bool(np.any(removed[scope.start_sample]))
            or bool(np.any(removed[scope.end_sample_exclusive - 1]))
        ):
            raise ProjectValidationError("Rumble receipt arrays violate scope or edge isolation.")
        if parsed_recipe is not None:
            recomputed_metrics = _channel_metrics(
                original, proposed, removed, parsed_recipe
            )
            if recomputed_metrics != metrics:
                raise ProjectValidationError(
                    "Rumble receipt metrics do not match independent array analysis."
                )
            if _audition_gains(original, proposed, parsed_recipe) != audition:
                raise ProjectValidationError(
                    "Rumble receipt audition evidence does not match its arrays."
                )
        if (
            float(np.max(np.abs(proposed))) * float(audition["proposed_linear_gain"]) > 1.0
            or float(np.max(np.abs(removed)))
            * float(audition["residue_monitor_linear_gain"])
            > 1.0
        ):
            raise ProjectValidationError("Rumble receipt arrays violate audition gain bounds.")
    return data


def render_rumble_preview(
    samples: np.ndarray,
    proposal_value: ContinuousNoiseProposalDocument | Mapping[str, Any],
    recipe_value: RumblePreviewRecipe | Mapping[str, Any],
) -> RumblePreviewResult:
    """Render immutable Original/Proposed/Removed arrays or fail closed."""

    if type(samples) is not np.ndarray:
        raise ProjectValidationError("Rumble preview PCM must be a NumPy array.")
    source_before = hashlib.sha256(samples.tobytes(order="A")).hexdigest()
    pcm, was_mono = _normalize_pcm(samples)
    proposal = _strict_current_proposal(proposal_value)
    recipe = (
        recipe_value
        if isinstance(recipe_value, RumblePreviewRecipe)
        else RumblePreviewRecipe.from_dict(dict(recipe_value))
    )
    recipe = RumblePreviewRecipe.from_dict(recipe.to_dict())
    expected_runtime = _runtime_identity()
    if recipe.algorithm["module_sha256"] != _current_preview_module_sha256():
        raise ProjectValidationError("Rumble preview recipe module identity is stale.")
    if any(recipe.algorithm[key] != value for key, value in expected_runtime.items()):
        raise ProjectValidationError("Rumble preview recipe runtime identity is stale.")
    if recipe.proposal_identity["proposal_body_sha256"] != proposal.proposal_body_sha256:
        raise ProjectValidationError("Rumble preview recipe belongs to a stale proposal.")
    if recipe.proposal_identity["rumble_body_sha256"] != canonical_json_sha256(
        proposal.rumble.to_dict()
    ):
        raise ProjectValidationError("Rumble preview recipe rumble identity is stale.")
    if (
        recipe.proposal_identity["analysis_module_sha256"]
        != proposal.algorithm["module_sha256"]
        or recipe.proposal_identity["analysis_config_sha256"]
        != proposal.algorithm["config_sha256"]
        or recipe.proposal_identity["analysis_numpy_version"]
        != proposal.algorithm["numpy_version"]
    ):
        raise ProjectValidationError("Rumble preview recipe analysis identity is stale.")
    if (
        recipe.selected_scope != proposal.scope
        or recipe.noise_references != proposal.noise_references
    ):
        raise ProjectValidationError("Rumble preview recipe scope evidence is stale.")
    if (
        recipe.observed_lower_hz != proposal.rumble.observed_lower_hz
        or recipe.observed_upper_hz != proposal.rumble.observed_upper_hz
        or recipe.comparison_lower_hz != proposal.config.rumble_comparison_lower_hz
        or recipe.comparison_upper_hz != proposal.config.rumble_comparison_upper_hz
        or recipe.analysis_window_ms != proposal.config.window_ms
    ):
        raise ProjectValidationError("Rumble preview recipe target band is stale.")
    expected_input = {
        "sample_rate": proposal.sample_rate,
        "sample_count": proposal.sample_count,
        "channel_count": proposal.channel_count,
    }
    if recipe.input_identity != expected_input or pcm.shape != (
        proposal.sample_count,
        proposal.channel_count,
    ):
        raise ProjectValidationError("Rumble preview input geometry is stale.")
    input_sha256 = _pcm_sha256(pcm)
    if (
        input_sha256 != proposal.normalized_pcm_sha256
        or input_sha256 != recipe.proposal_identity["normalized_pcm_sha256"]
    ):
        raise ProjectValidationError("Rumble preview PCM does not match the reviewed proposal.")
    if bool(np.any(np.abs(pcm) >= proposal.config.clipping_amplitude)):
        raise ProjectValidationError("Rumble preview refuses clipped source PCM.")

    scope = recipe.selected_scope
    scoped = pcm[scope.start_sample : scope.end_sample_exclusive]
    filter_identity = _filter_identity(recipe)
    padding_samples = int(filter_identity["reflection_padding_samples"])
    filtered = _high_pass_scope(
        scoped,
        sample_rate=proposal.sample_rate,
        cutoff_hz=recipe.config.cutoff_hz,
        order=recipe.config.filter_order,
        padding_samples=padding_samples,
    )
    envelope = _edge_envelope(scoped.shape[0], int(filter_identity["edge_fade_samples"]))
    raw_original = pcm.copy()
    raw_proposed = pcm.copy()
    candidate_removed = (scoped - filtered) * envelope[:, np.newaxis]
    raw_proposed[scope.start_sample : scope.end_sample_exclusive] = (
        scoped - candidate_removed
    )
    raw_removed = raw_original - raw_proposed
    if not bool(np.all(np.isfinite(raw_proposed))):
        raise ProjectValidationError("Rumble preview produced nonfinite PCM.")
    if bool(np.any(np.abs(raw_proposed) > 1.0)):
        raise ProjectValidationError("Rumble preview proposed PCM would clip.")
    metrics = _channel_metrics(raw_original, raw_proposed, raw_removed, recipe)
    _validate_metrics(metrics, recipe)
    audition = _audition_gains(raw_original, raw_proposed, recipe)
    proposed_monitor_peak = float(np.max(np.abs(raw_proposed))) * float(
        audition["proposed_linear_gain"]
    )
    residue_monitor_peak = float(np.max(np.abs(raw_removed))) * float(
        audition["residue_monitor_linear_gain"]
    )
    if proposed_monitor_peak > 1.0 or residue_monitor_peak > 1.0:
        raise ProjectValidationError("Rumble preview audition gain would clip.")

    before = slice(0, scope.start_sample)
    after = slice(scope.end_sample_exclusive, pcm.shape[0])
    reconstruction_error = float(
        np.max(np.abs(raw_original - (raw_proposed + raw_removed)))
    )
    algebra_tolerance = float(
        np.finfo(np.float64).eps
        * 2.0
        * max(1.0, float(np.max(np.abs(raw_original))))
    )
    source_after = hashlib.sha256(samples.tobytes(order="A")).hexdigest()
    proof = {
        "source_array_immutable": source_before == source_after,
        "original_matches_input": _pcm_sha256(raw_original) == input_sha256,
        "outside_scope_proposed_bit_identical": np.array_equal(
            raw_proposed[before], raw_original[before]
        )
        and np.array_equal(raw_proposed[after], raw_original[after]),
        "outside_scope_removed_zero": not bool(np.any(raw_removed[before]))
        and not bool(np.any(raw_removed[after])),
        "raw_algebra_float64_bounded": reconstruction_error <= algebra_tolerance,
        "edge_residue_starts_and_ends_at_zero": not bool(
            np.any(raw_removed[scope.start_sample])
        )
        and not bool(np.any(raw_removed[scope.end_sample_exclusive - 1])),
        "filter_response_attenuation_only": filter_identity["attenuation_only"] is True
        and float(filter_identity["maximum_frequency_gain"]) == 1.0,
        "proposed_does_not_clip": not bool(np.any(np.abs(raw_proposed) > 1.0)),
        "audition_gains_do_not_clip": proposed_monitor_peak <= 1.0
        and residue_monitor_peak <= 1.0,
    }
    if any(value is not True for value in proof.values()):
        raise ProjectValidationError("Rumble preview could not prove immutable bounded algebra.")
    algorithm_identity = {
        "id": RUMBLE_PREVIEW_ALGORITHM_ID,
        "module": RUMBLE_PREVIEW_MODULE_ID,
        "module_sha256": _current_preview_module_sha256(),
        **expected_runtime,
    }
    input_identity = {
        **expected_input,
        "normalized_pcm_sha256": input_sha256,
    }
    raw_array_identity = {
        "original_sha256": _pcm_sha256(raw_original),
        "proposed_sha256": _pcm_sha256(raw_proposed),
        "removed_sha256": _pcm_sha256(raw_removed),
        "algebra": "original = proposed + removed",
        "maximum_reconstruction_error": _quantize(reconstruction_error),
    }
    render_body: dict[str, Any] = {
        "schema": RUMBLE_PREVIEW_RENDER_SCHEMA,
        "recipe_body_sha256": recipe.recipe_body_sha256,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "algorithm": algorithm_identity,
        "input": input_identity,
        "selected_scope": scope.to_dict(),
        "filter": filter_identity,
        "raw_arrays": raw_array_identity,
        "audition": audition,
        "policy": _render_policy(),
    }
    render_manifest = dict(render_body)
    render_manifest["render_body_sha256"] = canonical_json_sha256(render_body)
    render_manifest = validate_rumble_preview_render_manifest(
        render_manifest, recipe=recipe
    )
    receipt_body: dict[str, Any] = {
        "schema": RUMBLE_PREVIEW_RECEIPT_SCHEMA,
        "render_body_sha256": render_manifest["render_body_sha256"],
        "recipe_body_sha256": recipe.recipe_body_sha256,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "algorithm": algorithm_identity,
        "input": input_identity,
        "selected_scope": scope.to_dict(),
        "filter": filter_identity,
        "raw_arrays": raw_array_identity,
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
    receipt = validate_rumble_preview_receipt(
        receipt,
        recipe=recipe,
        render_manifest=render_manifest,
        arrays=(original_output, proposed_output, removed_output),
    )
    original_output.setflags(write=False)
    proposed_output.setflags(write=False)
    removed_output.setflags(write=False)
    return RumblePreviewResult(
        original=original_output,
        proposed=proposed_output,
        removed=removed_output,
        render_manifest=render_manifest,
        receipt=receipt,
    )
