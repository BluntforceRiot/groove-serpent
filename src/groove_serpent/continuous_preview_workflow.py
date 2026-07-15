"""Persistent review workflow for bounded hum, rumble, hiss, and crackle previews.

The numerical foundations in :mod:`hum_preview`, :mod:`rumble_preview`,
:mod:`hiss_preview`, and :mod:`crackle_preview` deliberately accept only
in-memory arrays.  This module is
the auditable file boundary around those foundations.  It snapshots one
project-bound source, decodes one explicitly bounded sample range, records the
owner's noise-only reference assertions, and persists proposal, rejection, or
Original/Proposed/Removed audition receipts beside the project.

Nothing here can edit a project, apply restoration, overwrite source audio, or
authorize publication.  A review attestation requests an audition preview; it
is not proof that a person listened or approved the result.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import struct
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

import numpy as np

from . import __version__
from .album import project_speed_state
from .atomic_create import rename_no_replace
from .audio_snapshot import VerifiedAudioSnapshot, verified_audio_snapshot
from .continuous_noise import (
    CONTINUOUS_NOISE_DOCUMENT_SCHEMA,
    ContinuousNoiseConfig,
    ContinuousNoiseProposalDocument,
    NoiseAnalysisScope,
    NoiseReferenceRegion,
    analyze_continuous_noise,
)
from .crackle_preview import (
    CRACKLE_PREVIEW_RECEIPT_SCHEMA,
    CRACKLE_PREVIEW_RECIPE_SCHEMA,
    CRACKLE_PREVIEW_RENDER_SCHEMA,
    CRACKLE_PROPOSAL_SCHEMA,
    CRACKLE_REVIEW_ATTESTATION_SCHEMA,
    CrackleAnalysisConfig,
    CracklePreviewConfig,
    CracklePreviewRecipe,
    CrackleProposal,
    REVIEW_ACKNOWLEDGEMENT as CRACKLE_REVIEW_ACKNOWLEDGEMENT,
    analyze_crackle,
    create_crackle_preview_recipe,
    render_crackle_preview,
    validate_crackle_preview_receipt,
    validate_crackle_preview_render_manifest,
)
from .errors import GrooveSerpentError, ProjectValidationError
from .hiss_preview import (
    HISS_PREVIEW_RECEIPT_SCHEMA,
    HISS_PREVIEW_RECIPE_SCHEMA,
    HISS_PREVIEW_RENDER_SCHEMA,
    HISS_PROPOSAL_SCHEMA,
    HISS_REVIEW_ATTESTATION_SCHEMA,
    HissAnalysisConfig,
    HissPreviewConfig,
    HissPreviewRecipe,
    HissProposal,
    analyze_hiss,
    create_hiss_preview_recipe,
    render_hiss_preview,
    REVIEW_ACKNOWLEDGEMENT as HISS_REVIEW_ACKNOWLEDGEMENT,
    validate_hiss_preview_receipt,
    validate_hiss_preview_render_manifest,
)
from .hum_preview import (
    HUM_PREVIEW_RECEIPT_SCHEMA,
    HUM_PREVIEW_RECIPE_SCHEMA,
    HUM_PREVIEW_RENDER_SCHEMA,
    HUM_REVIEW_ATTESTATION_SCHEMA,
    HumPreviewConfig,
    HumPreviewRecipe,
    REVIEW_ACKNOWLEDGEMENT as HUM_REVIEW_ACKNOWLEDGEMENT,
    create_hum_preview_recipe,
    render_hum_preview,
    validate_hum_preview_receipt,
    validate_hum_preview_render_manifest,
)
from .media import find_tool, sha256_file, tool_version
from .models import Project, resolve_source_path, utc_now_iso
from .project_io import decode_project_json, load_project_with_sha256
from .publication import canonical_json_sha256
from .rumble_preview import (
    RUMBLE_PREVIEW_RECEIPT_SCHEMA,
    RUMBLE_PREVIEW_RECIPE_SCHEMA,
    RUMBLE_PREVIEW_RENDER_SCHEMA,
    RUMBLE_REVIEW_ATTESTATION_SCHEMA,
    RumblePreviewConfig,
    RumblePreviewRecipe,
    REVIEW_ACKNOWLEDGEMENT as RUMBLE_REVIEW_ACKNOWLEDGEMENT,
    create_rumble_preview_recipe,
    render_rumble_preview,
    validate_rumble_preview_receipt,
    validate_rumble_preview_render_manifest,
)
from .subprocess_policy import require_ffmpeg_nostdin
from .subprocess_policy import run_bounded_capture


ContinuousKind = Literal["hum", "rumble", "hiss", "crackle"]

CONTINUOUS_PROPOSAL_SCHEMA = "groove-serpent.continuous-preview-proposal/1"
CONTINUOUS_DECISION_SCHEMA = "groove-serpent.continuous-preview-decision/1"
CONTINUOUS_PREVIEW_SCHEMA = "groove-serpent.continuous-preview-receipt/1"
CONTINUOUS_CATALOG_SCHEMA = "groove-serpent.continuous-preview-catalog/1"
CONTINUOUS_EXPECTED_SCHEMA = "groove-serpent.continuous-preview-expected-context/1"
CONTINUOUS_ATTESTATION_SCHEMA = "groove-serpent.continuous-preview-attestation/1"
CONTINUOUS_WORKFLOW_ID = "groove-serpent.bounded-continuous-preview-workflow/1"
CONTINUOUS_REVIEW_DECISION = "request_owner_audition_preview"
CONTINUOUS_REJECTION_DECISION = "reject_proposal_without_applying"
CONTINUOUS_REVIEW_ACKNOWLEDGEMENT = (
    "caller_attestation_is_not_proof_of_human_audition_or_restoration_approval"
)

MAX_SCOPE_FRAMES = 6_000_000
MAX_PCM_VALUES = 12_000_000
MAX_SCOPE_SECONDS = 90.0
MAX_REFERENCE_REGIONS = 64
MAX_CATALOG_ENTRIES = 512
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 64 * 1024

_DIGEST_CHARS = frozenset("0123456789abcdef")
_REFERENCE_ROLES = frozenset({"lead_in", "lead_out", "inter_track", "user_selected"})


@dataclass(frozen=True, slots=True)
class ContinuousMethodContract:
    """Exact schema and authority namespace for one registered processor."""

    kind: ContinuousKind
    proposal_schema: str
    recipe_schema: str
    render_schema: str
    receipt_schema: str
    authority_profile: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "proposal_schema": self.proposal_schema,
            "recipe_schema": self.recipe_schema,
            "render_schema": self.render_schema,
            "receipt_schema": self.receipt_schema,
            "authority_profile": self.authority_profile,
        }


# This explicit table is the extension point for a future independently designed
# processor.  Registering a method requires its own proposal/recipe/render/receipt
# schemas and authority profile; no method may inherit another method's evidence.
CONTINUOUS_METHOD_REGISTRY: dict[str, ContinuousMethodContract] = {
    "hum": ContinuousMethodContract(
        kind="hum",
        proposal_schema=CONTINUOUS_NOISE_DOCUMENT_SCHEMA,
        recipe_schema=HUM_PREVIEW_RECIPE_SCHEMA,
        render_schema=HUM_PREVIEW_RENDER_SCHEMA,
        receipt_schema=HUM_PREVIEW_RECEIPT_SCHEMA,
        authority_profile="stationary_hum_owner_audition_only",
    ),
    "rumble": ContinuousMethodContract(
        kind="rumble",
        proposal_schema=CONTINUOUS_NOISE_DOCUMENT_SCHEMA,
        recipe_schema=RUMBLE_PREVIEW_RECIPE_SCHEMA,
        render_schema=RUMBLE_PREVIEW_RENDER_SCHEMA,
        receipt_schema=RUMBLE_PREVIEW_RECEIPT_SCHEMA,
        authority_profile="stationary_rumble_owner_audition_only",
    ),
    "hiss": ContinuousMethodContract(
        kind="hiss",
        proposal_schema=HISS_PROPOSAL_SCHEMA,
        recipe_schema=HISS_PREVIEW_RECIPE_SCHEMA,
        render_schema=HISS_PREVIEW_RENDER_SCHEMA,
        receipt_schema=HISS_PREVIEW_RECEIPT_SCHEMA,
        authority_profile="stationary_broadband_hiss_owner_audition_only",
    ),
    "crackle": ContinuousMethodContract(
        kind="crackle",
        proposal_schema=CRACKLE_PROPOSAL_SCHEMA,
        recipe_schema=CRACKLE_PREVIEW_RECIPE_SCHEMA,
        render_schema=CRACKLE_PREVIEW_RENDER_SCHEMA,
        receipt_schema=CRACKLE_PREVIEW_RECEIPT_SCHEMA,
        authority_profile="bounded_continuous_crackle_owner_audition_only",
    ),
}
_KINDS = frozenset(CONTINUOUS_METHOD_REGISTRY)


@dataclass(frozen=True, slots=True)
class ReviewedNoiseReference:
    """One exact source interval the owner marked as noise-only evidence."""

    label: str
    role: Literal["lead_in", "lead_out", "inter_track", "user_selected"]
    start_sample: int
    end_sample_exclusive: int
    owner_attested_noise_only: bool

    def validate(self, start: int, end: int) -> None:
        _text(self.label, "Noise-reference label", 128)
        if self.role not in _REFERENCE_ROLES:
            raise ProjectValidationError("Noise-reference role is unsupported.")
        _integer(self.start_sample, "Noise-reference start", start, end - 1)
        _integer(self.end_sample_exclusive, "Noise-reference end", start + 1, end)
        if self.end_sample_exclusive <= self.start_sample:
            raise ProjectValidationError("Noise-reference interval is empty.")
        if self.owner_attested_noise_only is not True:
            raise ProjectValidationError(
                "Every continuous-noise reference requires an explicit owner noise-only assertion."
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "role": self.role,
            "start_sample": self.start_sample,
            "end_sample_exclusive": self.end_sample_exclusive,
            "owner_attested_noise_only": True,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, scope_start: int, scope_end: int
    ) -> "ReviewedNoiseReference":
        data = _object(value, "Noise reference")
        _strict_keys(
            data,
            {
                "label",
                "role",
                "start_sample",
                "end_sample_exclusive",
                "owner_attested_noise_only",
            },
            "Noise reference",
        )
        result = cls(
            label=_text(data["label"], "Noise-reference label", 128),
            role=cast(
                Literal["lead_in", "lead_out", "inter_track", "user_selected"],
                data["role"],
            ),
            start_sample=_integer(
                data["start_sample"], "Noise-reference start", scope_start, scope_end - 1
            ),
            end_sample_exclusive=_integer(
                data["end_sample_exclusive"],
                "Noise-reference end",
                scope_start + 1,
                scope_end,
            ),
            owner_attested_noise_only=data["owner_attested_noise_only"],
        )
        result.validate(scope_start, scope_end)
        return result


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
        raise ProjectValidationError(
            f"{label} fields are invalid (missing={sorted(expected - set(value))}, "
            f"extra={sorted(set(value) - expected)})."
        )


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProjectValidationError(
            f"{label} must be a JSON integer between {minimum} and {maximum}."
        )
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProjectValidationError(f"{label} must be a finite JSON number.")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
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
        raise ProjectValidationError(f"{label} must be bounded, trimmed printable text.")
    return value


def _digest(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST_CHARS for character in value)
    ):
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return value


def _kind(value: Any) -> ContinuousKind:
    if value not in _KINDS:
        raise ProjectValidationError(
            "Continuous preview kind must be hum, rumble, hiss, or crackle."
        )
    return cast(ContinuousKind, value)


def _method_contract(kind: ContinuousKind) -> ContinuousMethodContract:
    return CONTINUOUS_METHOD_REGISTRY[kind]


def _module_sha256(module_file: str | None, label: str) -> str:
    if not module_file:
        raise ProjectValidationError(f"{label} has no verifiable filesystem identity.")
    return sha256_file(Path(module_file))


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )


def _workflow_module_sha256() -> str:
    return _module_sha256(__file__, "Continuous preview workflow module")


def _authority(kind: ContinuousKind) -> dict[str, Any]:
    return {
        "method_profile": _method_contract(kind).authority_profile,
        "automatic_application_forbidden": True,
        "automatic_publication_forbidden": True,
        "may_edit_project": False,
        "may_modify_source_audio": False,
        "may_claim_quality_neutrality": False,
        "owner_audition_required": True,
    }


def _catalog_authority() -> dict[str, bool]:
    return {
        "automatic_application_forbidden": True,
        "automatic_publication_forbidden": True,
        "may_edit_project": False,
        "may_modify_source_audio": False,
        "may_claim_quality_neutrality": False,
        "owner_audition_required": True,
    }


def _workspace_for(project_path: Path) -> Path:
    stem = "-".join(
        part
        for part in "".join(
            character if character.isascii() and character.isalnum() else "-"
            for character in project_path.stem
        ).split("-")
        if part
    )[:80] or "project"
    root = project_path.parent.resolve()
    workspace = root / ".groove-serpent" / "continuous-preview" / stem
    if workspace.resolve() != workspace:
        raise ProjectValidationError(
            "Continuous-preview workspace may not traverse a symlink or reparse point."
        )
    try:
        workspace.relative_to(root)
    except ValueError as exc:
        raise ProjectValidationError(
            "Continuous-preview workspace left the project folder."
        ) from exc
    return workspace


def continuous_preview_workspace(project_path: Path | str) -> Path:
    """Return the deterministic, project-contained persistence directory."""

    return _workspace_for(Path(project_path).expanduser().resolve())


def _config_identity(kind: ContinuousKind) -> dict[str, Any]:
    if kind in {"hum", "rumble"}:
        analysis = ContinuousNoiseConfig().to_dict()
    elif kind == "hiss":
        analysis = HissAnalysisConfig().to_dict()
    else:
        analysis = CrackleAnalysisConfig().to_dict()
    if kind == "hum":
        preview = HumPreviewConfig().to_dict()
    elif kind == "rumble":
        preview = RumblePreviewConfig().to_dict()
    elif kind == "hiss":
        preview = HissPreviewConfig().to_dict()
    else:
        preview = CracklePreviewConfig().to_dict()
    return {
        "analysis": analysis,
        "analysis_sha256": canonical_json_sha256(analysis),
        "preview": preview,
        "preview_sha256": canonical_json_sha256(preview),
    }


def _foundation_module_identity(kind: ContinuousKind) -> dict[str, str]:
    from . import continuous_noise as continuous_module
    from . import crackle_preview as crackle_module
    from . import hiss_preview as hiss_module
    from . import hum_preview as hum_module
    from . import rumble_preview as rumble_module

    analysis_file = {
        "hum": continuous_module.__file__,
        "rumble": continuous_module.__file__,
        "hiss": hiss_module.__file__,
        "crackle": crackle_module.__file__,
    }[kind]
    preview_file = {
        "hum": hum_module.__file__,
        "rumble": rumble_module.__file__,
        "hiss": hiss_module.__file__,
        "crackle": crackle_module.__file__,
    }[kind]
    return {
        "analysis_module_sha256": _module_sha256(analysis_file, "Analysis module"),
        "preview_module_sha256": _module_sha256(preview_file, "Preview module"),
        "workflow_module_sha256": _workflow_module_sha256(),
    }


def _tool_identity() -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for name in ("ffmpeg", "ffprobe"):
        executable = Path(find_tool(name)).resolve()
        result[name] = {
            "executable_sha256": sha256_file(executable),
            "version": _text(tool_version(name), f"{name} version", 512),
        }
    return result


def _context(
    project: Project,
    project_sha256: str,
    kind: ContinuousKind,
) -> dict[str, Any]:
    speed = project_speed_state(project)
    speed_body = {
        "schema": speed.schema,
        "capture_rpm": speed.capture_rpm,
        "intended_rpm": speed.intended_rpm,
        "fine_factor": speed.fine_factor,
    }
    context_body: dict[str, Any] = {
        "schema": CONTINUOUS_EXPECTED_SCHEMA,
        "kind": kind,
        "app_version": __version__,
        "workflow_id": CONTINUOUS_WORKFLOW_ID,
        "method_contract": _method_contract(kind).to_dict(),
        "project": {
            "revision": project.revision,
            "sha256": project_sha256,
            "state_sha256": project.state_sha256,
        },
        "source": {
            "sha256": project.source.sha256,
            "size_bytes": project.source.size_bytes,
            "sample_rate": project.source.sample_rate,
            "channels": project.source.channels,
            "sample_count": project.source.sample_count,
            "bits_per_raw_sample": project.source.bits_per_raw_sample,
            "codec_name": project.source.codec_name,
        },
        "speed": {
            "state": speed_body,
            "state_sha256": speed.sha256,
            "effective_speed_factor": speed.effective_speed_factor,
            "processing_domain": "raw_capture_before_speed_correction",
        },
        "config": _config_identity(kind),
        "tools": _tool_identity(),
        "modules": _foundation_module_identity(kind),
        "limits": {
            "maximum_scope_frames": MAX_SCOPE_FRAMES,
            "maximum_pcm_values": MAX_PCM_VALUES,
            "maximum_scope_seconds": MAX_SCOPE_SECONDS,
            "maximum_reference_regions": MAX_REFERENCE_REGIONS,
        },
    }
    context = dict(context_body)
    context["context_sha256"] = canonical_json_sha256(context_body)
    return context


def current_continuous_preview_context(
    project_path: Path | str,
    kind: ContinuousKind,
) -> dict[str, Any]:
    path = Path(project_path).expanduser().resolve()
    project, project_sha256 = load_project_with_sha256(path)
    return _context(project, project_sha256, _kind(kind))


def _validate_context(value: Any) -> dict[str, Any]:
    data = _object(value, "Continuous-preview expected context")
    expected = {
        "schema",
        "kind",
        "app_version",
        "workflow_id",
        "method_contract",
        "project",
        "source",
        "speed",
        "config",
        "tools",
        "modules",
        "limits",
        "context_sha256",
    }
    _strict_keys(data, expected, "Continuous-preview expected context")
    if data["schema"] != CONTINUOUS_EXPECTED_SCHEMA:
        raise ProjectValidationError("Continuous-preview context schema is unsupported.")
    _kind(data["kind"])
    _text(data["app_version"], "Context application version", 64)
    if data["workflow_id"] != CONTINUOUS_WORKFLOW_ID:
        raise ProjectValidationError("Continuous-preview workflow identity is unsupported.")
    if data["method_contract"] != _method_contract(_kind(data["kind"])).to_dict():
        raise ProjectValidationError("Continuous-preview method contract is unsupported.")
    _digest(data["context_sha256"], "Continuous-preview context SHA-256")
    body = dict(data)
    del body["context_sha256"]
    if canonical_json_sha256(body) != data["context_sha256"]:
        raise ProjectValidationError("Continuous-preview context identity is invalid.")
    project = _object(data["project"], "Context project")
    _strict_keys(project, {"revision", "sha256", "state_sha256"}, "Context project")
    _integer(project["revision"], "Context project revision", 1, 2**63 - 1)
    _digest(project["sha256"], "Context project SHA-256")
    _digest(project["state_sha256"], "Context project-state SHA-256")
    source = _object(data["source"], "Context source")
    _strict_keys(
        source,
        {
            "sha256",
            "size_bytes",
            "sample_rate",
            "channels",
            "sample_count",
            "bits_per_raw_sample",
            "codec_name",
        },
        "Context source",
    )
    _digest(source["sha256"], "Context source SHA-256")
    _integer(source["size_bytes"], "Context source size", 1, 2**63 - 1)
    _integer(source["sample_rate"], "Context sample rate", 8_000, 768_000)
    _integer(source["channels"], "Context channels", 1, 32)
    _integer(source["sample_count"], "Context source samples", 1, 2**63 - 1)
    if source["bits_per_raw_sample"] is not None:
        _integer(source["bits_per_raw_sample"], "Context bit depth", 1, 128)
    _text(source["codec_name"], "Context codec", 128)
    speed = _object(data["speed"], "Context speed")
    _strict_keys(
        speed,
        {"state", "state_sha256", "effective_speed_factor", "processing_domain"},
        "Context speed",
    )
    _digest(speed["state_sha256"], "Context speed-state SHA-256")
    _number(speed["effective_speed_factor"], "Context speed factor", 0.25, 2.0)
    if speed["processing_domain"] != "raw_capture_before_speed_correction":
        raise ProjectValidationError("Continuous preview must remain in the raw capture domain.")
    config = _object(data["config"], "Context config")
    _strict_keys(
        config,
        {"analysis", "analysis_sha256", "preview", "preview_sha256"},
        "Context config",
    )
    _digest(config["analysis_sha256"], "Analysis config SHA-256")
    _digest(config["preview_sha256"], "Preview config SHA-256")
    if canonical_json_sha256(config["analysis"]) != config["analysis_sha256"]:
        raise ProjectValidationError("Analysis configuration identity is invalid.")
    if canonical_json_sha256(config["preview"]) != config["preview_sha256"]:
        raise ProjectValidationError("Preview configuration identity is invalid.")
    tools = _object(data["tools"], "Context tools")
    _strict_keys(tools, {"ffmpeg", "ffprobe"}, "Context tools")
    for name in ("ffmpeg", "ffprobe"):
        tool = _object(tools[name], f"Context {name}")
        _strict_keys(tool, {"executable_sha256", "version"}, f"Context {name}")
        _digest(tool["executable_sha256"], f"Context {name} executable SHA-256")
        _text(tool["version"], f"Context {name} version", 512)
    modules = _object(data["modules"], "Context modules")
    _strict_keys(
        modules,
        {
            "analysis_module_sha256",
            "preview_module_sha256",
            "workflow_module_sha256",
        },
        "Context modules",
    )
    for key in modules:
        _digest(modules[key], f"Context {key}")
    limits = _object(data["limits"], "Context limits")
    _strict_keys(
        limits,
        {
            "maximum_scope_frames",
            "maximum_pcm_values",
            "maximum_scope_seconds",
            "maximum_reference_regions",
        },
        "Context limits",
    )
    _integer(limits["maximum_scope_frames"], "Maximum scope frames", 1, 100_000_000)
    _integer(limits["maximum_pcm_values"], "Maximum PCM values", 1, 1_000_000_000)
    _number(limits["maximum_scope_seconds"], "Maximum scope seconds", 0.1, 3_600.0)
    _integer(limits["maximum_reference_regions"], "Maximum references", 2, 1_000)
    authority_free_state = _object(speed["state"], "Context speed state")
    _strict_keys(
        authority_free_state,
        {"schema", "capture_rpm", "intended_rpm", "fine_factor"},
        "Context speed state",
    )
    return data


def _assert_current_context(project_path: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    parsed = _validate_context(dict(expected))
    current = current_continuous_preview_context(project_path, _kind(parsed["kind"]))
    if current != parsed:
        raise ProjectValidationError(
            "The project, source, speed, config, decoder tools, or restoration modules changed."
        )
    return current


def _validate_geometry(
    project: Project,
    start_sample: int,
    end_sample_exclusive: int,
    references: Sequence[ReviewedNoiseReference],
) -> None:
    sample_count = project.source.sample_count
    if type(sample_count) is not int or sample_count <= 0:
        raise ProjectValidationError("Continuous previews require an exact source sample count.")
    _integer(start_sample, "Scope start", 0, sample_count - 1)
    _integer(end_sample_exclusive, "Scope end", 1, sample_count)
    if end_sample_exclusive <= start_sample:
        raise ProjectValidationError("Continuous-preview scope is empty.")
    frames = end_sample_exclusive - start_sample
    if frames > MAX_SCOPE_FRAMES:
        raise ProjectValidationError(
            f"Continuous-preview scope exceeds the {MAX_SCOPE_FRAMES}-frame limit."
        )
    if frames * project.source.channels > MAX_PCM_VALUES:
        raise ProjectValidationError(
            f"Continuous-preview scope exceeds the {MAX_PCM_VALUES}-value PCM limit."
        )
    if frames / project.source.sample_rate > MAX_SCOPE_SECONDS:
        raise ProjectValidationError(
            f"Continuous-preview scope exceeds the {MAX_SCOPE_SECONDS:g}-second limit."
        )
    if not 2 <= len(references) <= MAX_REFERENCE_REGIONS:
        raise ProjectValidationError(
            f"Continuous preview requires 2 to {MAX_REFERENCE_REGIONS} reviewed references."
        )
    previous_end = start_sample
    labels: set[str] = set()
    for reference in references:
        reference.validate(start_sample, end_sample_exclusive)
        folded = reference.label.casefold()
        if folded in labels:
            raise ProjectValidationError("Noise-reference labels must be unique.")
        labels.add(folded)
        if reference.start_sample < previous_end:
            raise ProjectValidationError("Noise references must be ordered and non-overlapping.")
        previous_end = reference.end_sample_exclusive
    total = sum(item.end_sample_exclusive - item.start_sample for item in references)
    if total >= frames:
        raise ProjectValidationError("Noise references must leave program audio to compare.")


def _decode_scope(
    snapshot: VerifiedAudioSnapshot,
    project: Project,
    start_sample: int,
    end_sample_exclusive: int,
) -> np.ndarray:
    """Decode exactly one bounded source-frame interval as little-endian float64."""

    frames = end_sample_exclusive - start_sample
    channels = project.source.channels
    expected_bytes = frames * channels * 8
    command = require_ffmpeg_nostdin(
        [
            find_tool("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(snapshot.path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            (
                f"atrim=start_sample={start_sample}:"
                f"end_sample={end_sample_exclusive},asetpts=PTS-STARTPTS"
            ),
            "-ac",
            str(channels),
            "-ar",
            str(project.source.sample_rate),
            "-f",
            "f64le",
            "-acodec",
            "pcm_f64le",
            "-fs",
            str(expected_bytes),
            "pipe:1",
        ]
    )
    completed = run_bounded_capture(
        command,
        stdout_limit=expected_bytes,
        stderr_limit=MAX_DIAGNOSTIC_BYTES,
        timeout=300.0,
    )
    stdout = completed.stdout
    if completed.stdout_truncated:
        raise ProjectValidationError("FFmpeg returned more PCM than the bounded request allowed.")
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GrooveSerpentError(f"FFmpeg could not decode the continuous preview: {message}")
    if len(stdout) != expected_bytes:
        raise ProjectValidationError(
            f"FFmpeg decoded {len(stdout)} bytes; exactly {expected_bytes} were expected."
        )
    values = np.frombuffer(stdout, dtype="<f8")
    pcm = np.ascontiguousarray(values.reshape(frames, channels), dtype="<f8")
    if not bool(np.all(np.isfinite(pcm))) or bool(np.any(np.abs(pcm) > 1.0)):
        raise ProjectValidationError("Decoded continuous-preview PCM is nonfinite or unclamped.")
    return pcm


def _local_geometry(
    start_sample: int,
    end_sample_exclusive: int,
    references: Sequence[ReviewedNoiseReference],
) -> tuple[NoiseAnalysisScope, tuple[NoiseReferenceRegion, ...]]:
    frames = end_sample_exclusive - start_sample
    scope = NoiseAnalysisScope("reviewed_scope", 0, frames)
    local = tuple(
        NoiseReferenceRegion(
            label=item.label,
            role=item.role,
            start_sample=item.start_sample - start_sample,
            end_sample_exclusive=item.end_sample_exclusive - start_sample,
        )
        for item in references
    )
    return scope, local


def _proposal_status(kind: ContinuousKind, foundation: Mapping[str, Any]) -> str:
    if kind in {"hiss", "crackle"}:
        return cast(str, foundation["status"])
    branch = _object(foundation[kind], f"{kind} proposal")
    return cast(str, branch["status"])


def _selection_dict(
    start: int,
    end: int,
    references: Sequence[ReviewedNoiseReference],
) -> dict[str, Any]:
    body = {
        "source_start_sample": start,
        "source_end_sample_exclusive": end,
        "owner_attested_scope_reviewed": True,
        "references": [item.to_dict() for item in references],
    }
    body["selection_sha256"] = canonical_json_sha256(body)
    return body


def _validate_selection(value: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    data = _object(value, "Continuous-preview selection")
    _strict_keys(
        data,
        {
            "source_start_sample",
            "source_end_sample_exclusive",
            "owner_attested_scope_reviewed",
            "references",
            "selection_sha256",
        },
        "Continuous-preview selection",
    )
    source = _object(context["source"], "Context source")
    sample_count = cast(int, source["sample_count"])
    start = _integer(data["source_start_sample"], "Selection start", 0, sample_count - 1)
    end = _integer(data["source_end_sample_exclusive"], "Selection end", 1, sample_count)
    if data["owner_attested_scope_reviewed"] is not True:
        raise ProjectValidationError("Continuous-preview scope requires explicit owner review.")
    references = tuple(
        ReviewedNoiseReference.from_dict(item, scope_start=start, scope_end=end)
        for item in _array(data["references"], "Selection references")
    )
    _digest(data["selection_sha256"], "Selection SHA-256")
    body = dict(data)
    del body["selection_sha256"]
    if canonical_json_sha256(body) != data["selection_sha256"]:
        raise ProjectValidationError("Continuous-preview selection identity is invalid.")
    if end <= start or len(references) < 2:
        raise ProjectValidationError("Continuous-preview selection geometry is invalid.")
    return data


def validate_continuous_proposal(value: Any) -> dict[str, Any]:
    data = _object(value, "Continuous-preview proposal")
    _strict_keys(
        data,
        {
            "schema",
            "proposal_sha256",
            "created_at",
            "kind",
            "context",
            "selection",
            "foundation",
            "status",
            "authority",
        },
        "Continuous-preview proposal",
    )
    if data["schema"] != CONTINUOUS_PROPOSAL_SCHEMA:
        raise ProjectValidationError("Continuous-preview proposal schema is unsupported.")
    kind = _kind(data["kind"])
    _text(data["created_at"], "Proposal timestamp", 64)
    context = _validate_context(data["context"])
    if context["kind"] != kind:
        raise ProjectValidationError("Proposal kind and context disagree.")
    selection = _validate_selection(data["selection"], context)
    foundation = _object(data["foundation"], "Foundation proposal")
    if kind == "hiss":
        parsed_foundation = HissProposal.from_dict(foundation).to_dict()
    elif kind == "crackle":
        parsed_foundation = CrackleProposal.from_dict(foundation).to_dict()
    else:
        parsed_foundation = ContinuousNoiseProposalDocument.from_dict(foundation).to_dict()
    if foundation != parsed_foundation:
        raise ProjectValidationError("Foundation proposal is not canonical.")
    source = _object(context["source"], "Context source")
    frames = selection["source_end_sample_exclusive"] - selection["source_start_sample"]
    if (
        foundation["sample_rate"] != source["sample_rate"]
        or foundation["sample_count"] != frames
        or foundation["channel_count"] != source["channels"]
    ):
        raise ProjectValidationError("Foundation proposal geometry differs from its source scope.")
    local_scope, local_refs = _local_geometry(
        selection["source_start_sample"],
        selection["source_end_sample_exclusive"],
        tuple(
            ReviewedNoiseReference.from_dict(
                item,
                scope_start=selection["source_start_sample"],
                scope_end=selection["source_end_sample_exclusive"],
            )
            for item in selection["references"]
        ),
    )
    if foundation["scope"] != local_scope.to_dict() or foundation["noise_references"] != [
        item.to_dict() for item in local_refs
    ]:
        raise ProjectValidationError("Foundation proposal is not bound to the reviewed references.")
    if data["status"] != _proposal_status(kind, foundation):
        raise ProjectValidationError("Continuous-preview proposal status is inconsistent.")
    if data["authority"] != _authority(kind):
        raise ProjectValidationError("Continuous-preview authority protections are mandatory.")
    _digest(data["proposal_sha256"], "Continuous-preview proposal SHA-256")
    body = dict(data)
    del body["proposal_sha256"]
    if canonical_json_sha256(body) != data["proposal_sha256"]:
        raise ProjectValidationError("Continuous-preview proposal identity is invalid.")
    return data


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    if len(raw) > MAX_JSON_BYTES:
        raise ProjectValidationError("Continuous-preview JSON exceeds its bounded size limit.")
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        rename_no_replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return hashlib.sha256(raw).hexdigest()


def _unique_path(workspace: Path, prefix: str, *, directory: bool = False) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    if workspace.resolve() != workspace:
        raise ProjectValidationError("Continuous-preview workspace changed unexpectedly.")
    for _attempt in range(20):
        suffix = uuid.uuid4().hex
        candidate = workspace / (f"{prefix}-{suffix}" if directory else f"{prefix}-{suffix}.json")
        if not candidate.exists():
            return candidate
    raise GrooveSerpentError("Could not allocate a unique continuous-preview artifact path.")


def propose_continuous_preview(
    project_path: Path | str,
    *,
    kind: ContinuousKind,
    start_sample: int,
    end_sample_exclusive: int,
    references: Sequence[ReviewedNoiseReference],
    expected_context: Mapping[str, Any] | None = None,
    source_snapshot: VerifiedAudioSnapshot | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Persist one current proposal after exact source and owner-reference checks."""

    path = Path(project_path).expanduser().resolve()
    kind = _kind(kind)
    project, project_sha256 = load_project_with_sha256(path)
    context = _context(project, project_sha256, kind)
    if expected_context is not None and dict(expected_context) != context:
        raise ProjectValidationError("Submitted expected context is stale.")
    refs = tuple(references)
    _validate_geometry(project, start_sample, end_sample_exclusive, refs)
    source = resolve_source_path(project, path).resolve()
    owns_snapshot = source_snapshot is None
    snapshot = source_snapshot or verified_audio_snapshot(
        source,
        expected_sha256=project.source.sha256,
        expected_size_bytes=project.source.size_bytes,
        label="Continuous-preview source audio",
    )
    try:
        snapshot.assert_snapshot_unchanged(force=True)
        snapshot.assert_live_unchanged(force=True)
        pcm = _decode_scope(snapshot, project, start_sample, end_sample_exclusive)
        scope, local_refs = _local_geometry(start_sample, end_sample_exclusive, refs)
        if kind == "hiss":
            foundation = analyze_hiss(
                pcm,
                sample_rate=project.source.sample_rate,
                scope=scope,
                noise_references=local_refs,
                config=HissAnalysisConfig(),
            ).to_dict()
        elif kind == "crackle":
            foundation = analyze_crackle(
                pcm,
                sample_rate=project.source.sample_rate,
                scope=scope,
                noise_references=local_refs,
                config=CrackleAnalysisConfig(),
            ).to_dict()
        else:
            foundation = analyze_continuous_noise(
                pcm,
                sample_rate=project.source.sample_rate,
                scope=scope,
                noise_references=local_refs,
                config=ContinuousNoiseConfig(),
            ).to_dict()
        snapshot.assert_snapshot_unchanged(force=True)
        snapshot.assert_live_unchanged(force=True)
        current_project, current_sha = load_project_with_sha256(path)
        if current_sha != project_sha256 or current_project.state_sha256 != project.state_sha256:
            raise ProjectValidationError(
                "The project changed while continuous evidence was decoded."
            )
        if _context(current_project, current_sha, kind) != context:
            raise ProjectValidationError("The continuous-preview context changed during analysis.")
    finally:
        if owns_snapshot:
            snapshot.close()
    proposal_body: dict[str, Any] = {
        "schema": CONTINUOUS_PROPOSAL_SCHEMA,
        "created_at": utc_now_iso(),
        "kind": kind,
        "context": context,
        "selection": _selection_dict(start_sample, end_sample_exclusive, refs),
        "foundation": foundation,
        "status": _proposal_status(kind, foundation),
        "authority": _authority(kind),
    }
    proposal = dict(proposal_body)
    proposal["proposal_sha256"] = canonical_json_sha256(proposal_body)
    proposal = validate_continuous_proposal(proposal)
    output = _unique_path(_workspace_for(path), f"proposal-{kind}")
    _atomic_json(output, proposal)
    return output, proposal


def continuous_attestation_template(proposal_value: Mapping[str, Any]) -> dict[str, Any]:
    """Return every exact field a caller must affirm before preview rendering."""

    proposal = validate_continuous_proposal(dict(proposal_value))
    context = proposal["context"]
    expected = {
        "project_sha256": context["project"]["sha256"],
        "project_state_sha256": context["project"]["state_sha256"],
        "source_sha256": context["source"]["sha256"],
        "speed_state_sha256": context["speed"]["state_sha256"],
        "analysis_config_sha256": context["config"]["analysis_sha256"],
        "preview_config_sha256": context["config"]["preview_sha256"],
        "ffmpeg_executable_sha256": context["tools"]["ffmpeg"]["executable_sha256"],
        "ffprobe_executable_sha256": context["tools"]["ffprobe"]["executable_sha256"],
        **context["modules"],
    }
    return {
        "schema": CONTINUOUS_ATTESTATION_SCHEMA,
        "attestation_token": "REPLACE_WITH_DISTINCT_SHA256",
        "decision": CONTINUOUS_REVIEW_DECISION,
        "acknowledgement": CONTINUOUS_REVIEW_ACKNOWLEDGEMENT,
        "owner_attested_scope_reviewed": True,
        "owner_attested_references_reviewed": True,
        "proposal_sha256": proposal["proposal_sha256"],
        "context_sha256": context["context_sha256"],
        "expected": expected,
    }


def validate_continuous_attestation(
    value: Any,
    proposal_value: Mapping[str, Any],
) -> dict[str, Any]:
    proposal = validate_continuous_proposal(dict(proposal_value))
    data = _object(value, "Continuous-preview attestation")
    _strict_keys(
        data,
        {
            "schema",
            "attestation_token",
            "decision",
            "acknowledgement",
            "owner_attested_scope_reviewed",
            "owner_attested_references_reviewed",
            "proposal_sha256",
            "context_sha256",
            "expected",
        },
        "Continuous-preview attestation",
    )
    if data["schema"] != CONTINUOUS_ATTESTATION_SCHEMA:
        raise ProjectValidationError("Continuous-preview attestation schema is unsupported.")
    token = _digest(data["attestation_token"], "Continuous-preview attestation token")
    if len(set(token)) == 1:
        raise ProjectValidationError("Continuous-preview attestation token is non-distinct.")
    if data["decision"] != CONTINUOUS_REVIEW_DECISION:
        raise ProjectValidationError("Continuous-preview attestation decision is unsupported.")
    if data["acknowledgement"] != CONTINUOUS_REVIEW_ACKNOWLEDGEMENT:
        raise ProjectValidationError("Continuous-preview authority acknowledgement is required.")
    if (
        data["owner_attested_scope_reviewed"] is not True
        or data["owner_attested_references_reviewed"] is not True
    ):
        raise ProjectValidationError(
            "Continuous-preview scope and references require owner review."
        )
    if data["proposal_sha256"] != proposal["proposal_sha256"]:
        raise ProjectValidationError("Continuous-preview attestation targets another proposal.")
    context = proposal["context"]
    if data["context_sha256"] != context["context_sha256"]:
        raise ProjectValidationError("Continuous-preview attestation context is stale.")
    template = continuous_attestation_template(proposal)
    if data["expected"] != template["expected"]:
        raise ProjectValidationError(
            "Expected project, source, speed, config, tool, or module hashes are stale."
        )
    return data


def _foundation_attestation(
    kind: ContinuousKind,
    proposal: Mapping[str, Any],
    attestation: Mapping[str, Any],
) -> dict[str, Any]:
    foundation = proposal["foundation"]
    selected_scope = foundation["scope"]
    schema = {
        "hum": HUM_REVIEW_ATTESTATION_SCHEMA,
        "rumble": RUMBLE_REVIEW_ATTESTATION_SCHEMA,
        "hiss": HISS_REVIEW_ATTESTATION_SCHEMA,
        "crackle": CRACKLE_REVIEW_ATTESTATION_SCHEMA,
    }[kind]
    acknowledgement = {
        "hum": HUM_REVIEW_ACKNOWLEDGEMENT,
        "rumble": RUMBLE_REVIEW_ACKNOWLEDGEMENT,
        "hiss": HISS_REVIEW_ACKNOWLEDGEMENT,
        "crackle": CRACKLE_REVIEW_ACKNOWLEDGEMENT,
    }[kind]
    return {
        "schema": schema,
        "attestation_token": attestation["attestation_token"],
        "decision": CONTINUOUS_REVIEW_DECISION,
        "proposal_body_sha256": foundation["proposal_body_sha256"],
        "selected_scope": selected_scope,
        "acknowledgement": acknowledgement,
    }


def _write_float32_wav(path: Path, samples: np.ndarray, sample_rate: int) -> dict[str, Any]:
    framed = samples[:, np.newaxis] if samples.ndim == 1 else samples
    pcm = np.ascontiguousarray(framed, dtype="<f4")
    channels = pcm.shape[1]
    data = pcm.tobytes(order="C")
    # WAVE_FORMAT_IEEE_FLOAT with a fact chunk.  File size remains far below RIFF32.
    fmt = struct.pack(
        "<HHIIHH",
        3,
        channels,
        sample_rate,
        sample_rate * channels * 4,
        channels * 4,
        32,
    )
    fact = struct.pack("<I", pcm.shape[0])
    riff_size = 4 + (8 + len(fmt)) + (8 + len(fact)) + (8 + len(data))
    if riff_size >= 2**32:
        raise ProjectValidationError("Continuous-preview WAV exceeds the RIFF32 size limit.")
    raw = (
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVEfmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"fact"
        + struct.pack("<I", len(fact))
        + fact
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    path.write_bytes(raw)
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "sample_rate": sample_rate,
        "channels": channels,
        "frame_count": pcm.shape[0],
        "pcm_f32le_sha256": hashlib.sha256(data).hexdigest(),
        "format": "wave_ieee_float32",
    }


def _preview_result(
    kind: ContinuousKind,
    pcm: np.ndarray,
    foundation: Mapping[str, Any],
    attestation: Mapping[str, Any],
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    Mapping[str, Any],
    Mapping[str, Any],
    Mapping[str, Any],
]:
    internal_attestation = _foundation_attestation(kind, {"foundation": foundation}, attestation)
    if kind == "hum":
        proposal = ContinuousNoiseProposalDocument.from_dict(foundation)
        hum_recipe = create_hum_preview_recipe(proposal, internal_attestation)
        hum_result = render_hum_preview(pcm, proposal, hum_recipe)
        return (
            hum_result.original,
            hum_result.proposed,
            hum_result.removed,
            hum_recipe.to_dict(),
            hum_result.render_manifest,
            hum_result.receipt,
        )
    if kind == "rumble":
        proposal = ContinuousNoiseProposalDocument.from_dict(foundation)
        rumble_recipe = create_rumble_preview_recipe(proposal, internal_attestation)
        rumble_result = render_rumble_preview(pcm, proposal, rumble_recipe)
        return (
            rumble_result.original,
            rumble_result.proposed,
            rumble_result.removed,
            rumble_recipe.to_dict(),
            rumble_result.render_manifest,
            rumble_result.receipt,
        )
    if kind == "crackle":
        crackle_proposal = CrackleProposal.from_dict(foundation)
        crackle_recipe = create_crackle_preview_recipe(
            crackle_proposal,
            internal_attestation,
        )
        crackle_result = render_crackle_preview(pcm, crackle_proposal, crackle_recipe)
        return (
            crackle_result.original,
            crackle_result.proposed,
            crackle_result.removed,
            crackle_recipe.to_dict(),
            crackle_result.render_manifest,
            crackle_result.receipt,
        )
    hiss_proposal = HissProposal.from_dict(foundation)
    hiss_recipe = create_hiss_preview_recipe(hiss_proposal, internal_attestation)
    hiss_result = render_hiss_preview(pcm, hiss_proposal, hiss_recipe)
    return (
        hiss_result.original,
        hiss_result.proposed,
        hiss_result.removed,
        hiss_recipe.to_dict(),
        hiss_result.render_manifest,
        hiss_result.receipt,
    )


def validate_continuous_preview_receipt(value: Any) -> dict[str, Any]:
    data = _object(value, "Continuous-preview receipt")
    _strict_keys(
        data,
        {
            "schema",
            "receipt_sha256",
            "created_at",
            "kind",
            "proposal_sha256",
            "proposal_created_at",
            "context",
            "selection",
            "attestation",
            "foundation",
            "recipe",
            "render",
            "foundation_receipt",
            "audio",
            "authority",
        },
        "Continuous-preview receipt",
    )
    if data["schema"] != CONTINUOUS_PREVIEW_SCHEMA:
        raise ProjectValidationError("Continuous-preview receipt schema is unsupported.")
    kind = _kind(data["kind"])
    _text(data["created_at"], "Continuous-preview receipt timestamp", 64)
    context = _validate_context(data["context"])
    if context["kind"] != kind:
        raise ProjectValidationError("Continuous-preview receipt kind and context disagree.")
    _validate_selection(data["selection"], context)
    _digest(data["proposal_sha256"], "Continuous-preview proposal SHA-256")
    proposal_for_attestation = {
        "schema": CONTINUOUS_PROPOSAL_SCHEMA,
        "proposal_sha256": data["proposal_sha256"],
        "created_at": data["proposal_created_at"],
        "kind": kind,
        "context": context,
        "selection": data["selection"],
        "foundation": data["foundation"],
        "status": "proposed",
        "authority": _authority(kind),
    }
    proposal_for_attestation = validate_continuous_proposal(proposal_for_attestation)
    # The original proposal timestamp is intentionally not duplicated in a receipt;
    # validate the strict attestation fields directly against its bound identities.
    attestation = _object(data["attestation"], "Continuous-preview attestation")
    template_expected = {
        "project_sha256": context["project"]["sha256"],
        "project_state_sha256": context["project"]["state_sha256"],
        "source_sha256": context["source"]["sha256"],
        "speed_state_sha256": context["speed"]["state_sha256"],
        "analysis_config_sha256": context["config"]["analysis_sha256"],
        "preview_config_sha256": context["config"]["preview_sha256"],
        "ffmpeg_executable_sha256": context["tools"]["ffmpeg"]["executable_sha256"],
        "ffprobe_executable_sha256": context["tools"]["ffprobe"]["executable_sha256"],
        **context["modules"],
    }
    if (
        set(attestation)
        != {
            "schema",
            "attestation_token",
            "decision",
            "acknowledgement",
            "owner_attested_scope_reviewed",
            "owner_attested_references_reviewed",
            "proposal_sha256",
            "context_sha256",
            "expected",
        }
        or attestation["schema"] != CONTINUOUS_ATTESTATION_SCHEMA
        or attestation["decision"] != CONTINUOUS_REVIEW_DECISION
        or attestation["acknowledgement"] != CONTINUOUS_REVIEW_ACKNOWLEDGEMENT
        or attestation["owner_attested_scope_reviewed"] is not True
        or attestation["owner_attested_references_reviewed"] is not True
        or attestation["proposal_sha256"] != data["proposal_sha256"]
        or attestation["context_sha256"] != context["context_sha256"]
        or attestation["expected"] != template_expected
    ):
        raise ProjectValidationError("Continuous-preview receipt attestation is invalid.")
    _digest(attestation["attestation_token"], "Continuous-preview attestation token")
    validate_continuous_attestation(attestation, proposal_for_attestation)
    foundation = _object(data["foundation"], "Foundation proposal")
    recipe = _object(data["recipe"], "Foundation recipe")
    render = _object(data["render"], "Foundation render")
    receipt = _object(data["foundation_receipt"], "Foundation receipt")
    status: str
    if kind == "hum":
        proposal = ContinuousNoiseProposalDocument.from_dict(foundation)
        hum_recipe = HumPreviewRecipe.from_dict(recipe)
        validate_hum_preview_render_manifest(render, recipe=hum_recipe)
        validate_hum_preview_receipt(receipt, recipe=hum_recipe, render_manifest=render)
        status = proposal.hum.status
    elif kind == "rumble":
        proposal = ContinuousNoiseProposalDocument.from_dict(foundation)
        rumble_recipe = RumblePreviewRecipe.from_dict(recipe)
        validate_rumble_preview_render_manifest(render, recipe=rumble_recipe)
        validate_rumble_preview_receipt(
            receipt, recipe=rumble_recipe, render_manifest=render
        )
        status = proposal.rumble.status
    elif kind == "hiss":
        hiss_proposal = HissProposal.from_dict(foundation)
        hiss_recipe = HissPreviewRecipe.from_dict(recipe)
        validate_hiss_preview_render_manifest(render, recipe=hiss_recipe)
        validate_hiss_preview_receipt(receipt, recipe=hiss_recipe, render_manifest=render)
        status = hiss_proposal.status
    else:
        crackle_proposal = CrackleProposal.from_dict(foundation)
        crackle_recipe = CracklePreviewRecipe.from_dict(recipe)
        validate_crackle_preview_render_manifest(render, recipe=crackle_recipe)
        validate_crackle_preview_receipt(
            receipt,
            recipe=crackle_recipe,
            render_manifest=render,
        )
        status = crackle_proposal.status
    if status != "proposed":
        raise ProjectValidationError("A continuous preview cannot bind an abstained proposal.")
    audio = _object(data["audio"], "Continuous-preview audio")
    _strict_keys(audio, {"original", "proposed", "removed"}, "Continuous-preview audio")
    for role in ("original", "proposed", "removed"):
        item = _object(audio[role], f"Continuous-preview {role} audio")
        _strict_keys(
            item,
            {
                "filename",
                "sha256",
                "size_bytes",
                "sample_rate",
                "channels",
                "frame_count",
                "pcm_f32le_sha256",
                "format",
                "audition_gain",
            },
            f"Continuous-preview {role} audio",
        )
        if Path(_text(item["filename"], f"{role} filename", 64)).name != item["filename"]:
            raise ProjectValidationError("Continuous-preview audio filename is unsafe.")
        _digest(item["sha256"], f"{role} audio SHA-256")
        _digest(item["pcm_f32le_sha256"], f"{role} PCM SHA-256")
        _integer(item["size_bytes"], f"{role} audio size", 45, 2**32 - 1)
        _integer(item["sample_rate"], f"{role} sample rate", 8_000, 768_000)
        _integer(item["channels"], f"{role} channels", 1, 32)
        _integer(item["frame_count"], f"{role} frame count", 1, MAX_SCOPE_FRAMES)
        _number(item["audition_gain"], f"{role} audition gain", 0.000001, 100.0)
        if item["format"] != "wave_ieee_float32":
            raise ProjectValidationError("Continuous-preview audio format is unsupported.")
    if data["authority"] != _authority(kind):
        raise ProjectValidationError("Continuous-preview receipt authority is invalid.")
    _digest(data["receipt_sha256"], "Continuous-preview receipt SHA-256")
    body = dict(data)
    del body["receipt_sha256"]
    if canonical_json_sha256(body) != data["receipt_sha256"]:
        raise ProjectValidationError("Continuous-preview receipt identity is invalid.")
    return data


def render_continuous_preview(
    project_path: Path | str,
    proposal_value: Mapping[str, Any],
    attestation_value: Mapping[str, Any],
    *,
    source_snapshot: VerifiedAudioSnapshot | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Persist one immutable three-way audition bundle or refuse stale inputs."""

    path = Path(project_path).expanduser().resolve()
    proposal = validate_continuous_proposal(dict(proposal_value))
    if proposal["status"] != "proposed":
        raise ProjectValidationError("An abstained proposal cannot produce an audition preview.")
    _assert_current_context(path, proposal["context"])
    attestation = validate_continuous_attestation(attestation_value, proposal)
    project, project_sha256 = load_project_with_sha256(path)
    if project_sha256 != proposal["context"]["project"]["sha256"]:
        raise ProjectValidationError("The project changed before continuous preview rendering.")
    selection = proposal["selection"]
    start = cast(int, selection["source_start_sample"])
    end = cast(int, selection["source_end_sample_exclusive"])
    refs = tuple(
        ReviewedNoiseReference.from_dict(item, scope_start=start, scope_end=end)
        for item in selection["references"]
    )
    _validate_geometry(project, start, end, refs)
    source = resolve_source_path(project, path).resolve()
    owns_snapshot = source_snapshot is None
    snapshot = source_snapshot or verified_audio_snapshot(
        source,
        expected_sha256=project.source.sha256,
        expected_size_bytes=project.source.size_bytes,
        label="Continuous-preview source audio",
    )
    try:
        snapshot.assert_snapshot_unchanged(force=True)
        snapshot.assert_live_unchanged(force=True)
        pcm = _decode_scope(snapshot, project, start, end)
        kind = _kind(proposal["kind"])
        original, proposed, removed, recipe, render, foundation_receipt = _preview_result(
            kind, pcm, proposal["foundation"], attestation
        )
        snapshot.assert_snapshot_unchanged(force=True)
        snapshot.assert_live_unchanged(force=True)
        _assert_current_context(path, proposal["context"])
    finally:
        if owns_snapshot:
            snapshot.close()
    audition = foundation_receipt["audition"]
    gains = {
        "original": float(audition["original_linear_gain"]),
        "proposed": float(audition["proposed_linear_gain"]),
        "removed": float(audition["residue_monitor_linear_gain"]),
    }
    workspace = _workspace_for(path)
    destination = _unique_path(workspace, f"preview-{proposal['kind']}", directory=True)
    stage = Path(tempfile.mkdtemp(dir=workspace, prefix=f".{destination.name}."))
    try:
        audio: dict[str, Any] = {}
        for role, samples in (
            ("original", original),
            ("proposed", proposed),
            ("removed", removed),
        ):
            file_receipt = _write_float32_wav(
                stage / f"{role}.wav",
                samples * gains[role],
                project.source.sample_rate,
            )
            file_receipt["audition_gain"] = gains[role]
            audio[role] = file_receipt
        receipt_body: dict[str, Any] = {
            "schema": CONTINUOUS_PREVIEW_SCHEMA,
            "created_at": utc_now_iso(),
            "kind": proposal["kind"],
            "proposal_sha256": proposal["proposal_sha256"],
            "proposal_created_at": proposal["created_at"],
            "context": proposal["context"],
            "selection": proposal["selection"],
            "attestation": attestation,
            "foundation": proposal["foundation"],
            "recipe": recipe,
            "render": render,
            "foundation_receipt": foundation_receipt,
            "audio": audio,
            "authority": _authority(_kind(proposal["kind"])),
        }
        receipt = dict(receipt_body)
        receipt["receipt_sha256"] = canonical_json_sha256(receipt_body)
        receipt = validate_continuous_preview_receipt(receipt)
        _atomic_json(stage / "preview.json", receipt)
        rename_no_replace(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return destination, receipt


def validate_continuous_decision(value: Any) -> dict[str, Any]:
    data = _object(value, "Continuous-preview decision")
    _strict_keys(
        data,
        {
            "schema",
            "decision_sha256",
            "created_at",
            "kind",
            "decision",
            "proposal_sha256",
            "context_sha256",
            "expected_project_sha256",
            "expected_source_sha256",
            "expected_speed_state_sha256",
            "reason",
            "authority",
        },
        "Continuous-preview decision",
    )
    if data["schema"] != CONTINUOUS_DECISION_SCHEMA:
        raise ProjectValidationError("Continuous-preview decision schema is unsupported.")
    kind = _kind(data["kind"])
    if data["decision"] != CONTINUOUS_REJECTION_DECISION:
        raise ProjectValidationError("Continuous-preview decision is unsupported.")
    _text(data["created_at"], "Continuous-preview decision timestamp", 64)
    for key in (
        "proposal_sha256",
        "context_sha256",
        "expected_project_sha256",
        "expected_source_sha256",
        "expected_speed_state_sha256",
        "decision_sha256",
    ):
        _digest(data[key], f"Continuous-preview {key}")
    _text(data["reason"], "Continuous-preview rejection reason", 512)
    if data["authority"] != _authority(kind):
        raise ProjectValidationError("Continuous-preview rejection authority is invalid.")
    body = dict(data)
    del body["decision_sha256"]
    if canonical_json_sha256(body) != data["decision_sha256"]:
        raise ProjectValidationError("Continuous-preview decision identity is invalid.")
    return data


def reject_continuous_proposal(
    project_path: Path | str,
    proposal_value: Mapping[str, Any],
    *,
    reason: str,
) -> tuple[Path, dict[str, Any]]:
    """Persist an explicit non-mutating rejection for one exact current proposal."""

    path = Path(project_path).expanduser().resolve()
    proposal = validate_continuous_proposal(dict(proposal_value))
    _assert_current_context(path, proposal["context"])
    reason = _text(reason, "Continuous-preview rejection reason", 512)
    context = proposal["context"]
    body: dict[str, Any] = {
        "schema": CONTINUOUS_DECISION_SCHEMA,
        "created_at": utc_now_iso(),
        "kind": proposal["kind"],
        "decision": CONTINUOUS_REJECTION_DECISION,
        "proposal_sha256": proposal["proposal_sha256"],
        "context_sha256": context["context_sha256"],
        "expected_project_sha256": context["project"]["sha256"],
        "expected_source_sha256": context["source"]["sha256"],
        "expected_speed_state_sha256": context["speed"]["state_sha256"],
        "reason": reason,
        "authority": _authority(_kind(proposal["kind"])),
    }
    decision = dict(body)
    decision["decision_sha256"] = canonical_json_sha256(body)
    decision = validate_continuous_decision(decision)
    output = _unique_path(_workspace_for(path), f"decision-{proposal['kind']}")
    _atomic_json(output, decision)
    return output, decision


def _load_json(path: Path) -> dict[str, Any]:
    try:
        before = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_nlink) != 1
            or before.st_size <= 0
            or before.st_size > MAX_JSON_BYTES
        ):
            raise ProjectValidationError("Continuous-preview artifact is not a bounded plain file.")
        with path.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            raw = handle.read(MAX_JSON_BYTES + 1)
        after = path.lstat()
        if (
            _stat_identity(before) != _stat_identity(opened)
            or _stat_identity(opened) != _stat_identity(after)
            or len(raw) != before.st_size
        ):
            raise ProjectValidationError("Continuous-preview artifact changed while being read.")
        return decode_project_json(raw)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ProjectValidationError(f"Continuous-preview JSON is invalid: {exc}") from exc


def load_continuous_proposal(path: Path | str) -> dict[str, Any]:
    return validate_continuous_proposal(_load_json(Path(path).expanduser().resolve()))


def load_continuous_expected_context(path: Path | str) -> dict[str, Any]:
    return _validate_context(_load_json(Path(path).expanduser().resolve()))


def write_continuous_expected_context(
    context_value: Mapping[str, Any], path: Path | str
) -> str:
    context = _validate_context(dict(context_value))
    return _atomic_json(Path(path).expanduser().absolute(), context)


def load_continuous_attestation(
    path: Path | str,
    proposal_value: Mapping[str, Any],
) -> dict[str, Any]:
    return validate_continuous_attestation(
        _load_json(Path(path).expanduser().resolve()), proposal_value
    )


def write_continuous_attestation(
    attestation_value: Mapping[str, Any],
    proposal_value: Mapping[str, Any],
    path: Path | str,
) -> str:
    attestation = validate_continuous_attestation(attestation_value, proposal_value)
    return _atomic_json(Path(path).expanduser().absolute(), attestation)


def load_continuous_preview_receipt(path: Path | str) -> dict[str, Any]:
    candidate = Path(path).expanduser().resolve()
    manifest = candidate / "preview.json" if candidate.is_dir() else candidate
    receipt = validate_continuous_preview_receipt(_load_json(manifest))
    bundle = manifest.parent
    expected_names = {"preview.json", "original.wav", "proposed.wav", "removed.wav"}
    actual_names = {item.name for item in bundle.iterdir()}
    if actual_names != expected_names:
        raise ProjectValidationError("Continuous-preview bundle contents are invalid.")
    for role in ("original", "proposed", "removed"):
        item = receipt["audio"][role]
        output = bundle / item["filename"]
        value = output.lstat()
        if output.is_symlink() or not stat.S_ISREG(value.st_mode):
            raise ProjectValidationError("Continuous-preview audio is not a plain file.")
        if value.st_size != item["size_bytes"] or sha256_file(output) != item["sha256"]:
            raise ProjectValidationError("Continuous-preview audio changed after rendering.")
    return receipt


def discover_continuous_preview_catalog(project_path: Path | str) -> dict[str, Any]:
    """Rediscover current/stale/invalid artifacts without mutating the workspace."""

    path = Path(project_path).expanduser().resolve()
    workspace = _workspace_for(path)
    entries: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    current_contexts: dict[ContinuousKind, dict[str, Any]] = {}

    def current_context_for(kind_value: Any) -> dict[str, Any]:
        artifact_kind = _kind(kind_value)
        cached = current_contexts.get(artifact_kind)
        if cached is None:
            cached = current_continuous_preview_context(path, artifact_kind)
            current_contexts[artifact_kind] = cached
        return cached

    if workspace.exists():
        children = sorted(workspace.iterdir(), key=lambda item: item.name)
        if len(children) > MAX_CATALOG_ENTRIES:
            raise ProjectValidationError(
                f"Continuous-preview catalog exceeds {MAX_CATALOG_ENTRIES} entries."
            )
        for child in children:
            try:
                if child.is_dir() and child.name.startswith("preview-"):
                    payload = load_continuous_preview_receipt(child)
                    artifact_kind = "preview"
                    identity = payload["receipt_sha256"]
                    context = payload["context"]
                    proposal_sha = payload["proposal_sha256"]
                elif child.is_file() and child.name.startswith("proposal-"):
                    payload = validate_continuous_proposal(_load_json(child))
                    artifact_kind = "proposal"
                    identity = payload["proposal_sha256"]
                    context = payload["context"]
                    proposal_sha = payload["proposal_sha256"]
                elif child.is_file() and child.name.startswith("decision-"):
                    payload = validate_continuous_decision(_load_json(child))
                    artifact_kind = "decision"
                    identity = payload["decision_sha256"]
                    proposal_sha = payload["proposal_sha256"]
                    context = None
                else:
                    raise ProjectValidationError("Unknown continuous-preview workspace entry.")
                current = False
                stale_reason: str | None = None
                if context is not None:
                    current = current_context_for(payload["kind"]) == context
                    if not current:
                        stale_reason = "project_source_speed_config_tool_or_module_changed"
                else:
                    # A decision is current only when its explicit expected identities still match.
                    current_context = current_context_for(payload["kind"])
                    current = (
                        payload["context_sha256"] == current_context["context_sha256"]
                        and payload["expected_project_sha256"]
                        == current_context["project"]["sha256"]
                        and payload["expected_source_sha256"]
                        == current_context["source"]["sha256"]
                        and payload["expected_speed_state_sha256"]
                        == current_context["speed"]["state_sha256"]
                    )
                    if not current:
                        stale_reason = "project_source_speed_or_context_changed"
                entries.append(
                    {
                        "artifact_kind": artifact_kind,
                        "kind": payload["kind"],
                        "identity_sha256": identity,
                        "proposal_sha256": proposal_sha,
                        "path": str(child),
                        "status": "current" if current else "stale",
                        "stale_reason": stale_reason,
                        "payload": payload,
                    }
                )
            except (OSError, GrooveSerpentError, ValueError) as exc:
                invalid.append(
                    {
                        "path": str(child),
                        "error": str(exc)[:512],
                    }
                )
    summary = {
        "total": len(entries) + len(invalid),
        "current": sum(item["status"] == "current" for item in entries),
        "stale": sum(item["status"] == "stale" for item in entries),
        "invalid": len(invalid),
        "proposals": sum(item["artifact_kind"] == "proposal" for item in entries),
        "previews": sum(item["artifact_kind"] == "preview" for item in entries),
        "decisions": sum(item["artifact_kind"] == "decision" for item in entries),
    }
    return {
        "schema": CONTINUOUS_CATALOG_SCHEMA,
        "workspace": str(workspace),
        "summary": summary,
        "entries": entries,
        "invalid": invalid,
        "authority": _catalog_authority(),
    }


def find_current_continuous_artifact(
    project_path: Path | str,
    *,
    artifact_kind: Literal["proposal", "preview", "decision"],
    identity_sha256: str,
) -> dict[str, Any]:
    digest = _digest(identity_sha256, "Continuous-preview artifact identity")
    catalog = discover_continuous_preview_catalog(project_path)
    matches = [
        item
        for item in catalog["entries"]
        if item["artifact_kind"] == artifact_kind
        and item["identity_sha256"] == digest
        and item["status"] == "current"
    ]
    if len(matches) != 1:
        raise ProjectValidationError("No unique current continuous-preview artifact matches.")
    return cast(dict[str, Any], matches[0])


__all__ = [
    "CONTINUOUS_ATTESTATION_SCHEMA",
    "CONTINUOUS_CATALOG_SCHEMA",
    "CONTINUOUS_EXPECTED_SCHEMA",
    "CONTINUOUS_METHOD_REGISTRY",
    "CONTINUOUS_PREVIEW_SCHEMA",
    "CONTINUOUS_PROPOSAL_SCHEMA",
    "CONTINUOUS_REVIEW_ACKNOWLEDGEMENT",
    "CONTINUOUS_REVIEW_DECISION",
    "ReviewedNoiseReference",
    "continuous_attestation_template",
    "continuous_preview_workspace",
    "current_continuous_preview_context",
    "discover_continuous_preview_catalog",
    "find_current_continuous_artifact",
    "load_continuous_attestation",
    "load_continuous_expected_context",
    "load_continuous_preview_receipt",
    "load_continuous_proposal",
    "propose_continuous_preview",
    "reject_continuous_proposal",
    "render_continuous_preview",
    "validate_continuous_attestation",
    "validate_continuous_decision",
    "validate_continuous_preview_receipt",
    "validate_continuous_proposal",
    "write_continuous_attestation",
    "write_continuous_expected_context",
]
