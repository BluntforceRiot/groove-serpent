"""Deterministic, owner-audition-only preview of stationary hum evidence.

The renderer subtracts only the exact 50/60 Hz sinusoidal lines and harmonics
declared by one current, strictly validated continuous-noise proposal.  It is
not a broad equalizer and does not implement rumble, hiss, crackle, project
mutation, publication, or approval.  Coefficients are fitted independently in
every declared noise-reference window and must remain stable across windows
and channels before a preview is returned.

A review attestation is mandatory to create a recipe, but a caller-supplied
token is not proof that a human actually listened.  A future review surface
must own that trust boundary.  Every successful output remains an audition
preview, never a claim of transparent or quality-neutral restoration.

This pure-array foundation materializes and hashes the entire supplied PCM
array.  It is not a bounded streaming decoder for large real captures.
"""

from __future__ import annotations

import hashlib
import math
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

HUM_REVIEW_ATTESTATION_SCHEMA = "groove-serpent.hum-preview-review-attestation/1"
HUM_PREVIEW_RECIPE_SCHEMA = "groove-serpent.hum-preview-recipe/1"
HUM_PREVIEW_RENDER_SCHEMA = "groove-serpent.hum-preview-render/1"
HUM_PREVIEW_RECEIPT_SCHEMA = "groove-serpent.hum-preview-receipt/1"
HUM_PREVIEW_ALGORITHM_ID = "groove-serpent.stationary-hum-preview/1"
HUM_PREVIEW_MODULE_ID = "groove_serpent.hum_preview"
REVIEW_DECISION = "request_owner_audition_preview"
REVIEW_ACKNOWLEDGEMENT = "caller_attestation_is_not_proof_of_human_audition_or_restoration_approval"

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


def _module_sha256(module_path: str | None) -> str:
    if not module_path:
        raise ProjectValidationError("Required analysis module has no filesystem identity.")
    try:
        return hashlib.sha256(Path(module_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ProjectValidationError(
            "Required analysis module identity could not be read."
        ) from exc


def _current_analysis_module_sha256() -> str:
    return _module_sha256(continuous_noise_module.__file__)


def _current_preview_module_sha256() -> str:
    return _module_sha256(__file__)


def _pcm_sha256(pcm: np.ndarray) -> str:
    return hashlib.sha256(pcm.tobytes(order="C")).hexdigest()


def _normalize_pcm(samples: np.ndarray) -> tuple[np.ndarray, bool]:
    if type(samples) is not np.ndarray:
        raise ProjectValidationError("Hum preview PCM must be a NumPy array.")
    if samples.ndim not in (1, 2):
        raise ProjectValidationError("Hum preview PCM must have one or two dimensions.")
    if samples.dtype.kind != "f":
        raise ProjectValidationError("Hum preview PCM must use a floating-point dtype.")
    if samples.shape[0] < 1:
        raise ProjectValidationError("Hum preview PCM must contain at least one frame.")
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    if not 1 <= channels <= _MAX_CHANNELS:
        raise ProjectValidationError(
            f"Hum preview PCM must contain between 1 and {_MAX_CHANNELS} channels."
        )
    if not bool(np.all(np.isfinite(samples))):
        raise ProjectValidationError("Hum preview PCM must contain only finite values.")
    if bool(np.any(np.abs(samples) > 1.0)):
        raise ProjectValidationError("Hum preview PCM must be normalized to [-1, 1].")
    framed = samples[:, np.newaxis] if samples.ndim == 1 else samples
    return np.ascontiguousarray(framed, dtype="<f8"), samples.ndim == 1


def _recipe_policy() -> dict[str, Any]:
    return {
        "automatic_application_forbidden": True,
        "automatic_publication_forbidden": True,
        "attestation_is_not_human_audition_proof": True,
        "mode": "owner_audition_preview_only",
        "quality_neutrality_claimed": False,
        "rumble_rendering_included": False,
        "source_audio_modified": False,
    }


@dataclass(frozen=True, slots=True)
class HumPreviewConfig:
    """Conservative v1 fit, removal, edge, and audition bounds."""

    edge_fade_ms: int = 25
    maximum_coefficient_relative_spread: float = 0.35
    minimum_fitted_amplitude: float = 0.000001
    maximum_fitted_amplitude: float = 0.02
    maximum_channel_fundamental_ratio: float = 4.0
    maximum_removed_rms_dbfs: float = -28.0
    maximum_removed_peak: float = 0.03
    maximum_removed_energy_ratio: float = 0.02
    maximum_retained_line_ratio: float = 0.35
    minimum_loudness_windows: int = 2
    loudness_window_floor_dbfs: float = -55.0
    maximum_match_gain_db: float = 0.15
    maximum_match_mad_db: float = 0.05
    original_audition_gain: float = 1.0
    residue_monitor_gain: float = 16.0

    def validate(self) -> None:
        _integer(self.edge_fade_ms, "Hum edge fade", 5, 250)
        _number(
            self.maximum_coefficient_relative_spread,
            "Maximum coefficient relative spread",
            0.01,
            1.0,
        )
        minimum = _number(
            self.minimum_fitted_amplitude,
            "Minimum fitted amplitude",
            0.000000001,
            0.01,
        )
        maximum = _number(
            self.maximum_fitted_amplitude,
            "Maximum fitted amplitude",
            0.000001,
            0.1,
        )
        if minimum >= maximum:
            raise ProjectValidationError(
                "Minimum fitted amplitude must be below maximum fitted amplitude."
            )
        _number(
            self.maximum_channel_fundamental_ratio,
            "Maximum channel fundamental ratio",
            1.0,
            20.0,
        )
        _number(self.maximum_removed_rms_dbfs, "Maximum removed RMS", -100.0, -6.0)
        _number(self.maximum_removed_peak, "Maximum removed peak", 0.000001, 0.25)
        _number(
            self.maximum_removed_energy_ratio,
            "Maximum removed energy ratio",
            0.000001,
            0.25,
        )
        _number(
            self.maximum_retained_line_ratio,
            "Maximum retained line ratio",
            0.0,
            1.0,
        )
        _integer(self.minimum_loudness_windows, "Minimum loudness windows", 2, 1_000)
        _number(
            self.loudness_window_floor_dbfs,
            "Loudness window floor",
            -120.0,
            -20.0,
        )
        _number(self.maximum_match_gain_db, "Maximum matching gain", 0.001, 1.0)
        _number(self.maximum_match_mad_db, "Maximum matching MAD", 0.0, 0.5)
        if self.original_audition_gain != 1.0:
            raise ProjectValidationError("Original audition gain must remain exactly 1.0.")
        _number(self.residue_monitor_gain, "Residue monitor gain", 1.0, 64.0)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> HumPreviewConfig:
        data = _object(value, "Hum preview configuration")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hum preview configuration")
        result = cls(**data)
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class HumReviewAttestation:
    """Caller attestation required for recipe creation, not proof of listening."""

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
    def from_dict(cls, value: Any, sample_count: int) -> HumReviewAttestation:
        data = _object(value, "Hum preview review attestation")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hum preview review attestation")
        if data["schema"] != HUM_REVIEW_ATTESTATION_SCHEMA:
            raise ProjectValidationError("Hum review attestation schema is unsupported.")
        token = _digest(data["attestation_token"], "Hum review attestation token")
        if len(set(token)) == 1:
            raise ProjectValidationError(
                "Hum review attestation token is structurally non-distinct."
            )
        if data["decision"] != REVIEW_DECISION:
            raise ProjectValidationError("Hum review attestation decision is unsupported.")
        if data["acknowledgement"] != REVIEW_ACKNOWLEDGEMENT:
            raise ProjectValidationError(
                "Hum review attestation must acknowledge its limited authority."
            )
        return cls(
            schema=HUM_REVIEW_ATTESTATION_SCHEMA,
            attestation_token=token,
            decision=REVIEW_DECISION,
            proposal_body_sha256=_digest(
                data["proposal_body_sha256"],
                "Attested proposal body SHA-256",
            ),
            selected_scope=NoiseAnalysisScope.from_dict(
                data["selected_scope"],
                sample_count,
            ),
            acknowledgement=REVIEW_ACKNOWLEDGEMENT,
        )


@dataclass(frozen=True, slots=True)
class HumPreviewRecipe:
    schema: str
    recipe_body_sha256: str
    proposal_identity: dict[str, str]
    input_identity: dict[str, Any]
    selected_scope: NoiseAnalysisScope
    fundamental_hz: int
    harmonics: tuple[int, ...]
    review_attestation: HumReviewAttestation
    algorithm: dict[str, str]
    config: HumPreviewConfig
    policy: dict[str, Any]

    def body_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "proposal_identity": dict(self.proposal_identity),
            "input_identity": dict(self.input_identity),
            "selected_scope": self.selected_scope.to_dict(),
            "fundamental_hz": self.fundamental_hz,
            "harmonics": list(self.harmonics),
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
    def from_dict(cls, value: Any) -> HumPreviewRecipe:
        data = _object(value, "Hum preview recipe")
        _strict_keys(data, set(cls.__dataclass_fields__), "Hum preview recipe")
        if data["schema"] != HUM_PREVIEW_RECIPE_SCHEMA:
            raise ProjectValidationError("Hum preview recipe schema is unsupported.")
        body_sha = _digest(data["recipe_body_sha256"], "Hum recipe body SHA-256")
        body = dict(data)
        del body["recipe_body_sha256"]
        if canonical_json_sha256(body) != body_sha:
            raise ProjectValidationError("Hum preview recipe body identity is stale.")
        input_identity = _object(data["input_identity"], "Hum recipe input identity")
        _strict_keys(
            input_identity,
            {"sample_rate", "sample_count", "channel_count"},
            "Hum recipe input identity",
        )
        sample_count = _integer(
            input_identity["sample_count"],
            "Hum recipe sample count",
            1,
            2**63 - 1,
        )
        sample_rate = _integer(
            input_identity["sample_rate"],
            "Hum recipe sample rate",
            8_000,
            768_000,
        )
        channel_count = _integer(
            input_identity["channel_count"],
            "Hum recipe channel count",
            1,
            _MAX_CHANNELS,
        )
        selected_scope = NoiseAnalysisScope.from_dict(data["selected_scope"], sample_count)
        proposal_identity = _object(
            data["proposal_identity"],
            "Hum recipe proposal identity",
        )
        _strict_keys(
            proposal_identity,
            {
                "analysis_algorithm_id",
                "analysis_config_sha256",
                "analysis_module_sha256",
                "hum_body_sha256",
                "normalized_pcm_sha256",
                "proposal_body_sha256",
                "scope_sha256",
            },
            "Hum recipe proposal identity",
        )
        if proposal_identity["analysis_algorithm_id"] != CONTINUOUS_NOISE_ALGORITHM_ID:
            raise ProjectValidationError("Hum recipe analysis algorithm is unsupported.")
        for key in (
            "analysis_config_sha256",
            "analysis_module_sha256",
            "hum_body_sha256",
            "normalized_pcm_sha256",
            "proposal_body_sha256",
            "scope_sha256",
        ):
            _digest(proposal_identity[key], f"Hum recipe {key}")
        if proposal_identity["scope_sha256"] != canonical_json_sha256(selected_scope.to_dict()):
            raise ProjectValidationError("Hum recipe scope identity is inconsistent.")
        fundamental = _integer(data["fundamental_hz"], "Hum fundamental", 50, 60)
        if fundamental not in (50, 60):
            raise ProjectValidationError("Hum recipe fundamental must be 50 or 60 Hz.")
        harmonic_values = _array(data["harmonics"], "Hum recipe harmonics")
        if (
            any(type(item) is not int or item not in (1, 2, 3, 4) for item in harmonic_values)
            or harmonic_values != sorted(set(harmonic_values))
            or 1 not in harmonic_values
            or len(harmonic_values) < 2
        ):
            raise ProjectValidationError("Hum recipe harmonics are invalid.")
        review = HumReviewAttestation.from_dict(
            data["review_attestation"],
            sample_count,
        )
        if review.proposal_body_sha256 != proposal_identity["proposal_body_sha256"]:
            raise ProjectValidationError("Hum recipe review attests a different proposal.")
        if review.selected_scope != selected_scope:
            raise ProjectValidationError("Hum recipe review attests a different scope.")
        config = HumPreviewConfig.from_dict(data["config"])
        algorithm = _object(data["algorithm"], "Hum preview algorithm identity")
        _strict_keys(
            algorithm,
            {"config_sha256", "id", "module", "module_sha256", "numpy_version"},
            "Hum preview algorithm identity",
        )
        if algorithm["id"] != HUM_PREVIEW_ALGORITHM_ID:
            raise ProjectValidationError("Hum preview algorithm is unsupported.")
        if algorithm["module"] != HUM_PREVIEW_MODULE_ID:
            raise ProjectValidationError("Hum preview module is unsupported.")
        _digest(algorithm["module_sha256"], "Hum preview module SHA-256")
        _digest(algorithm["config_sha256"], "Hum preview config SHA-256")
        if algorithm["config_sha256"] != canonical_json_sha256(config.to_dict()):
            raise ProjectValidationError("Hum preview config identity is inconsistent.")
        _text(algorithm["numpy_version"], "Hum preview NumPy version", maximum=64)
        policy = _object(data["policy"], "Hum preview recipe policy")
        expected_policy = _recipe_policy()
        _strict_keys(policy, set(expected_policy), "Hum preview recipe policy")
        if policy != expected_policy:
            raise ProjectValidationError("Hum preview recipe protections are mandatory.")
        return cls(
            schema=HUM_PREVIEW_RECIPE_SCHEMA,
            recipe_body_sha256=body_sha,
            proposal_identity={key: cast(str, proposal_identity[key]) for key in proposal_identity},
            input_identity={
                "sample_rate": sample_rate,
                "sample_count": sample_count,
                "channel_count": channel_count,
            },
            selected_scope=selected_scope,
            fundamental_hz=fundamental,
            harmonics=tuple(cast(list[int], harmonic_values)),
            review_attestation=review,
            algorithm={key: cast(str, algorithm[key]) for key in algorithm},
            config=config,
            policy=expected_policy,
        )


@dataclass(frozen=True, slots=True)
class HumPreviewResult:
    original: np.ndarray
    proposed: np.ndarray
    removed: np.ndarray
    render_manifest: dict[str, Any]
    receipt: dict[str, Any]


def _strict_current_proposal(
    value: ContinuousNoiseProposalDocument | Mapping[str, Any],
) -> ContinuousNoiseProposalDocument:
    raw = value.to_dict() if isinstance(value, ContinuousNoiseProposalDocument) else dict(value)
    proposal = ContinuousNoiseProposalDocument.from_dict(raw)
    if proposal.schema != CONTINUOUS_NOISE_DOCUMENT_SCHEMA:
        raise ProjectValidationError("Hum preview requires a v1 continuous-noise proposal.")
    if proposal.algorithm["module_sha256"] != _current_analysis_module_sha256():
        raise ProjectValidationError("Hum preview proposal analysis module is stale.")
    if proposal.algorithm["numpy_version"] != np.__version__:
        raise ProjectValidationError("Hum preview proposal NumPy identity is stale.")
    if proposal.hum.status != "proposed":
        raise ProjectValidationError("Hum preview cannot render an abstained proposal.")
    if proposal.hum.fundamental_hz not in (50, 60):
        raise ProjectValidationError("Hum preview proposal has no supported fundamental.")
    return proposal


def create_hum_preview_recipe(
    proposal_value: ContinuousNoiseProposalDocument | Mapping[str, Any],
    review_attestation_value: HumReviewAttestation | Mapping[str, Any],
    *,
    config: HumPreviewConfig | None = None,
) -> HumPreviewRecipe:
    """Create a hash-bound preview recipe from one explicit caller attestation."""

    proposal = _strict_current_proposal(proposal_value)
    review_raw = (
        review_attestation_value.to_dict()
        if isinstance(review_attestation_value, HumReviewAttestation)
        else dict(review_attestation_value)
    )
    review = HumReviewAttestation.from_dict(review_raw, proposal.sample_count)
    if review.proposal_body_sha256 != proposal.proposal_body_sha256:
        raise ProjectValidationError("Hum review attestation is stale for this proposal.")
    if review.selected_scope != proposal.scope:
        raise ProjectValidationError("Hum preview v1 requires the exactly reviewed proposal scope.")
    settings = config or HumPreviewConfig()
    settings.validate()
    config_dict = settings.to_dict()
    fundamental = proposal.hum.fundamental_hz
    if fundamental is None:
        raise ProjectValidationError("Hum preview proposal has no fundamental.")
    proposal_identity = {
        "analysis_algorithm_id": proposal.algorithm["id"],
        "analysis_config_sha256": proposal.algorithm["config_sha256"],
        "analysis_module_sha256": proposal.algorithm["module_sha256"],
        "hum_body_sha256": canonical_json_sha256(proposal.hum.to_dict()),
        "normalized_pcm_sha256": proposal.normalized_pcm_sha256,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "scope_sha256": canonical_json_sha256(proposal.scope.to_dict()),
    }
    recipe = HumPreviewRecipe(
        schema=HUM_PREVIEW_RECIPE_SCHEMA,
        recipe_body_sha256="",
        proposal_identity=proposal_identity,
        input_identity={
            "sample_rate": proposal.sample_rate,
            "sample_count": proposal.sample_count,
            "channel_count": proposal.channel_count,
        },
        selected_scope=proposal.scope,
        fundamental_hz=fundamental,
        harmonics=proposal.hum.detected_harmonics,
        review_attestation=review,
        algorithm={
            "config_sha256": canonical_json_sha256(config_dict),
            "id": HUM_PREVIEW_ALGORITHM_ID,
            "module": HUM_PREVIEW_MODULE_ID,
            "module_sha256": _current_preview_module_sha256(),
            "numpy_version": np.__version__,
        },
        config=settings,
        policy=_recipe_policy(),
    )
    recipe = replace(
        recipe,
        recipe_body_sha256=canonical_json_sha256(recipe.body_dict()),
    )
    return HumPreviewRecipe.from_dict(recipe.to_dict())


def _window_starts(start: int, end: int, length: int) -> tuple[int, ...]:
    return tuple(range(start, end - length + 1, length))


def _program_ranges(
    scope: NoiseAnalysisScope,
    references: Sequence[NoiseReferenceRegion],
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    cursor = scope.start_sample
    for reference in sorted(references, key=lambda item: item.start_sample):
        if cursor < reference.start_sample:
            ranges.append((cursor, reference.start_sample))
        cursor = reference.end_sample_exclusive
    if cursor < scope.end_sample_exclusive:
        ranges.append((cursor, scope.end_sample_exclusive))
    return tuple(ranges)


def _fit_window(
    values: np.ndarray,
    *,
    absolute_start: int,
    sample_rate: int,
    frequencies: np.ndarray,
) -> np.ndarray:
    indexes = np.arange(
        absolute_start,
        absolute_start + values.shape[0],
        dtype=np.float64,
    )
    columns: list[np.ndarray] = []
    for frequency in frequencies:
        phase = 2.0 * np.pi * float(frequency) * indexes / sample_rate
        columns.extend((np.cos(phase), np.sin(phase)))
    columns.append(np.ones(values.shape[0], dtype=np.float64))
    design = np.column_stack(columns)
    coefficients, _residuals, rank, _singular = np.linalg.lstsq(
        design,
        values,
        rcond=None,
    )
    if rank != design.shape[1] or not bool(np.all(np.isfinite(coefficients))):
        raise ProjectValidationError("Hum sinusoidal regression is singular or nonfinite.")
    return np.asarray(coefficients[:-1], dtype=np.float64)


def _fit_reference_coefficients(
    pcm: np.ndarray,
    proposal: ContinuousNoiseProposalDocument,
    frequencies: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    window_samples = round(proposal.sample_rate * proposal.config.window_ms / 1_000)
    starts: list[int] = []
    for reference in proposal.noise_references:
        starts.extend(
            _window_starts(
                reference.start_sample,
                reference.end_sample_exclusive,
                window_samples,
            )
        )
    if len(starts) < 2:
        raise ProjectValidationError("Hum preview has insufficient reference fit windows.")
    coefficients = np.empty(
        (pcm.shape[1], len(starts), frequencies.size * 2),
        dtype=np.float64,
    )
    for channel in range(pcm.shape[1]):
        for window_index, start in enumerate(starts):
            coefficients[channel, window_index, :] = _fit_window(
                pcm[start : start + window_samples, channel],
                absolute_start=start,
                sample_rate=proposal.sample_rate,
                frequencies=frequencies,
            )
    aggregate = np.median(coefficients, axis=1)
    return aggregate, coefficients


def _edge_envelope(length: int, fade_samples: int) -> np.ndarray:
    if fade_samples < 1 or length <= fade_samples * 2:
        raise ProjectValidationError("Hum preview scope is too short for bounded edge treatment.")
    envelope = np.ones(length, dtype=np.float64)
    phase = np.arange(fade_samples, dtype=np.float64) / fade_samples
    fade = 0.5 - 0.5 * np.cos(np.pi * phase)
    envelope[:fade_samples] = fade
    envelope[-fade_samples:] = fade[::-1]
    return envelope


def _db_rms(values: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(values, dtype=np.float64))))
    return max(-400.0, 20.0 * math.log10(max(rms, _EPSILON)))


def _line_metrics(
    original: np.ndarray,
    proposed: np.ndarray,
    proposal_document: ContinuousNoiseProposalDocument,
    frequencies: np.ndarray,
    aggregate_coefficients: np.ndarray,
    all_coefficients: np.ndarray,
) -> list[dict[str, Any]]:
    window_samples = round(
        proposal_document.sample_rate * proposal_document.config.window_ms / 1_000
    )
    starts: list[int] = []
    for reference in proposal_document.noise_references:
        starts.extend(
            _window_starts(
                reference.start_sample,
                reference.end_sample_exclusive,
                window_samples,
            )
        )
    result: list[dict[str, Any]] = []
    for channel in range(original.shape[1]):
        residual_fits = np.stack(
            [
                _fit_window(
                    proposed[start : start + window_samples, channel],
                    absolute_start=start,
                    sample_rate=proposal_document.sample_rate,
                    frequencies=frequencies,
                )
                for start in starts
            ]
        )
        harmonic_metrics: list[dict[str, Any]] = []
        for index, harmonic in enumerate(proposal_document.hum.detected_harmonics):
            cosine = float(aggregate_coefficients[channel, index * 2])
            sine = float(aggregate_coefficients[channel, index * 2 + 1])
            amplitude = math.hypot(cosine, sine)
            window_vectors = all_coefficients[
                channel,
                :,
                index * 2 : index * 2 + 2,
            ]
            deviations = np.linalg.norm(
                window_vectors - np.array((cosine, sine)),
                axis=1,
            ) / max(amplitude, _EPSILON)
            residual_vector = np.median(
                residual_fits[:, index * 2 : index * 2 + 2],
                axis=0,
            )
            residual_amplitude = float(np.linalg.norm(residual_vector))
            harmonic_metrics.append(
                {
                    "harmonic": harmonic,
                    "frequency_hz": _quantize(float(frequencies[index])),
                    "cosine_coefficient": _quantize(cosine),
                    "sine_coefficient": _quantize(sine),
                    "fitted_amplitude": _quantize(amplitude),
                    "coefficient_relative_spread": _quantize(float(np.max(deviations))),
                    "residual_amplitude": _quantize(residual_amplitude),
                    "retained_residual_ratio": _quantize(
                        residual_amplitude / max(amplitude, _EPSILON)
                    ),
                }
            )
        selected = slice(
            proposal_document.scope.start_sample,
            proposal_document.scope.end_sample_exclusive,
        )
        removed_channel = original[selected, channel] - proposed[selected, channel]
        original_energy = float(np.sum(np.square(original[selected, channel])))
        removed_energy = float(np.sum(np.square(removed_channel)))
        result.append(
            {
                "channel_index": channel,
                "removed_rms_dbfs": _quantize(_db_rms(removed_channel)),
                "removed_peak": _quantize(float(np.max(np.abs(removed_channel)))),
                "removed_energy_ratio": _quantize(removed_energy / max(original_energy, _EPSILON)),
                "harmonics": harmonic_metrics,
            }
        )
    return result


def _audition_gains(
    original: np.ndarray,
    proposed: np.ndarray,
    proposal_document: ContinuousNoiseProposalDocument,
    config: HumPreviewConfig,
) -> dict[str, Any]:
    window_samples = round(
        proposal_document.sample_rate * proposal_document.config.window_ms / 1_000
    )
    starts: list[int] = []
    for start, end in _program_ranges(
        proposal_document.scope,
        proposal_document.noise_references,
    ):
        starts.extend(_window_starts(start, end, window_samples))
    ratios_db: list[float] = []
    for start in starts:
        before = original[start : start + window_samples]
        after = proposed[start : start + window_samples]
        before_rms = float(np.sqrt(np.mean(np.square(before))))
        after_rms = float(np.sqrt(np.mean(np.square(after))))
        if (
            _db_rms(before) >= config.loudness_window_floor_dbfs
            and _db_rms(after) >= config.loudness_window_floor_dbfs
        ):
            ratios_db.append(
                20.0 * math.log10(max(before_rms, _EPSILON) / max(after_rms, _EPSILON))
            )
    if len(ratios_db) < config.minimum_loudness_windows:
        raise ProjectValidationError(
            "Hum preview cannot establish reliable matched audition loudness."
        )
    median_db = float(np.median(np.asarray(ratios_db, dtype=np.float64)))
    mad_db = float(np.median(np.abs(np.asarray(ratios_db, dtype=np.float64) - median_db)))
    if abs(median_db) > config.maximum_match_gain_db or mad_db > config.maximum_match_mad_db:
        raise ProjectValidationError(
            "Hum preview loudness match exceeds conservative gain or stability bounds."
        )
    return {
        "method": "median_program_window_rms_log_ratio/1",
        "window_count": len(ratios_db),
        "original_linear_gain": 1.0,
        "proposed_linear_gain": _quantize(10.0 ** (median_db / 20.0)),
        "proposed_gain_db": _quantize(median_db),
        "match_mad_db": _quantize(mad_db),
        "residue_monitor_linear_gain": _quantize(config.residue_monitor_gain),
        "raw_arrays_are_gain_neutral": True,
    }


def _validate_metrics(
    metrics: list[dict[str, Any]],
    recipe: HumPreviewRecipe,
) -> None:
    if len(metrics) != recipe.input_identity["channel_count"]:
        raise ProjectValidationError("Hum preview metrics do not cover every channel.")
    fundamental_amplitudes: list[float] = []
    for channel, metric in enumerate(metrics):
        if metric["channel_index"] != channel:
            raise ProjectValidationError("Hum preview channel metrics are unordered.")
        if metric["removed_rms_dbfs"] > recipe.config.maximum_removed_rms_dbfs:
            raise ProjectValidationError("Hum preview removed RMS exceeds its safety bound.")
        if metric["removed_peak"] > recipe.config.maximum_removed_peak:
            raise ProjectValidationError("Hum preview removed peak exceeds its safety bound.")
        if metric["removed_energy_ratio"] > recipe.config.maximum_removed_energy_ratio:
            raise ProjectValidationError("Hum preview removed energy exceeds its safety bound.")
        for harmonic_metric in metric["harmonics"]:
            amplitude = float(harmonic_metric["fitted_amplitude"])
            if not (
                recipe.config.minimum_fitted_amplitude
                <= amplitude
                <= recipe.config.maximum_fitted_amplitude
            ):
                raise ProjectValidationError("Hum preview fitted amplitude is out of bounds.")
            if (
                harmonic_metric["coefficient_relative_spread"]
                > recipe.config.maximum_coefficient_relative_spread
            ):
                raise ProjectValidationError(
                    "Hum preview sinusoidal fit is unstable across reference windows."
                )
            if (
                harmonic_metric["retained_residual_ratio"]
                > recipe.config.maximum_retained_line_ratio
            ):
                raise ProjectValidationError("Hum preview retains too much fitted line evidence.")
        fundamental_amplitudes.append(float(metric["harmonics"][0]["fitted_amplitude"]))
    channel_ratio = max(fundamental_amplitudes) / max(
        min(fundamental_amplitudes),
        _EPSILON,
    )
    if channel_ratio > recipe.config.maximum_channel_fundamental_ratio:
        raise ProjectValidationError("Hum preview fundamental amplitudes disagree across channels.")


def _receipt_policy() -> dict[str, Any]:
    return {
        "attestation_is_not_human_audition_proof": True,
        "automatic_application_forbidden": True,
        "mode": "owner_audition_preview_only",
        "quality_neutrality_claimed": False,
        "raw_arrays_are_not_publication_outputs": True,
    }


def _render_policy() -> dict[str, Any]:
    return {
        "audition_gains_are_separate_from_raw_arrays": True,
        "automatic_application_forbidden": True,
        "mode": "owner_audition_preview_only",
        "quality_neutrality_claimed": False,
        "source_audio_modified": False,
    }


def _validate_raw_array_identity(value: Any) -> dict[str, Any]:
    data = _object(value, "Hum render raw arrays")
    _strict_keys(
        data,
        {
            "original_sha256",
            "proposed_sha256",
            "removed_sha256",
            "algebra",
            "maximum_reconstruction_error",
        },
        "Hum render raw arrays",
    )
    for key in ("original_sha256", "proposed_sha256", "removed_sha256"):
        _digest(data[key], f"Hum render {key}")
    if data["algebra"] != "original = proposed + removed":
        raise ProjectValidationError("Hum render raw-array algebra is unsupported.")
    _number(
        data["maximum_reconstruction_error"],
        "Hum raw-array reconstruction error",
        0.0,
        0.000001,
    )
    return data


def _validate_audition_identity(value: Any) -> dict[str, Any]:
    data = _object(value, "Hum render audition gains")
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
        "Hum render audition gains",
    )
    if data["method"] != "median_program_window_rms_log_ratio/1":
        raise ProjectValidationError("Hum render matching method is unsupported.")
    _integer(data["window_count"], "Hum match windows", 2, 1_000_000)
    _number(data["original_linear_gain"], "Original audition gain", 0.01, 100.0)
    _number(data["proposed_linear_gain"], "Proposed audition gain", 0.01, 100.0)
    _number(data["proposed_gain_db"], "Proposed audition gain dB", -40.0, 40.0)
    _number(data["match_mad_db"], "Audition match MAD", 0.0, 40.0)
    _number(data["residue_monitor_linear_gain"], "Residue monitor gain", 1.0, 64.0)
    if data["raw_arrays_are_gain_neutral"] is not True:
        raise ProjectValidationError("Hum render must preserve gain-neutral raw arrays.")
    return data


def validate_hum_preview_render_manifest(
    value: Any,
    *,
    recipe: HumPreviewRecipe | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Strictly validate the hash-bound description of one array render."""

    data = _object(value, "Hum preview render manifest")
    expected = {
        "schema",
        "render_body_sha256",
        "recipe_body_sha256",
        "proposal_body_sha256",
        "algorithm",
        "input",
        "selected_scope",
        "raw_arrays",
        "audition",
        "policy",
    }
    _strict_keys(data, expected, "Hum preview render manifest")
    if data["schema"] != HUM_PREVIEW_RENDER_SCHEMA:
        raise ProjectValidationError("Hum preview render schema is unsupported.")
    root = _digest(data["render_body_sha256"], "Hum render body SHA-256")
    body = dict(data)
    del body["render_body_sha256"]
    if canonical_json_sha256(body) != root:
        raise ProjectValidationError("Hum preview render body identity is stale.")
    recipe_sha = _digest(data["recipe_body_sha256"], "Hum render recipe SHA-256")
    proposal_sha = _digest(
        data["proposal_body_sha256"],
        "Hum render proposal SHA-256",
    )
    algorithm = _object(data["algorithm"], "Hum render algorithm")
    _strict_keys(
        algorithm,
        {"id", "module", "module_sha256", "numpy_version"},
        "Hum render algorithm",
    )
    if algorithm["id"] != HUM_PREVIEW_ALGORITHM_ID or algorithm["module"] != HUM_PREVIEW_MODULE_ID:
        raise ProjectValidationError("Hum render algorithm is unsupported.")
    _digest(algorithm["module_sha256"], "Hum render module SHA-256")
    _text(algorithm["numpy_version"], "Hum render NumPy version", maximum=64)
    input_identity = _object(data["input"], "Hum render input")
    _strict_keys(
        input_identity,
        {"sample_rate", "sample_count", "channel_count", "normalized_pcm_sha256"},
        "Hum render input",
    )
    sample_count = _integer(
        input_identity["sample_count"],
        "Hum render samples",
        1,
        2**63 - 1,
    )
    _integer(input_identity["channel_count"], "Hum render channels", 1, _MAX_CHANNELS)
    _integer(input_identity["sample_rate"], "Hum render sample rate", 8_000, 768_000)
    _digest(input_identity["normalized_pcm_sha256"], "Hum render PCM SHA-256")
    scope = NoiseAnalysisScope.from_dict(data["selected_scope"], sample_count)
    _validate_raw_array_identity(data["raw_arrays"])
    audition = _validate_audition_identity(data["audition"])
    policy = _object(data["policy"], "Hum render policy")
    expected_policy = _render_policy()
    _strict_keys(policy, set(expected_policy), "Hum render policy")
    if policy != expected_policy:
        raise ProjectValidationError("Hum preview render protections are mandatory.")
    if recipe is not None:
        parsed_recipe = (
            recipe
            if isinstance(recipe, HumPreviewRecipe)
            else HumPreviewRecipe.from_dict(dict(recipe))
        )
        if recipe_sha != parsed_recipe.recipe_body_sha256:
            raise ProjectValidationError("Hum render belongs to a different recipe.")
        if proposal_sha != parsed_recipe.proposal_identity["proposal_body_sha256"]:
            raise ProjectValidationError("Hum render belongs to a different proposal.")
        if scope != parsed_recipe.selected_scope:
            raise ProjectValidationError("Hum render belongs to a different scope.")
        if (
            algorithm["module_sha256"] != _current_preview_module_sha256()
            or algorithm["numpy_version"] != np.__version__
            or parsed_recipe.algorithm["module_sha256"] != _current_preview_module_sha256()
            or parsed_recipe.algorithm["numpy_version"] != np.__version__
        ):
            raise ProjectValidationError("Hum render algorithm identity is not current.")
        if input_identity != {
            **parsed_recipe.input_identity,
            "normalized_pcm_sha256": parsed_recipe.proposal_identity["normalized_pcm_sha256"],
        }:
            raise ProjectValidationError("Hum render input identity is inconsistent.")
        if audition["original_linear_gain"] != parsed_recipe.config.original_audition_gain:
            raise ProjectValidationError("Hum render original gain is inconsistent.")
        expected_linear_gain = 10.0 ** (float(audition["proposed_gain_db"]) / 20.0)
        if not math.isclose(
            float(audition["proposed_linear_gain"]),
            expected_linear_gain,
            rel_tol=1e-10,
            abs_tol=1e-12,
        ):
            raise ProjectValidationError("Hum render gain dB and linear value disagree.")
        if audition["residue_monitor_linear_gain"] != (parsed_recipe.config.residue_monitor_gain):
            raise ProjectValidationError("Hum render residue gain is inconsistent.")
        if (
            abs(float(audition["proposed_gain_db"])) > parsed_recipe.config.maximum_match_gain_db
            or float(audition["match_mad_db"]) > parsed_recipe.config.maximum_match_mad_db
        ):
            raise ProjectValidationError("Hum render matching evidence exceeds recipe bounds.")
    return data


def _validate_harmonic_metric(value: Any, expected_harmonic: int) -> dict[str, Any]:
    data = _object(value, "Hum receipt harmonic metric")
    _strict_keys(
        data,
        {
            "harmonic",
            "frequency_hz",
            "cosine_coefficient",
            "sine_coefficient",
            "fitted_amplitude",
            "coefficient_relative_spread",
            "residual_amplitude",
            "retained_residual_ratio",
        },
        "Hum receipt harmonic metric",
    )
    if data["harmonic"] != expected_harmonic:
        raise ProjectValidationError("Hum receipt harmonic sequence is inconsistent.")
    _number(data["frequency_hz"], "Hum receipt frequency", 1.0, 1_000.0)
    _number(data["cosine_coefficient"], "Hum cosine coefficient", -1.0, 1.0)
    _number(data["sine_coefficient"], "Hum sine coefficient", -1.0, 1.0)
    _number(data["fitted_amplitude"], "Hum fitted amplitude", 0.0, 1.0)
    _number(data["coefficient_relative_spread"], "Hum coefficient spread", 0.0, 1_000.0)
    _number(data["residual_amplitude"], "Hum residual amplitude", 0.0, 1.0)
    _number(data["retained_residual_ratio"], "Hum residual ratio", 0.0, 1_000.0)
    return data


def validate_hum_preview_receipt(
    value: Any,
    *,
    recipe: HumPreviewRecipe | Mapping[str, Any] | None = None,
    render_manifest: Mapping[str, Any] | None = None,
    arrays: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> dict[str, Any]:
    """Validate a coherent report; pass ``arrays`` for independent PCM checks."""

    data = _object(value, "Hum preview receipt")
    expected = {
        "schema",
        "receipt_body_sha256",
        "render_body_sha256",
        "recipe_body_sha256",
        "proposal_body_sha256",
        "algorithm",
        "input",
        "selected_scope",
        "raw_arrays",
        "audition",
        "channel_metrics",
        "aggregate",
        "proof",
        "policy",
    }
    _strict_keys(data, expected, "Hum preview receipt")
    if data["schema"] != HUM_PREVIEW_RECEIPT_SCHEMA:
        raise ProjectValidationError("Hum preview receipt schema is unsupported.")
    root = _digest(data["receipt_body_sha256"], "Hum receipt body SHA-256")
    body = dict(data)
    del body["receipt_body_sha256"]
    if canonical_json_sha256(body) != root:
        raise ProjectValidationError("Hum preview receipt body identity is stale.")
    recipe_sha = _digest(data["recipe_body_sha256"], "Hum receipt recipe SHA-256")
    render_sha = _digest(data["render_body_sha256"], "Hum receipt render SHA-256")
    _digest(data["proposal_body_sha256"], "Hum receipt proposal SHA-256")
    algorithm = _object(data["algorithm"], "Hum receipt algorithm")
    _strict_keys(
        algorithm,
        {"id", "module", "module_sha256", "numpy_version"},
        "Hum receipt algorithm",
    )
    if algorithm["id"] != HUM_PREVIEW_ALGORITHM_ID or algorithm["module"] != HUM_PREVIEW_MODULE_ID:
        raise ProjectValidationError("Hum receipt algorithm is unsupported.")
    _digest(algorithm["module_sha256"], "Hum receipt module SHA-256")
    _text(algorithm["numpy_version"], "Hum receipt NumPy version", maximum=64)
    input_identity = _object(data["input"], "Hum receipt input")
    _strict_keys(
        input_identity,
        {"sample_rate", "sample_count", "channel_count", "normalized_pcm_sha256"},
        "Hum receipt input",
    )
    sample_count = _integer(input_identity["sample_count"], "Hum receipt samples", 1, 2**63 - 1)
    channels = _integer(input_identity["channel_count"], "Hum receipt channels", 1, _MAX_CHANNELS)
    _integer(input_identity["sample_rate"], "Hum receipt sample rate", 8_000, 768_000)
    _digest(input_identity["normalized_pcm_sha256"], "Hum receipt PCM SHA-256")
    scope = NoiseAnalysisScope.from_dict(data["selected_scope"], sample_count)
    raw_arrays = _validate_raw_array_identity(data["raw_arrays"])
    audition = _validate_audition_identity(data["audition"])
    metrics = _array(data["channel_metrics"], "Hum receipt channel metrics")
    if len(metrics) != channels:
        raise ProjectValidationError("Hum receipt metrics do not cover every channel.")
    expected_harmonics: tuple[int, ...] | None = None
    parsed_recipe: HumPreviewRecipe | None = None
    if recipe is not None:
        parsed_recipe = (
            recipe
            if isinstance(recipe, HumPreviewRecipe)
            else HumPreviewRecipe.from_dict(dict(recipe))
        )
        expected_harmonics = parsed_recipe.harmonics
        if recipe_sha != parsed_recipe.recipe_body_sha256:
            raise ProjectValidationError("Hum receipt belongs to a different recipe.")
        if data["proposal_body_sha256"] != parsed_recipe.proposal_identity["proposal_body_sha256"]:
            raise ProjectValidationError("Hum receipt belongs to a different proposal.")
        if scope != parsed_recipe.selected_scope:
            raise ProjectValidationError("Hum receipt belongs to a different scope.")
        if algorithm["module_sha256"] != _current_preview_module_sha256():
            raise ProjectValidationError("Hum receipt preview module is not current.")
        if algorithm["numpy_version"] != np.__version__:
            raise ProjectValidationError("Hum receipt NumPy identity is not current.")
        if parsed_recipe.algorithm["module_sha256"] != _current_preview_module_sha256():
            raise ProjectValidationError("Hum receipt recipe module is not current.")
        if parsed_recipe.algorithm["numpy_version"] != np.__version__:
            raise ProjectValidationError("Hum receipt recipe NumPy identity is not current.")
        if input_identity != {
            **parsed_recipe.input_identity,
            "normalized_pcm_sha256": parsed_recipe.proposal_identity["normalized_pcm_sha256"],
        }:
            raise ProjectValidationError("Hum receipt input identity is inconsistent.")
        if audition["original_linear_gain"] != 1.0:
            raise ProjectValidationError("Hum receipt original gain must be exactly 1.0.")
        expected_linear_gain = 10.0 ** (float(audition["proposed_gain_db"]) / 20.0)
        if not math.isclose(
            float(audition["proposed_linear_gain"]),
            expected_linear_gain,
            rel_tol=1e-10,
            abs_tol=1e-12,
        ):
            raise ProjectValidationError("Hum receipt gain dB and linear value disagree.")
        if audition["residue_monitor_linear_gain"] != (parsed_recipe.config.residue_monitor_gain):
            raise ProjectValidationError("Hum receipt residue gain is inconsistent.")
        if (
            abs(float(audition["proposed_gain_db"])) > parsed_recipe.config.maximum_match_gain_db
            or float(audition["match_mad_db"]) > parsed_recipe.config.maximum_match_mad_db
        ):
            raise ProjectValidationError("Hum receipt match exceeds recipe bounds.")
    parsed_metrics: list[dict[str, Any]] = []
    for channel, raw_metric in enumerate(metrics):
        metric = _object(raw_metric, "Hum receipt channel metric")
        _strict_keys(
            metric,
            {
                "channel_index",
                "removed_rms_dbfs",
                "removed_peak",
                "removed_energy_ratio",
                "harmonics",
            },
            "Hum receipt channel metric",
        )
        if metric["channel_index"] != channel:
            raise ProjectValidationError("Hum receipt channels are unordered.")
        _number(metric["removed_rms_dbfs"], "Hum removed RMS", -400.0, 1.0)
        _number(metric["removed_peak"], "Hum removed peak", 0.0, 1.0)
        _number(metric["removed_energy_ratio"], "Hum removed energy ratio", 0.0, 1.0)
        harmonics = _array(metric["harmonics"], "Hum receipt harmonics")
        if expected_harmonics is None:
            harmonic_ids = tuple(
                _integer(item["harmonic"], "Hum receipt harmonic", 1, 4)
                for item in harmonics
                if isinstance(item, dict)
            )
            if len(harmonic_ids) != len(harmonics):
                raise ProjectValidationError("Hum receipt harmonic metrics are malformed.")
            if (
                harmonic_ids != tuple(sorted(set(harmonic_ids)))
                or 1 not in harmonic_ids
                or len(harmonic_ids) < 2
            ):
                raise ProjectValidationError(
                    "Hum receipt harmonic metrics are incomplete or duplicated."
                )
        else:
            harmonic_ids = expected_harmonics
        if len(harmonics) != len(harmonic_ids):
            raise ProjectValidationError("Hum receipt harmonic count is inconsistent.")
        parsed_harmonics = [
            _validate_harmonic_metric(raw_harmonic, harmonic)
            for raw_harmonic, harmonic in zip(harmonics, harmonic_ids, strict=True)
        ]
        if parsed_recipe is not None:
            for harmonic_metric, harmonic in zip(
                parsed_harmonics,
                parsed_recipe.harmonics,
                strict=True,
            ):
                if harmonic_metric["frequency_hz"] != (parsed_recipe.fundamental_hz * harmonic):
                    raise ProjectValidationError("Hum receipt harmonic frequency is inconsistent.")
        parsed_metrics.append(
            {
                "channel_index": channel,
                "removed_rms_dbfs": metric["removed_rms_dbfs"],
                "removed_peak": metric["removed_peak"],
                "removed_energy_ratio": metric["removed_energy_ratio"],
                "harmonics": parsed_harmonics,
            }
        )
    if parsed_recipe is not None:
        _validate_metrics(parsed_metrics, parsed_recipe)
    aggregate = _object(data["aggregate"], "Hum receipt aggregate")
    _strict_keys(
        aggregate,
        {
            "maximum_removed_peak",
            "maximum_removed_energy_ratio",
            "maximum_retained_line_ratio",
            "maximum_coefficient_relative_spread",
        },
        "Hum receipt aggregate",
    )
    for key in aggregate:
        _number(aggregate[key], f"Hum receipt aggregate {key}", 0.0, 1_000.0)
    retained = [
        float(harmonic["retained_residual_ratio"])
        for metric in parsed_metrics
        for harmonic in metric["harmonics"]
    ]
    spreads = [
        float(harmonic["coefficient_relative_spread"])
        for metric in parsed_metrics
        for harmonic in metric["harmonics"]
    ]
    exact_aggregate = {
        "maximum_removed_peak": max(float(metric["removed_peak"]) for metric in parsed_metrics),
        "maximum_removed_energy_ratio": max(
            float(metric["removed_energy_ratio"]) for metric in parsed_metrics
        ),
        "maximum_retained_line_ratio": max(retained),
        "maximum_coefficient_relative_spread": max(spreads),
    }
    if aggregate != exact_aggregate:
        raise ProjectValidationError("Hum receipt aggregate does not match its metrics.")
    proof = _object(data["proof"], "Hum receipt proof")
    proof_keys = {
        "source_array_immutable",
        "original_matches_input",
        "outside_scope_proposed_bit_identical",
        "outside_scope_removed_zero",
        "raw_algebra_float64_bounded",
        "edge_residue_starts_and_ends_at_zero",
        "proposed_does_not_clip",
        "audition_gains_do_not_clip",
    }
    _strict_keys(proof, proof_keys, "Hum receipt proof")
    if any(proof[key] is not True for key in proof_keys):
        raise ProjectValidationError("Hum preview receipt proof must be wholly true.")
    policy = _object(data["policy"], "Hum receipt policy")
    expected_policy = _receipt_policy()
    _strict_keys(policy, set(expected_policy), "Hum receipt policy")
    if policy != expected_policy:
        raise ProjectValidationError("Hum preview receipt protections are mandatory.")
    if render_manifest is not None:
        parsed_render = validate_hum_preview_render_manifest(
            dict(render_manifest),
            recipe=parsed_recipe,
        )
        if render_sha != parsed_render["render_body_sha256"]:
            raise ProjectValidationError("Hum receipt belongs to a different render.")
        for field in ("input", "selected_scope", "raw_arrays", "audition"):
            if data[field] != parsed_render[field]:
                raise ProjectValidationError(
                    f"Hum receipt {field} differs from its render manifest."
                )
    if arrays is not None:
        if len(arrays) != 3:
            raise ProjectValidationError("Hum receipt array proof requires three arrays.")
        original, was_original_mono = _normalize_pcm(arrays[0])
        proposed, was_proposed_mono = _normalize_pcm(arrays[1])
        removed, was_removed_mono = _normalize_pcm(arrays[2])
        if not (
            was_original_mono == was_proposed_mono == was_removed_mono
            and original.shape == proposed.shape == removed.shape
            and original.shape == (sample_count, channels)
        ):
            raise ProjectValidationError("Hum receipt arrays have inconsistent geometry.")
        if (
            _pcm_sha256(original) != raw_arrays["original_sha256"]
            or _pcm_sha256(proposed) != raw_arrays["proposed_sha256"]
            or _pcm_sha256(removed) != raw_arrays["removed_sha256"]
            or _pcm_sha256(original) != input_identity["normalized_pcm_sha256"]
        ):
            raise ProjectValidationError("Hum receipt arrays do not match their hashes.")
        reconstruction_error = float(np.max(np.abs(original - (proposed + removed))))
        if _quantize(reconstruction_error) != raw_arrays["maximum_reconstruction_error"]:
            raise ProjectValidationError("Hum receipt array algebra does not match its report.")
        if (
            not np.array_equal(
                proposed[: scope.start_sample],
                original[: scope.start_sample],
            )
            or not np.array_equal(
                proposed[scope.end_sample_exclusive :],
                original[scope.end_sample_exclusive :],
            )
            or bool(np.any(removed[: scope.start_sample]))
            or bool(np.any(removed[scope.end_sample_exclusive :]))
        ):
            raise ProjectValidationError("Hum receipt arrays violate scope isolation.")
        if (
            bool(np.any(removed[scope.start_sample]))
            or bool(np.any(removed[scope.end_sample_exclusive - 1]))
            or float(np.max(np.abs(proposed))) * float(audition["proposed_linear_gain"]) > 1.0
            or float(np.max(np.abs(removed))) * float(audition["residue_monitor_linear_gain"]) > 1.0
        ):
            raise ProjectValidationError("Hum receipt arrays violate edge or gain bounds.")
    return data


def render_hum_preview(
    samples: np.ndarray,
    proposal_value: ContinuousNoiseProposalDocument | Mapping[str, Any],
    recipe_value: HumPreviewRecipe | Mapping[str, Any],
) -> HumPreviewResult:
    """Render one immutable raw-array preview or fail closed."""

    if type(samples) is not np.ndarray:
        raise ProjectValidationError("Hum preview PCM must be a NumPy array.")
    source_before = hashlib.sha256(samples.tobytes(order="A")).hexdigest()
    pcm, was_mono = _normalize_pcm(samples)
    proposal = _strict_current_proposal(proposal_value)
    recipe = (
        recipe_value
        if isinstance(recipe_value, HumPreviewRecipe)
        else HumPreviewRecipe.from_dict(dict(recipe_value))
    )
    recipe = HumPreviewRecipe.from_dict(recipe.to_dict())
    if recipe.algorithm["module_sha256"] != _current_preview_module_sha256():
        raise ProjectValidationError("Hum preview recipe module identity is stale.")
    if recipe.algorithm["numpy_version"] != np.__version__:
        raise ProjectValidationError("Hum preview recipe NumPy identity is stale.")
    if recipe.proposal_identity["proposal_body_sha256"] != proposal.proposal_body_sha256:
        raise ProjectValidationError("Hum preview recipe belongs to a stale proposal.")
    if recipe.proposal_identity["hum_body_sha256"] != canonical_json_sha256(proposal.hum.to_dict()):
        raise ProjectValidationError("Hum preview recipe hum identity is stale.")
    if recipe.proposal_identity["analysis_module_sha256"] != proposal.algorithm["module_sha256"]:
        raise ProjectValidationError("Hum preview recipe analysis identity is stale.")
    if recipe.proposal_identity["analysis_config_sha256"] != proposal.algorithm["config_sha256"]:
        raise ProjectValidationError("Hum preview recipe analysis config is stale.")
    if recipe.selected_scope != proposal.scope:
        raise ProjectValidationError("Hum preview recipe scope is stale.")
    if recipe.fundamental_hz != proposal.hum.fundamental_hz or recipe.harmonics != (
        proposal.hum.detected_harmonics
    ):
        raise ProjectValidationError("Hum preview recipe target is stale.")
    if (
        pcm.shape[0] != proposal.sample_count
        or pcm.shape[1] != proposal.channel_count
        or recipe.input_identity
        != {
            "sample_rate": proposal.sample_rate,
            "sample_count": proposal.sample_count,
            "channel_count": proposal.channel_count,
        }
    ):
        raise ProjectValidationError("Hum preview input geometry is stale.")
    input_sha256 = _pcm_sha256(pcm)
    if (
        input_sha256 != proposal.normalized_pcm_sha256
        or input_sha256 != recipe.proposal_identity["normalized_pcm_sha256"]
    ):
        raise ProjectValidationError("Hum preview PCM does not match the reviewed proposal.")
    if bool(np.any(np.abs(pcm) >= proposal.config.clipping_amplitude)):
        raise ProjectValidationError("Hum preview refuses clipped source PCM.")

    frequencies = np.asarray(
        [recipe.fundamental_hz * harmonic for harmonic in recipe.harmonics],
        dtype=np.float64,
    )
    aggregate_coefficients, all_coefficients = _fit_reference_coefficients(
        pcm,
        proposal,
        frequencies,
    )
    scope = recipe.selected_scope
    scope_length = scope.end_sample_exclusive - scope.start_sample
    fade_samples = round(proposal.sample_rate * recipe.config.edge_fade_ms / 1_000)
    envelope = _edge_envelope(scope_length, fade_samples)
    indexes = np.arange(
        scope.start_sample,
        scope.end_sample_exclusive,
        dtype=np.float64,
    )
    model = np.zeros((scope_length, pcm.shape[1]), dtype=np.float64)
    for frequency_index, frequency in enumerate(frequencies):
        phase = 2.0 * np.pi * float(frequency) * indexes / proposal.sample_rate
        cosine = np.cos(phase)
        sine = np.sin(phase)
        for channel in range(pcm.shape[1]):
            model[:, channel] += (
                aggregate_coefficients[channel, frequency_index * 2] * cosine
                + aggregate_coefficients[channel, frequency_index * 2 + 1] * sine
            )
    raw_original = pcm.copy()
    raw_proposed = pcm.copy()
    raw_proposed[scope.start_sample : scope.end_sample_exclusive] -= model * envelope[:, np.newaxis]
    raw_removed = raw_original - raw_proposed
    if not bool(np.all(np.isfinite(raw_proposed))):
        raise ProjectValidationError("Hum preview produced nonfinite PCM.")
    if bool(np.any(np.abs(raw_proposed) > 1.0)):
        raise ProjectValidationError("Hum preview proposed PCM would clip.")
    metrics = _line_metrics(
        raw_original,
        raw_proposed,
        proposal,
        frequencies,
        aggregate_coefficients,
        all_coefficients,
    )
    _validate_metrics(metrics, recipe)
    audition = _audition_gains(raw_original, raw_proposed, proposal, recipe.config)
    proposed_monitor_peak = float(np.max(np.abs(raw_proposed))) * float(
        audition["proposed_linear_gain"]
    )
    residue_monitor_peak = float(np.max(np.abs(raw_removed))) * float(
        audition["residue_monitor_linear_gain"]
    )
    if proposed_monitor_peak > 1.0 or residue_monitor_peak > 1.0:
        raise ProjectValidationError("Hum preview audition gain would clip.")

    before = slice(0, scope.start_sample)
    after = slice(scope.end_sample_exclusive, pcm.shape[0])
    outside_proposed = np.array_equal(
        raw_proposed[before], raw_original[before]
    ) and np.array_equal(raw_proposed[after], raw_original[after])
    outside_removed = not bool(np.any(raw_removed[before])) and not bool(np.any(raw_removed[after]))
    reconstruction_error = float(np.max(np.abs(raw_original - (raw_proposed + raw_removed))))
    algebra_tolerance = float(
        np.finfo(np.float64).eps * 2.0 * max(1.0, float(np.max(np.abs(raw_original))))
    )
    edge_zero = not bool(np.any(raw_removed[scope.start_sample])) and not bool(
        np.any(raw_removed[scope.end_sample_exclusive - 1])
    )
    source_after = hashlib.sha256(samples.tobytes(order="A")).hexdigest()
    proof = {
        "source_array_immutable": source_before == source_after,
        "original_matches_input": _pcm_sha256(raw_original) == input_sha256,
        "outside_scope_proposed_bit_identical": outside_proposed,
        "outside_scope_removed_zero": outside_removed,
        "raw_algebra_float64_bounded": reconstruction_error <= algebra_tolerance,
        "edge_residue_starts_and_ends_at_zero": edge_zero,
        "proposed_does_not_clip": not bool(np.any(np.abs(raw_proposed) > 1.0)),
        "audition_gains_do_not_clip": proposed_monitor_peak <= 1.0 and residue_monitor_peak <= 1.0,
    }
    if any(value is not True for value in proof.values()):
        raise ProjectValidationError("Hum preview could not prove immutable bounded algebra.")
    retained = [
        float(harmonic["retained_residual_ratio"])
        for metric in metrics
        for harmonic in metric["harmonics"]
    ]
    spreads = [
        float(harmonic["coefficient_relative_spread"])
        for metric in metrics
        for harmonic in metric["harmonics"]
    ]
    algorithm_identity = {
        "id": HUM_PREVIEW_ALGORITHM_ID,
        "module": HUM_PREVIEW_MODULE_ID,
        "module_sha256": _current_preview_module_sha256(),
        "numpy_version": np.__version__,
    }
    input_identity = {
        "sample_rate": proposal.sample_rate,
        "sample_count": proposal.sample_count,
        "channel_count": proposal.channel_count,
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
        "schema": HUM_PREVIEW_RENDER_SCHEMA,
        "recipe_body_sha256": recipe.recipe_body_sha256,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "algorithm": algorithm_identity,
        "input": input_identity,
        "selected_scope": scope.to_dict(),
        "raw_arrays": raw_array_identity,
        "audition": audition,
        "policy": _render_policy(),
    }
    render_manifest = dict(render_body)
    render_manifest["render_body_sha256"] = canonical_json_sha256(render_body)
    render_manifest = validate_hum_preview_render_manifest(
        render_manifest,
        recipe=recipe,
    )
    receipt_body: dict[str, Any] = {
        "schema": HUM_PREVIEW_RECEIPT_SCHEMA,
        "render_body_sha256": render_manifest["render_body_sha256"],
        "recipe_body_sha256": recipe.recipe_body_sha256,
        "proposal_body_sha256": proposal.proposal_body_sha256,
        "algorithm": algorithm_identity,
        "input": input_identity,
        "selected_scope": scope.to_dict(),
        "raw_arrays": raw_array_identity,
        "audition": audition,
        "channel_metrics": metrics,
        "aggregate": {
            "maximum_removed_peak": max(float(item["removed_peak"]) for item in metrics),
            "maximum_removed_energy_ratio": max(
                float(item["removed_energy_ratio"]) for item in metrics
            ),
            "maximum_retained_line_ratio": max(retained),
            "maximum_coefficient_relative_spread": max(spreads),
        },
        "proof": proof,
        "policy": _receipt_policy(),
    }
    receipt = dict(receipt_body)
    receipt["receipt_body_sha256"] = canonical_json_sha256(receipt_body)
    original_output = raw_original[:, 0].copy() if was_mono else raw_original.copy()
    proposed_output = raw_proposed[:, 0].copy() if was_mono else raw_proposed.copy()
    removed_output = raw_removed[:, 0].copy() if was_mono else raw_removed.copy()
    receipt = validate_hum_preview_receipt(
        receipt,
        recipe=recipe,
        render_manifest=render_manifest,
        arrays=(original_output, proposed_output, removed_output),
    )
    original_output.setflags(write=False)
    proposed_output.setflags(write=False)
    removed_output.setflags(write=False)
    return HumPreviewResult(
        original=original_output,
        proposed=proposed_output,
        removed=removed_output,
        render_manifest=render_manifest,
        receipt=receipt,
    )
