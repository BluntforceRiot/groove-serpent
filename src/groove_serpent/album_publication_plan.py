"""Strict, hash-bound album publication plans.

This module describes *intent*.  It does not execute audio processing or
publication.  Every stored path is a portable relative reference, every
processing edge is explicit, and the envelope binds a canonical plan body.
Execution code must still re-verify the referenced files and tool identities
immediately before use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .atomic_create import rename_no_replace
from .errors import ProjectValidationError
from .publication import same_file_object_stats
from .validation import strict_finite_number


ALBUM_PUBLICATION_PLAN_SCHEMA = "groove-serpent.album-publication-plan/1"
RESTORATION_RENDER_SCHEMA = "groove-serpent.restoration-render/1"
RESTORATION_NO_DERIVATIVE_SCHEMA = "groove-serpent.restoration-no-derivative/1"
RESTORATION_SCAN_SCHEMA = "groove-serpent.click-scan/1"

PROFILE_ARCHIVAL_SOURCE = "archival-source"
PROFILE_RESTORED_SIDE = "restored-side"
PROFILE_CORRECTED_LOSSLESS = "corrected-lossless"
PROFILE_PORTABLE = "portable"

_PROFILE_ORDER = (
    PROFILE_ARCHIVAL_SOURCE,
    PROFILE_RESTORED_SIDE,
    PROFILE_CORRECTED_LOSSLESS,
    PROFILE_PORTABLE,
)
_PROFILE_OPERATIONS = {
    PROFILE_ARCHIVAL_SOURCE: "assemble-archival",
    PROFILE_RESTORED_SIDE: "assemble-restored",
    PROFILE_CORRECTED_LOSSLESS: "encode-lossless",
    PROFILE_PORTABLE: "encode-portable",
}
_SIDE_OPERATIONS = {"source-side", "restore-side", "correct-speed-side"}
_AGGREGATE_OPERATIONS = {
    "assemble-archival",
    "assemble-restored",
    "encode-lossless",
    "encode-portable",
}
_OPERATIONS = _SIDE_OPERATIONS | _AGGREGATE_OPERATIONS
_IDENTITY_FIELDS = (
    "project_revision",
    "project_sha256",
    "editable_state_sha256",
    "source_sha256",
    "project_speed_state_sha256",
)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_ROLE_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_WINDOWS_DEVICE_STEMS = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}
_MAX_PLAN_BYTES = 4 * 1024 * 1024
_MAX_SIDES = 64
_MAX_NODES = 512
_MAX_INPUTS_PER_NODE = 64
_MAX_CONFIGURATION_DEPTH = 4
_MAX_CONFIGURATION_ENTRIES = 128
_MAX_CONFIGURATION_COLLECTION = 32
_MAX_CONFIGURATION_BYTES = 16 * 1024
_CONFIGURATION_KEY_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")


def _strict_keys(data: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(data)
    missing = expected - actual
    extra = actual - expected
    if missing:
        raise ProjectValidationError(
            f"{label} is missing required field(s): {', '.join(sorted(missing))}."
        )
    if extra:
        raise ProjectValidationError(
            f"{label} contains unsupported field(s): {', '.join(sorted(extra))}."
        )


def _strict_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectValidationError(f"{label} must be a JSON object.")
    return value


def _strict_array(value: Any, label: str, *, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise ProjectValidationError(f"{label} must be a JSON array.")
    if len(value) > maximum:
        raise ProjectValidationError(
            f"{label} exceeds the supported maximum of {maximum} entries."
        )
    return value


def _strict_text(value: Any, label: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
        or unicodedata.normalize("NFC", value) != value
    ):
        raise ProjectValidationError(
            f"{label} must be 1-{maximum} characters of trimmed NFC printable text."
        )
    return value


def _strict_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ProjectValidationError(
            f"{label} must be 64 lowercase hexadecimal characters."
        )
    return value


def _strict_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ProjectValidationError(
            f"{label} must use 1-64 lowercase letters, digits, dots, dashes, or underscores."
        )
    return value


def _strict_role(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ROLE_RE.fullmatch(value) is None:
        raise ProjectValidationError(
            f"{label} must use 1-64 lowercase letters, digits, dots, dashes, or underscores."
        )
    return value


def _relative_reference(value: Any, label: str) -> str:
    text = _strict_text(value, label, maximum=512)
    if (
        "\\" in text
        or text.startswith("/")
        or "//" in text
        or ":" in text
        or any(character in '<>"|?*' for character in text)
    ):
        raise ProjectValidationError(
            f"{label} must be a portable relative path using forward slashes."
        )
    parts = text.split("/")
    if any(
        part in {"", ".", ".."}
        or len(part) > 255
        or part.endswith((" ", "."))
        or part.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_STEMS
        for part in parts
    ):
        raise ProjectValidationError(
            f"{label} must remain inside the publication-plan folder."
        )
    normalized = PurePosixPath(text).as_posix()
    if normalized != text:
        raise ProjectValidationError(f"{label} is not a canonical relative path.")
    return normalized


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"Publication plan is not canonical finite JSON: {exc}"
        ) from exc
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _portable_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _normalized_configuration_value(
    value: Any,
    *,
    depth: int,
    remaining: list[int],
    label: str,
) -> Any:
    if depth > _MAX_CONFIGURATION_DEPTH:
        raise ProjectValidationError(
            f"{label} exceeds the maximum JSON depth of {_MAX_CONFIGURATION_DEPTH}."
        )
    remaining[0] -= 1
    if remaining[0] < 0:
        raise ProjectValidationError(
            f"{label} exceeds {_MAX_CONFIGURATION_ENTRIES} JSON values."
        )
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value <= (2**63 - 1):
            raise ProjectValidationError(
                f"{label} integers must fit in a signed 64-bit value."
            )
        return value
    if type(value) is float:
        return strict_finite_number(value, label)
    if isinstance(value, str):
        return _strict_text(value, label, maximum=256)
    if isinstance(value, list):
        if len(value) > _MAX_CONFIGURATION_COLLECTION:
            raise ProjectValidationError(
                f"{label} arrays cannot exceed {_MAX_CONFIGURATION_COLLECTION} items."
            )
        return [
            _normalized_configuration_value(
                item,
                depth=depth + 1,
                remaining=remaining,
                label=f"{label} item",
            )
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > _MAX_CONFIGURATION_COLLECTION:
            raise ProjectValidationError(
                f"{label} objects cannot exceed {_MAX_CONFIGURATION_COLLECTION} fields."
            )
        if any(
            not isinstance(key, str)
            or _CONFIGURATION_KEY_RE.fullmatch(key) is None
            for key in value
        ):
            raise ProjectValidationError(
                f"{label} keys must use 1-64 lowercase identifier characters."
            )
        normalized: dict[str, Any] = {}
        for key in sorted(value):
            normalized[key] = _normalized_configuration_value(
                value[key],
                depth=depth + 1,
                remaining=remaining,
                label=f"{label} field {key!r}",
            )
        return normalized
    raise ProjectValidationError(
        f"{label} contains an unsupported JSON value of type {type(value).__name__}."
    )


def _normalized_tool_configuration(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectValidationError("Tool configuration must be a JSON object.")
    normalized = _normalized_configuration_value(
        value,
        depth=0,
        remaining=[_MAX_CONFIGURATION_ENTRIES],
        label="Tool configuration",
    )
    if not isinstance(normalized, dict):
        raise AssertionError("Root tool configuration normalization must return an object.")
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_CONFIGURATION_BYTES:
        raise ProjectValidationError(
            f"Tool configuration exceeds {_MAX_CONFIGURATION_BYTES} canonical bytes."
        )
    return normalized


@dataclass(frozen=True, slots=True)
class SideIdentity:
    """The exact five-field current identity of one side project."""

    project_revision: int
    project_sha256: str
    editable_state_sha256: str
    source_sha256: str
    project_speed_state_sha256: str

    def validate(self) -> None:
        if (
            type(self.project_revision) is not int
            or not 1 <= self.project_revision <= (2**63 - 1)
        ):
            raise ProjectValidationError(
                "Side project revision must be an integer between 1 and 2^63-1."
            )
        for field_name in _IDENTITY_FIELDS[1:]:
            _strict_digest(
                getattr(self, field_name),
                f"Side identity {field_name.replace('_', ' ')}",
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "project_revision": self.project_revision,
            "project_sha256": self.project_sha256,
            "editable_state_sha256": self.editable_state_sha256,
            "source_sha256": self.source_sha256,
            "project_speed_state_sha256": self.project_speed_state_sha256,
        }

    @classmethod
    def from_dict(cls, value: Any) -> SideIdentity:
        data = _strict_object(value, "Side identity")
        _strict_keys(data, set(_IDENTITY_FIELDS), "Side identity")
        identity = cls(
            project_revision=data["project_revision"],
            project_sha256=data["project_sha256"],
            editable_state_sha256=data["editable_state_sha256"],
            source_sha256=data["source_sha256"],
            project_speed_state_sha256=data["project_speed_state_sha256"],
        )
        identity.validate()
        return identity


@dataclass(frozen=True, slots=True)
class SpeedSelection:
    """The exact album-selected speed state and effective execution factor."""

    selected_speed_state_sha256: str
    selected_effective_speed_factor: float

    def validate(self) -> None:
        _strict_digest(
            self.selected_speed_state_sha256,
            "Selected album speed-state SHA-256",
        )
        factor = strict_finite_number(
            self.selected_effective_speed_factor,
            "Selected effective speed factor",
        )
        if not 0.25 <= factor <= 2.0:
            raise ProjectValidationError(
                "Selected effective speed factor must be between 0.25 and 2.0."
            )

    def normalized(self) -> SpeedSelection:
        self.validate()
        return SpeedSelection(
            selected_speed_state_sha256=self.selected_speed_state_sha256,
            selected_effective_speed_factor=float(
                self.selected_effective_speed_factor
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        normalized = self.normalized()
        return {
            "selected_speed_state_sha256": normalized.selected_speed_state_sha256,
            "selected_effective_speed_factor": (
                normalized.selected_effective_speed_factor
            ),
        }

    @classmethod
    def from_dict(cls, value: Any) -> SpeedSelection:
        data = _strict_object(value, "Selected speed state")
        _strict_keys(
            data,
            {
                "selected_speed_state_sha256",
                "selected_effective_speed_factor",
            },
            "Selected speed state",
        )
        selection = cls(
            selected_speed_state_sha256=data["selected_speed_state_sha256"],
            selected_effective_speed_factor=data[
                "selected_effective_speed_factor"
            ],
        ).normalized()
        return selection


@dataclass(frozen=True, slots=True)
class RestorationRenderBinding:
    """A receipt binding, not permission to trust or execute the stored paths."""

    schema: str
    manifest_reference: str
    manifest_sha256: str
    audio_reference: str
    audio_sha256: str
    project_sha256: str
    source_sha256: str

    def validate(self, identity: SideIdentity) -> None:
        if self.schema != RESTORATION_RENDER_SCHEMA:
            raise ProjectValidationError(
                f"Restoration render schema must be {RESTORATION_RENDER_SCHEMA!r}."
            )
        _relative_reference(self.manifest_reference, "Render manifest reference")
        _relative_reference(self.audio_reference, "Rendered audio reference")
        for field_name in (
            "manifest_sha256",
            "audio_sha256",
            "project_sha256",
            "source_sha256",
        ):
            _strict_digest(
                getattr(self, field_name),
                f"Restoration {field_name.replace('_', ' ')}",
            )
        if self.project_sha256 != identity.project_sha256:
            raise ProjectValidationError(
                "Restoration render project SHA-256 does not match its side identity."
            )
        if self.source_sha256 != identity.source_sha256:
            raise ProjectValidationError(
                "Restoration render source SHA-256 does not match its side identity."
            )

    def to_dict(self, identity: SideIdentity) -> dict[str, Any]:
        self.validate(identity)
        return {
            "schema": self.schema,
            "manifest_reference": self.manifest_reference,
            "manifest_sha256": self.manifest_sha256,
            "audio_reference": self.audio_reference,
            "audio_sha256": self.audio_sha256,
            "project_sha256": self.project_sha256,
            "source_sha256": self.source_sha256,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, identity: SideIdentity
    ) -> RestorationRenderBinding:
        data = _strict_object(value, "Restoration render binding")
        _strict_keys(
            data,
            {
                "schema",
                "manifest_reference",
                "manifest_sha256",
                "audio_reference",
                "audio_sha256",
                "project_sha256",
                "source_sha256",
            },
            "Restoration render binding",
        )
        binding = cls(
            schema=data["schema"],
            manifest_reference=data["manifest_reference"],
            manifest_sha256=data["manifest_sha256"],
            audio_reference=data["audio_reference"],
            audio_sha256=data["audio_sha256"],
            project_sha256=data["project_sha256"],
            source_sha256=data["source_sha256"],
        )
        binding.validate(identity)
        return binding


@dataclass(frozen=True, slots=True)
class RestorationNoDerivativeBinding:
    """A complete zero-candidate review that explicitly authorizes pass-through."""

    schema: str
    scan_schema: str
    scan_reference: str
    scan_sha256: str
    project_sha256: str
    source_sha256: str
    restoration_status: str
    scan_range_covers_music: bool
    candidate_scan_truncated: bool
    retained_candidates: int

    def validate(self, identity: SideIdentity) -> None:
        if self.schema != RESTORATION_NO_DERIVATIVE_SCHEMA:
            raise ProjectValidationError(
                "No-derivative restoration schema must be "
                f"{RESTORATION_NO_DERIVATIVE_SCHEMA!r}."
            )
        if self.scan_schema != RESTORATION_SCAN_SCHEMA:
            raise ProjectValidationError(
                f"No-derivative scan schema must be {RESTORATION_SCAN_SCHEMA!r}."
            )
        _relative_reference(self.scan_reference, "No-derivative scan reference")
        _strict_digest(self.scan_sha256, "No-derivative scan SHA-256")
        _strict_digest(self.project_sha256, "No-derivative project SHA-256")
        _strict_digest(self.source_sha256, "No-derivative source SHA-256")
        if self.project_sha256 != identity.project_sha256:
            raise ProjectValidationError(
                "No-derivative project SHA-256 does not match its side identity."
            )
        if self.source_sha256 != identity.source_sha256:
            raise ProjectValidationError(
                "No-derivative source SHA-256 does not match its side identity."
            )
        if self.restoration_status != "complete":
            raise ProjectValidationError(
                "No-derivative restoration review must have complete coverage."
            )
        if self.scan_range_covers_music is not True:
            raise ProjectValidationError(
                "No-derivative restoration scan must cover the complete music range."
            )
        if self.candidate_scan_truncated is not False:
            raise ProjectValidationError(
                "No-derivative restoration scan must be untruncated."
            )
        if type(self.retained_candidates) is not int or self.retained_candidates != 0:
            raise ProjectValidationError(
                "No-derivative restoration review requires zero retained candidates."
            )

    def to_dict(self, identity: SideIdentity) -> dict[str, Any]:
        self.validate(identity)
        return {
            "schema": self.schema,
            "scan_schema": self.scan_schema,
            "scan_reference": self.scan_reference,
            "scan_sha256": self.scan_sha256,
            "project_sha256": self.project_sha256,
            "source_sha256": self.source_sha256,
            "restoration_status": self.restoration_status,
            "scan_range_covers_music": self.scan_range_covers_music,
            "candidate_scan_truncated": self.candidate_scan_truncated,
            "retained_candidates": self.retained_candidates,
        }

    @classmethod
    def from_dict(
        cls, value: Any, *, identity: SideIdentity
    ) -> RestorationNoDerivativeBinding:
        data = _strict_object(value, "No-derivative restoration binding")
        _strict_keys(
            data,
            {
                "schema",
                "scan_schema",
                "scan_reference",
                "scan_sha256",
                "project_sha256",
                "source_sha256",
                "restoration_status",
                "scan_range_covers_music",
                "candidate_scan_truncated",
                "retained_candidates",
            },
            "No-derivative restoration binding",
        )
        binding = cls(
            schema=data["schema"],
            scan_schema=data["scan_schema"],
            scan_reference=data["scan_reference"],
            scan_sha256=data["scan_sha256"],
            project_sha256=data["project_sha256"],
            source_sha256=data["source_sha256"],
            restoration_status=data["restoration_status"],
            scan_range_covers_music=data["scan_range_covers_music"],
            candidate_scan_truncated=data["candidate_scan_truncated"],
            retained_candidates=data["retained_candidates"],
        )
        binding.validate(identity)
        return binding


@dataclass(frozen=True, slots=True)
class PublicationSide:
    label: str
    order: int
    project_reference: str
    current_identity: SideIdentity
    selected_speed_state_sha256: str
    selected_effective_speed_factor: float
    restoration_render: RestorationRenderBinding | None = None
    restoration_no_derivative: RestorationNoDerivativeBinding | None = None

    def validate(self) -> None:
        _strict_text(self.label, "Publication side label", maximum=32)
        if type(self.order) is not int or not 1 <= self.order <= _MAX_SIDES:
            raise ProjectValidationError(
                f"Publication side order must be an integer from 1 to {_MAX_SIDES}."
            )
        _relative_reference(self.project_reference, "Side project reference")
        if not isinstance(self.current_identity, SideIdentity):
            raise ProjectValidationError("Side identity must use the SideIdentity model.")
        self.current_identity.validate()
        _strict_digest(
            self.selected_speed_state_sha256,
            "Selected album speed-state SHA-256",
        )
        factor = strict_finite_number(
            self.selected_effective_speed_factor,
            "Selected effective speed factor",
        )
        if not 0.25 <= factor <= 2.0:
            raise ProjectValidationError(
                "Selected effective speed factor must be between 0.25 and 2.0."
            )
        if self.restoration_render is not None:
            if not isinstance(self.restoration_render, RestorationRenderBinding):
                raise ProjectValidationError(
                    "Restoration binding must use RestorationRenderBinding."
                )
            self.restoration_render.validate(self.current_identity)
        if self.restoration_no_derivative is not None:
            if not isinstance(
                self.restoration_no_derivative, RestorationNoDerivativeBinding
            ):
                raise ProjectValidationError(
                    "No-derivative binding must use RestorationNoDerivativeBinding."
                )
            self.restoration_no_derivative.validate(self.current_identity)
        if (
            self.restoration_render is not None
            and self.restoration_no_derivative is not None
        ):
            raise ProjectValidationError(
                "A side cannot bind both a restoration render and no-derivative review."
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "label": self.label,
            "order": self.order,
            "project_reference": self.project_reference,
            "current_identity": self.current_identity.to_dict(),
            "selected_speed_state_sha256": self.selected_speed_state_sha256,
            "selected_effective_speed_factor": self.selected_effective_speed_factor,
            "restoration_render": (
                self.restoration_render.to_dict(self.current_identity)
                if self.restoration_render is not None
                else None
            ),
            "restoration_no_derivative": (
                self.restoration_no_derivative.to_dict(self.current_identity)
                if self.restoration_no_derivative is not None
                else None
            ),
        }

    def normalized(self) -> PublicationSide:
        self.validate()
        return PublicationSide(
            label=self.label,
            order=self.order,
            project_reference=self.project_reference,
            current_identity=self.current_identity,
            selected_speed_state_sha256=self.selected_speed_state_sha256,
            selected_effective_speed_factor=float(
                self.selected_effective_speed_factor
            ),
            restoration_render=self.restoration_render,
            restoration_no_derivative=self.restoration_no_derivative,
        )

    @classmethod
    def from_dict(cls, value: Any) -> PublicationSide:
        data = _strict_object(value, "Publication side")
        _strict_keys(
            data,
            {
                "label",
                "order",
                "project_reference",
                "current_identity",
                "selected_speed_state_sha256",
                "selected_effective_speed_factor",
                "restoration_render",
                "restoration_no_derivative",
            },
            "Publication side",
        )
        identity = SideIdentity.from_dict(data["current_identity"])
        side = cls(
            label=data["label"],
            order=data["order"],
            project_reference=data["project_reference"],
            current_identity=identity,
            selected_speed_state_sha256=data["selected_speed_state_sha256"],
            selected_effective_speed_factor=data["selected_effective_speed_factor"],
            restoration_render=(
                RestorationRenderBinding.from_dict(
                    data["restoration_render"], identity=identity
                )
                if data["restoration_render"] is not None
                else None
            ),
            restoration_no_derivative=(
                RestorationNoDerivativeBinding.from_dict(
                    data["restoration_no_derivative"], identity=identity
                )
                if data["restoration_no_derivative"] is not None
                else None
            ),
        )
        side.validate()
        return side


@dataclass(frozen=True, slots=True)
class ToolBinding:
    name: str
    version: str
    configuration: dict[str, Any]
    configuration_sha256: str

    def validate(self) -> None:
        _strict_text(self.name, "Tool name", maximum=128)
        _strict_text(self.version, "Tool version", maximum=128)
        normalized = _normalized_tool_configuration(self.configuration)
        configuration_sha256 = _strict_digest(
            self.configuration_sha256, "Tool configuration SHA-256"
        )
        if self.configuration != normalized:
            raise ProjectValidationError(
                "Tool configuration is not in canonical deterministic key order."
            )
        if configuration_sha256 != _canonical_sha256(normalized):
            raise ProjectValidationError(
                "Tool configuration SHA-256 does not match its canonical JSON."
            )

    @classmethod
    def create(
        cls,
        *,
        name: str,
        version: str,
        configuration: Mapping[str, Any],
    ) -> ToolBinding:
        normalized = _normalized_tool_configuration(dict(configuration))
        binding = cls(
            name=name,
            version=version,
            configuration=normalized,
            configuration_sha256=_canonical_sha256(normalized),
        )
        binding.validate()
        return binding

    def normalized(self) -> ToolBinding:
        self.validate()
        return ToolBinding(
            name=self.name,
            version=self.version,
            configuration=_normalized_tool_configuration(self.configuration),
            configuration_sha256=self.configuration_sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "name": self.name,
            "version": self.version,
            "configuration": _normalized_tool_configuration(self.configuration),
            "configuration_sha256": self.configuration_sha256,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ToolBinding:
        data = _strict_object(value, "Tool binding")
        _strict_keys(
            data,
            {"name", "version", "configuration", "configuration_sha256"},
            "Tool binding",
        )
        binding = cls(
            name=data["name"],
            version=data["version"],
            configuration=_normalized_tool_configuration(data["configuration"]),
            configuration_sha256=data["configuration_sha256"],
        )
        binding.validate()
        return binding


@dataclass(frozen=True, slots=True)
class ProcessingInput:
    role: str
    node_id: str

    def validate(self) -> None:
        _strict_role(self.role, "Processing input role")
        _strict_id(self.node_id, "Processing input node ID")

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {"role": self.role, "node_id": self.node_id}

    @classmethod
    def from_dict(cls, value: Any) -> ProcessingInput:
        data = _strict_object(value, "Processing input")
        _strict_keys(data, {"role", "node_id"}, "Processing input")
        item = cls(role=data["role"], node_id=data["node_id"])
        item.validate()
        return item


@dataclass(frozen=True, slots=True)
class ProcessingNode:
    node_id: str
    operation: str
    side_label: str | None
    inputs: tuple[ProcessingInput, ...]
    tool: ToolBinding

    def validate(self) -> None:
        _strict_id(self.node_id, "Processing node ID")
        if self.operation not in _OPERATIONS:
            raise ProjectValidationError(
                f"Unsupported processing operation {self.operation!r}."
            )
        if self.operation in _SIDE_OPERATIONS:
            _strict_text(self.side_label, "Processing node side label", maximum=32)
        elif self.side_label is not None:
            raise ProjectValidationError(
                "Album-level processing nodes must have a null side label."
            )
        if not isinstance(self.inputs, tuple):
            raise ProjectValidationError("Processing inputs must be an immutable tuple.")
        if len(self.inputs) > _MAX_INPUTS_PER_NODE:
            raise ProjectValidationError(
                f"A node cannot have more than {_MAX_INPUTS_PER_NODE} inputs."
            )
        roles: set[str] = set()
        node_ids: set[str] = set()
        for item in self.inputs:
            if not isinstance(item, ProcessingInput):
                raise ProjectValidationError(
                    "Processing inputs must use the ProcessingInput model."
                )
            item.validate()
            if item.role in roles:
                raise ProjectValidationError(
                    f"Node {self.node_id!r} has duplicate input role {item.role!r}."
                )
            if item.node_id in node_ids:
                raise ProjectValidationError(
                    f"Node {self.node_id!r} repeats input node {item.node_id!r}."
                )
            roles.add(item.role)
            node_ids.add(item.node_id)
        if not isinstance(self.tool, ToolBinding):
            raise ProjectValidationError("Every processing node requires a tool binding.")
        self.tool.validate()

    def normalized(self) -> ProcessingNode:
        self.validate()
        return ProcessingNode(
            node_id=self.node_id,
            operation=self.operation,
            side_label=self.side_label,
            inputs=tuple(sorted(self.inputs, key=lambda item: (item.role, item.node_id))),
            tool=self.tool.normalized(),
        )

    def to_dict(self) -> dict[str, Any]:
        normalized = self.normalized()
        return {
            "node_id": normalized.node_id,
            "operation": normalized.operation,
            "side_label": normalized.side_label,
            "inputs": [item.to_dict() for item in normalized.inputs],
            "tool": normalized.tool.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> ProcessingNode:
        data = _strict_object(value, "Processing node")
        _strict_keys(
            data,
            {"node_id", "operation", "side_label", "inputs", "tool"},
            "Processing node",
        )
        raw_inputs = _strict_array(
            data["inputs"], "Processing inputs", maximum=_MAX_INPUTS_PER_NODE
        )
        node = cls(
            node_id=data["node_id"],
            operation=data["operation"],
            side_label=data["side_label"],
            inputs=tuple(ProcessingInput.from_dict(item) for item in raw_inputs),
            tool=ToolBinding.from_dict(data["tool"]),
        )
        node.validate()
        return node


@dataclass(frozen=True, slots=True)
class ProfileOutput:
    profile: str
    node_id: str

    def validate(self) -> None:
        if self.profile not in _PROFILE_ORDER:
            raise ProjectValidationError(
                f"Unsupported publication profile {self.profile!r}."
            )
        _strict_id(self.node_id, "Profile output node ID")

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {"profile": self.profile, "node_id": self.node_id}

    @classmethod
    def from_dict(cls, value: Any) -> ProfileOutput:
        data = _strict_object(value, "Profile output binding")
        _strict_keys(data, {"profile", "node_id"}, "Profile output binding")
        binding = cls(profile=data["profile"], node_id=data["node_id"])
        binding.validate()
        return binding


@dataclass(frozen=True, slots=True)
class AlbumPublicationPlan:
    album_reference: str
    album_sha256: str
    sides: tuple[PublicationSide, ...]
    selected_profiles: tuple[str, ...]
    nodes: tuple[ProcessingNode, ...]
    profile_outputs: tuple[ProfileOutput, ...]
    body_sha256: str
    plan_sha256: str
    schema: str = ALBUM_PUBLICATION_PLAN_SCHEMA

    def _body_dict(self) -> dict[str, Any]:
        return {
            "album_reference": self.album_reference,
            "album_sha256": self.album_sha256,
            "sides": [side.to_dict() for side in self.sides],
            "selected_profiles": list(self.selected_profiles),
            "dag": {
                "nodes": [node.to_dict() for node in self.nodes],
                "profile_outputs": [item.to_dict() for item in self.profile_outputs],
            },
        }

    def validate(self) -> None:
        if self.schema != ALBUM_PUBLICATION_PLAN_SCHEMA:
            raise ProjectValidationError(
                f"Publication plan schema must be {ALBUM_PUBLICATION_PLAN_SCHEMA!r}."
            )
        _relative_reference(self.album_reference, "Album project reference")
        _strict_digest(self.album_sha256, "Album project SHA-256")
        _validate_sides(self.sides)
        _validate_reference_uniqueness(self.album_reference, self.sides)
        _validate_profiles(self.selected_profiles, self.profile_outputs)
        _validate_dag(
            sides=self.sides,
            selected_profiles=self.selected_profiles,
            nodes=self.nodes,
            profile_outputs=self.profile_outputs,
        )
        expected_body = _canonical_sha256(self._body_dict())
        _strict_digest(self.body_sha256, "Publication plan body SHA-256")
        if self.body_sha256 != expected_body:
            raise ProjectValidationError(
                "Publication plan body SHA-256 does not match the canonical body."
            )
        expected_plan = _canonical_sha256(
            {
                "schema": self.schema,
                "body": self._body_dict(),
                "body_sha256": expected_body,
            }
        )
        _strict_digest(self.plan_sha256, "Publication plan SHA-256")
        if self.plan_sha256 != expected_plan:
            raise ProjectValidationError(
                "Publication plan SHA-256 does not match the canonical envelope."
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": self.schema,
            "body": self._body_dict(),
            "body_sha256": self.body_sha256,
            "plan_sha256": self.plan_sha256,
        }

    @classmethod
    def create(
        cls,
        *,
        album_reference: str,
        album_sha256: str,
        sides: Iterable[PublicationSide],
        selected_profiles: Iterable[str],
        nodes: Iterable[ProcessingNode],
        profile_outputs: Iterable[ProfileOutput],
    ) -> AlbumPublicationPlan:
        side_items = tuple(sides)
        if any(not isinstance(side, PublicationSide) for side in side_items):
            raise ProjectValidationError(
                "Publication sides must use the PublicationSide model."
            )
        normalized_sides = tuple(
            sorted(
                (side.normalized() for side in side_items),
                key=lambda side: side.order,
            )
        )
        profile_set = tuple(selected_profiles)
        if any(not isinstance(profile, str) for profile in profile_set):
            raise ProjectValidationError("Publication profile names must be text.")
        unsupported_profiles = set(profile_set) - set(_PROFILE_ORDER)
        if unsupported_profiles:
            unsupported = ", ".join(repr(item) for item in sorted(unsupported_profiles))
            raise ProjectValidationError(
                f"Unsupported publication profile(s): {unsupported}."
            )
        if len(set(profile_set)) != len(profile_set):
            raise ProjectValidationError("Selected publication profiles are duplicated.")
        normalized_profiles = tuple(
            profile for profile in _PROFILE_ORDER if profile in profile_set
        )
        node_items = tuple(nodes)
        if any(not isinstance(node, ProcessingNode) for node in node_items):
            raise ProjectValidationError(
                "Processing nodes must use the ProcessingNode model."
            )
        normalized_nodes = tuple(
            sorted(
                (node.normalized() for node in node_items),
                key=lambda node: node.node_id,
            )
        )
        output_items = tuple(profile_outputs)
        if any(not isinstance(output, ProfileOutput) for output in output_items):
            raise ProjectValidationError(
                "Profile outputs must use the ProfileOutput model."
            )
        normalized_outputs = tuple(
            sorted(
                output_items,
                key=lambda item: _PROFILE_ORDER.index(item.profile)
                if item.profile in _PROFILE_ORDER
                else len(_PROFILE_ORDER),
            )
        )
        provisional = cls(
            album_reference=album_reference,
            album_sha256=album_sha256,
            sides=normalized_sides,
            selected_profiles=normalized_profiles,
            nodes=normalized_nodes,
            profile_outputs=normalized_outputs,
            body_sha256="0" * 64,
            plan_sha256="0" * 64,
        )
        _relative_reference(album_reference, "Album project reference")
        _strict_digest(album_sha256, "Album project SHA-256")
        _validate_sides(normalized_sides)
        _validate_reference_uniqueness(album_reference, normalized_sides)
        _validate_profiles(normalized_profiles, normalized_outputs)
        _validate_dag(
            sides=normalized_sides,
            selected_profiles=normalized_profiles,
            nodes=normalized_nodes,
            profile_outputs=normalized_outputs,
        )
        body = provisional._body_dict()
        body_sha256 = _canonical_sha256(body)
        plan_sha256 = _canonical_sha256(
            {
                "schema": ALBUM_PUBLICATION_PLAN_SCHEMA,
                "body": body,
                "body_sha256": body_sha256,
            }
        )
        plan = cls(
            album_reference=album_reference,
            album_sha256=album_sha256,
            sides=normalized_sides,
            selected_profiles=normalized_profiles,
            nodes=normalized_nodes,
            profile_outputs=normalized_outputs,
            body_sha256=body_sha256,
            plan_sha256=plan_sha256,
        )
        plan.validate()
        return plan

    @classmethod
    def from_dict(cls, value: Any) -> AlbumPublicationPlan:
        envelope = _strict_object(value, "Publication plan")
        _strict_keys(
            envelope,
            {"schema", "body", "body_sha256", "plan_sha256"},
            "Publication plan",
        )
        if envelope["schema"] != ALBUM_PUBLICATION_PLAN_SCHEMA:
            raise ProjectValidationError(
                f"Publication plan schema must be {ALBUM_PUBLICATION_PLAN_SCHEMA!r}."
            )
        body = _strict_object(envelope["body"], "Publication plan body")
        _strict_keys(
            body,
            {"album_reference", "album_sha256", "sides", "selected_profiles", "dag"},
            "Publication plan body",
        )
        raw_sides = _strict_array(body["sides"], "Publication sides", maximum=_MAX_SIDES)
        raw_profiles = _strict_array(
            body["selected_profiles"],
            "Selected publication profiles",
            maximum=len(_PROFILE_ORDER),
        )
        dag = _strict_object(body["dag"], "Publication processing DAG")
        _strict_keys(dag, {"nodes", "profile_outputs"}, "Publication processing DAG")
        raw_nodes = _strict_array(dag["nodes"], "Processing nodes", maximum=_MAX_NODES)
        raw_outputs = _strict_array(
            dag["profile_outputs"],
            "Profile output bindings",
            maximum=len(_PROFILE_ORDER),
        )
        plan = cls.create(
            album_reference=body["album_reference"],
            album_sha256=body["album_sha256"],
            sides=(PublicationSide.from_dict(item) for item in raw_sides),
            selected_profiles=raw_profiles,
            nodes=(ProcessingNode.from_dict(item) for item in raw_nodes),
            profile_outputs=(ProfileOutput.from_dict(item) for item in raw_outputs),
        )
        if body != plan._body_dict():
            raise ProjectValidationError(
                "Publication plan body is not in canonical deterministic order."
            )
        body_sha256 = _strict_digest(
            envelope["body_sha256"], "Publication plan body SHA-256"
        )
        plan_sha256 = _strict_digest(
            envelope["plan_sha256"], "Publication plan SHA-256"
        )
        if body_sha256 != plan.body_sha256:
            raise ProjectValidationError(
                "Publication plan body SHA-256 does not match the canonical body."
            )
        if plan_sha256 != plan.plan_sha256:
            raise ProjectValidationError(
                "Publication plan SHA-256 does not match the canonical envelope."
            )
        return plan


def _validate_sides(sides: tuple[PublicationSide, ...]) -> None:
    if not sides:
        raise ProjectValidationError("A publication plan requires at least one side.")
    if len(sides) > _MAX_SIDES:
        raise ProjectValidationError(
            f"A publication plan cannot exceed {_MAX_SIDES} sides."
        )
    labels: set[str] = set()
    projects: set[str] = set()
    for expected_order, side in enumerate(sides, start=1):
        if not isinstance(side, PublicationSide):
            raise ProjectValidationError(
                "Publication sides must use the PublicationSide model."
            )
        side.validate()
        if side.order != expected_order:
            raise ProjectValidationError(
                "Publication side order must be consecutive, start at 1, and match list order."
            )
        folded_label = _portable_key(side.label)
        folded_project = _portable_key(side.project_reference)
        if folded_label in labels:
            raise ProjectValidationError(f"Duplicate publication side label {side.label!r}.")
        if folded_project in projects:
            raise ProjectValidationError(
                f"Duplicate side project reference {side.project_reference!r}."
            )
        labels.add(folded_label)
        projects.add(folded_project)


def _validate_reference_uniqueness(
    album_reference: str, sides: tuple[PublicationSide, ...]
) -> None:
    references: dict[str, str] = {
        _portable_key(album_reference): "Album project reference"
    }
    for side in sides:
        entries = [("Side project reference", side.project_reference)]
        if side.restoration_render is not None:
            entries.extend(
                [
                    (
                        "Restoration render manifest reference",
                        side.restoration_render.manifest_reference,
                    ),
                    (
                        "Restoration rendered audio reference",
                        side.restoration_render.audio_reference,
                    ),
                ]
            )
        if side.restoration_no_derivative is not None:
            entries.append(
                (
                    "No-derivative restoration scan reference",
                    side.restoration_no_derivative.scan_reference,
                )
            )
        for label, reference in entries:
            key = _portable_key(reference)
            prior = references.get(key)
            if prior is not None:
                raise ProjectValidationError(
                    f"{label} duplicates {prior.lower()}: {reference!r}."
                )
            references[key] = label


def _validate_profiles(
    profiles: tuple[str, ...], outputs: tuple[ProfileOutput, ...]
) -> None:
    if not profiles:
        raise ProjectValidationError(
            "A publication plan requires at least one explicitly selected profile."
        )
    if len(set(profiles)) != len(profiles):
        raise ProjectValidationError("Selected publication profiles are duplicated.")
    expected_profiles = tuple(profile for profile in _PROFILE_ORDER if profile in profiles)
    if profiles != expected_profiles or any(profile not in _PROFILE_ORDER for profile in profiles):
        raise ProjectValidationError(
            "Selected publication profiles must be supported and canonically ordered."
        )
    output_profiles: set[str] = set()
    for output in outputs:
        if not isinstance(output, ProfileOutput):
            raise ProjectValidationError(
                "Profile outputs must use the ProfileOutput model."
            )
        output.validate()
        if output.profile in output_profiles:
            raise ProjectValidationError(
                f"Duplicate output binding for profile {output.profile!r}."
            )
        output_profiles.add(output.profile)
    if output_profiles != set(profiles):
        raise ProjectValidationError(
            "Profile output bindings must exactly match the selected profiles."
        )
    if PROFILE_PORTABLE in profiles and PROFILE_CORRECTED_LOSSLESS not in profiles:
        raise ProjectValidationError(
            "The portable profile requires an explicit corrected-lossless profile dependency."
        )


def _require_inputs(
    node: ProcessingNode,
    expected: Mapping[str, ProcessingNode],
) -> None:
    actual = {item.role: item.node_id for item in node.inputs}
    expected_ids = {role: upstream.node_id for role, upstream in expected.items()}
    if actual != expected_ids:
        raise ProjectValidationError(
            f"Node {node.node_id!r} inputs do not match its explicit semantic dependencies."
        )


def _validate_dag(
    *,
    sides: tuple[PublicationSide, ...],
    selected_profiles: tuple[str, ...],
    nodes: tuple[ProcessingNode, ...],
    profile_outputs: tuple[ProfileOutput, ...],
) -> None:
    if not nodes:
        raise ProjectValidationError("A publication plan requires a processing DAG.")
    if len(nodes) > _MAX_NODES:
        raise ProjectValidationError(
            f"A publication plan cannot exceed {_MAX_NODES} processing nodes."
        )
    node_by_id: dict[str, ProcessingNode] = {}
    for node in nodes:
        if not isinstance(node, ProcessingNode):
            raise ProjectValidationError(
                "Processing nodes must use the ProcessingNode model."
            )
        node.validate()
        if node.node_id in node_by_id:
            raise ProjectValidationError(f"Duplicate processing node ID {node.node_id!r}.")
        node_by_id[node.node_id] = node
    for node in nodes:
        for item in node.inputs:
            if item.node_id not in node_by_id:
                raise ProjectValidationError(
                    f"Node {node.node_id!r} references missing dependency {item.node_id!r}."
                )
            if item.node_id == node.node_id:
                raise ProjectValidationError(
                    f"Node {node.node_id!r} cannot depend on itself."
                )

    state: dict[str, int] = {}

    def visit(node_id: str) -> None:
        marker = state.get(node_id, 0)
        if marker == 1:
            raise ProjectValidationError("Publication processing DAG contains a cycle.")
        if marker == 2:
            return
        state[node_id] = 1
        for item in node_by_id[node_id].inputs:
            visit(item.node_id)
        state[node_id] = 2

    for node_id in sorted(node_by_id):
        visit(node_id)

    side_by_label = {side.label: side for side in sides}
    side_source: dict[str, ProcessingNode] = {}
    side_restore: dict[str, ProcessingNode] = {}
    side_correct: dict[str, ProcessingNode] = {}
    aggregate_by_operation: dict[str, ProcessingNode] = {}
    for node in nodes:
        if node.operation in _SIDE_OPERATIONS:
            if node.side_label not in side_by_label:
                raise ProjectValidationError(
                    f"Node {node.node_id!r} references an unknown album side."
                )
            assert node.side_label is not None
            target = {
                "source-side": side_source,
                "restore-side": side_restore,
                "correct-speed-side": side_correct,
            }[node.operation]
            if node.side_label in target:
                raise ProjectValidationError(
                    f"Side {node.side_label!r} has duplicate {node.operation!r} nodes."
                )
            target[node.side_label] = node
        else:
            if node.operation in aggregate_by_operation:
                raise ProjectValidationError(
                    f"Processing DAG repeats album operation {node.operation!r}."
                )
            aggregate_by_operation[node.operation] = node

    if set(side_source) != set(side_by_label):
        raise ProjectValidationError(
            "Processing DAG requires exactly one source-side node for every album side."
        )
    for source in side_source.values():
        _require_inputs(source, {})
    for label, restore in side_restore.items():
        if side_by_label[label].restoration_render is None:
            raise ProjectValidationError(
                f"Restore node for side {label!r} requires a restoration render binding."
            )
        _require_inputs(restore, {"source": side_source[label]})
    render_outcome_sides = {
        side.label for side in sides if side.restoration_render is not None
    }
    no_derivative_outcome_sides = {
        side.label
        for side in sides
        if side.restoration_no_derivative is not None
    }
    restoration_outcome_sides = (
        render_outcome_sides | no_derivative_outcome_sides
    )
    if render_outcome_sides != set(side_restore):
        raise ProjectValidationError(
            "Restoration render bindings must exactly match the DAG restore-side nodes."
        )
    restoration_consumers_present = bool(side_correct) or (
        "assemble-restored" in aggregate_by_operation
    )
    if restoration_outcome_sides and restoration_outcome_sides != set(side_by_label):
        raise ProjectValidationError(
            "A restoration-aware DAG requires an explicit render or no-derivative "
            "outcome for every album side."
        )
    if restoration_outcome_sides and not restoration_consumers_present:
        raise ProjectValidationError(
            "Restoration outcomes are not consumed by this processing DAG."
        )
    for label, corrected in side_correct.items():
        if restoration_outcome_sides:
            upstream = (
                side_restore[label]
                if label in render_outcome_sides
                else side_source[label]
            )
        else:
            upstream = side_source[label]
        _require_inputs(corrected, {"audio": upstream})

    archival = aggregate_by_operation.get("assemble-archival")
    if archival is not None:
        _require_inputs(
            archival,
            {
                f"side-{side.order:03d}": side_source[side.label]
                for side in sides
            },
        )
    restored = aggregate_by_operation.get("assemble-restored")
    if restored is not None:
        if restoration_outcome_sides != set(side_by_label):
            raise ProjectValidationError(
                "The restored-side profile requires an explicit reviewed restoration "
                "outcome for every album side."
            )
        if not render_outcome_sides:
            raise ProjectValidationError(
                "The restored-side profile requires at least one rendered derivative; "
                "an all-clean outcome would be redundant."
            )
        _require_inputs(
            restored,
            {
                f"side-{side.order:03d}": (
                    side_restore[side.label]
                    if side.label in render_outcome_sides
                    else side_source[side.label]
                )
                for side in sides
            },
        )
    lossless = aggregate_by_operation.get("encode-lossless")
    if lossless is not None:
        if set(side_correct) != set(side_by_label):
            raise ProjectValidationError(
                "The corrected-lossless profile requires one explicit speed-correction "
                "dependency for every album side."
            )
        _require_inputs(
            lossless,
            {
                f"side-{side.order:03d}": side_correct[side.label]
                for side in sides
            },
        )
    portable = aggregate_by_operation.get("encode-portable")
    if portable is not None:
        if lossless is None:
            raise ProjectValidationError(
                "Portable processing requires an explicit corrected-lossless node."
            )
        _require_inputs(portable, {"lossless": lossless})

    output_by_profile = {output.profile: output for output in profile_outputs}
    for profile in selected_profiles:
        output = output_by_profile[profile]
        output_node = node_by_id.get(output.node_id)
        if output_node is None:
            raise ProjectValidationError(
                f"Profile {profile!r} references missing output node {output.node_id!r}."
            )
        if output_node.operation != _PROFILE_OPERATIONS[profile]:
            raise ProjectValidationError(
                f"Profile {profile!r} must bind to a {_PROFILE_OPERATIONS[profile]!r} node."
            )

    reachable: set[str] = set()

    def collect(node_id: str) -> None:
        if node_id in reachable:
            return
        reachable.add(node_id)
        for item in node_by_id[node_id].inputs:
            collect(item.node_id)

    for output in profile_outputs:
        collect(output.node_id)
    if reachable != set(node_by_id):
        unused = ", ".join(sorted(set(node_by_id) - reachable))
        raise ProjectValidationError(
            f"Processing DAG contains nodes not bound to a selected profile: {unused}."
        )


class _DuplicateJsonKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"Duplicate JSON object field {key!r}.")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _optional_stat_int(details: os.stat_result, name: str) -> int:
    value = getattr(details, name, None)
    return value if isinstance(value, int) else -1


def _stable_file_identity(details: os.stat_result) -> tuple[int, ...]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
        _optional_stat_int(details, "st_birthtime_ns"),
        _optional_stat_int(details, "st_file_attributes"),
    )


def _os_flag(name: str) -> int:
    value = getattr(os, name, 0)
    return value if isinstance(value, int) else 0


def _has_reparse_attribute(details: os.stat_result) -> bool:
    value = getattr(details, "st_file_attributes", 0)
    return isinstance(value, int) and bool(value & 0x0400)


def load_album_publication_plan_with_sha256(
    path: Path,
) -> tuple[AlbumPublicationPlan, str]:
    """Load one bounded, strict plan and return its raw file SHA-256."""

    supplied = path.expanduser()
    try:
        path_before = supplied.lstat()
        if stat.S_ISLNK(path_before.st_mode) or _has_reparse_attribute(path_before):
            raise ProjectValidationError(
                "Publication plan must not be a symbolic link or reparse point."
            )
        if not stat.S_ISREG(path_before.st_mode):
            raise ProjectValidationError("Publication plan must be a regular file.")
        if path_before.st_size > _MAX_PLAN_BYTES:
            raise ProjectValidationError(
                f"Publication plan exceeds {_MAX_PLAN_BYTES} bytes."
            )
        flags = os.O_RDONLY | _os_flag("O_BINARY") | _os_flag("O_NOFOLLOW")
        descriptor = os.open(supplied, flags)
        try:
            opened = os.fstat(descriptor)
            if not same_file_object_stats(opened, path_before):
                raise ProjectValidationError(
                    "Publication plan changed before it could be opened."
                )
            if not stat.S_ISREG(opened.st_mode):
                raise ProjectValidationError(
                    "Publication plan must be a regular file."
                )
            if opened.st_size > _MAX_PLAN_BYTES:
                raise ProjectValidationError(
                    f"Publication plan exceeds {_MAX_PLAN_BYTES} bytes."
                )
            handle = os.fdopen(descriptor, "rb")
            descriptor = -1
            with handle:
                raw = handle.read(_MAX_PLAN_BYTES + 1)
                after = os.fstat(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(raw) > _MAX_PLAN_BYTES:
            raise ProjectValidationError(
                f"Publication plan exceeds {_MAX_PLAN_BYTES} bytes."
            )
        path_after = supplied.lstat()
        if (
            _stable_file_identity(opened) != _stable_file_identity(after)
            or _stable_file_identity(path_before)
            != _stable_file_identity(path_after)
            or not same_file_object_stats(after, path_after)
            or len(raw) != opened.st_size
        ):
            raise ProjectValidationError(
                "Publication plan changed while it was being loaded."
            )
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
        return AlbumPublicationPlan.from_dict(data), hashlib.sha256(raw).hexdigest()
    except ProjectValidationError:
        raise
    except (
        AttributeError,
        KeyError,
        OSError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise ProjectValidationError(
            f"Album publication plan is invalid: {exc}"
        ) from exc


def load_album_publication_plan(path: Path) -> AlbumPublicationPlan:
    return load_album_publication_plan_with_sha256(path)[0]


def save_album_publication_plan(plan: AlbumPublicationPlan, path: Path) -> None:
    """Atomically create ``path`` without an ordinary overwrite mode."""

    if not isinstance(plan, AlbumPublicationPlan):
        raise ProjectValidationError(
            "Publication plan must use the AlbumPublicationPlan model."
        )
    plan.validate()
    # ``resolve`` would follow a dangling destination symlink and turn a safe
    # no-overwrite request into a write at the link target.  ``abspath`` keeps
    # the final path component intact so the preflight and hard-link commit
    # both fail closed when any entry already occupies it.
    destination = Path(os.path.abspath(os.fspath(path.expanduser())))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ProjectValidationError(
            f"Publication plan already exists: {destination}."
        )
    text = json.dumps(
        plan.to_dict(),
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    if len(text.encode("utf-8")) > _MAX_PLAN_BYTES:
        raise ProjectValidationError(
            f"Publication plan exceeds {_MAX_PLAN_BYTES} serialized bytes."
        )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            rename_no_replace(temporary, destination)
        except FileExistsError as exc:
            raise ProjectValidationError(
                f"Publication plan already exists: {destination}."
            ) from exc
        except OSError as exc:
            raise ProjectValidationError(
                "The filesystem cannot atomically create a no-overwrite publication plan."
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class IdentityMismatch:
    code: str
    side_label: str | None
    field: str
    expected: Any
    current: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "side_label": self.side_label,
            "field": self.field,
            "expected": self.expected,
            "current": self.current,
        }


@dataclass(frozen=True, slots=True)
class PlanIdentityVerification:
    ok: bool
    plan_sha256: str
    mismatches: tuple[IdentityMismatch, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "plan_sha256": self.plan_sha256,
            "mismatches": [item.to_dict() for item in self.mismatches],
        }


def verify_album_publication_plan_identity(
    plan: AlbumPublicationPlan,
    *,
    current_album_sha256: Any,
    current_side_identities: Mapping[str, SideIdentity | Mapping[str, Any]],
    current_side_speed_selections: Mapping[
        str, SpeedSelection | Mapping[str, Any]
    ],
) -> PlanIdentityVerification:
    """Compare one freshly inspected album state without approving it.

    Both mappings must come from the same inspection that produced
    ``current_album_sha256``.  Requiring the current speed selection separately
    prevents a caller from attaching arbitrary execution factors to an otherwise
    valid album and side identity.
    """

    plan.validate()
    mismatches: list[IdentityMismatch] = []
    try:
        current_album = _strict_digest(
            current_album_sha256, "Current album project SHA-256"
        )
    except ProjectValidationError:
        current_album = None
        mismatches.append(
            IdentityMismatch(
                code="current_album_identity_invalid",
                side_label=None,
                field="album_sha256",
                expected=plan.album_sha256,
                current=None,
            )
        )
    if current_album is not None and current_album != plan.album_sha256:
        mismatches.append(
            IdentityMismatch(
                code="album_sha256_mismatch",
                side_label=None,
                field="album_sha256",
                expected=plan.album_sha256,
                current=current_album,
            )
        )

    expected_by_label = {side.label: side for side in plan.sides}
    raw_identity_by_label = dict(current_side_identities)
    raw_speed_by_label = dict(current_side_speed_selections)
    for label, expected_side in expected_by_label.items():
        expected = expected_side.current_identity
        raw = raw_identity_by_label.get(label)
        if raw is None:
            mismatches.append(
                IdentityMismatch(
                    code="side_missing",
                    side_label=label,
                    field="side_label",
                    expected=label,
                    current=None,
                )
            )
        else:
            try:
                current = (
                    raw
                    if isinstance(raw, SideIdentity)
                    else SideIdentity.from_dict(raw)
                )
                current.validate()
            except (ProjectValidationError, TypeError, ValueError):
                mismatches.append(
                    IdentityMismatch(
                        code="current_side_identity_invalid",
                        side_label=label,
                        field="current_identity",
                        expected=expected.to_dict(),
                        current=None,
                    )
                )
            else:
                for field_name in _IDENTITY_FIELDS:
                    expected_value = getattr(expected, field_name)
                    current_value = getattr(current, field_name)
                    if current_value != expected_value:
                        mismatches.append(
                            IdentityMismatch(
                                code=f"side_{field_name}_mismatch",
                                side_label=label,
                                field=field_name,
                                expected=expected_value,
                                current=current_value,
                            )
                        )

        expected_speed = SpeedSelection(
            selected_speed_state_sha256=(
                expected_side.selected_speed_state_sha256
            ),
            selected_effective_speed_factor=(
                expected_side.selected_effective_speed_factor
            ),
        ).normalized()
        raw_speed = raw_speed_by_label.get(label)
        if raw_speed is None:
            mismatches.append(
                IdentityMismatch(
                    code="side_speed_selection_missing",
                    side_label=label,
                    field="speed_selection",
                    expected=expected_speed.to_dict(),
                    current=None,
                )
            )
        else:
            try:
                current_speed = (
                    raw_speed.normalized()
                    if isinstance(raw_speed, SpeedSelection)
                    else SpeedSelection.from_dict(raw_speed)
                )
            except (ProjectValidationError, TypeError, ValueError):
                mismatches.append(
                    IdentityMismatch(
                        code="current_side_speed_selection_invalid",
                        side_label=label,
                        field="speed_selection",
                        expected=expected_speed.to_dict(),
                        current=None,
                    )
                )
            else:
                for field_name in (
                    "selected_speed_state_sha256",
                    "selected_effective_speed_factor",
                ):
                    expected_value = getattr(expected_speed, field_name)
                    current_value = getattr(current_speed, field_name)
                    if current_value != expected_value:
                        mismatches.append(
                            IdentityMismatch(
                                code=f"side_{field_name}_mismatch",
                                side_label=label,
                                field=field_name,
                                expected=expected_value,
                                current=current_value,
                            )
                        )
    current_labels = set(raw_identity_by_label) | set(raw_speed_by_label)
    for label in sorted(current_labels - set(expected_by_label), key=str):
        mismatches.append(
            IdentityMismatch(
                code="side_unexpected",
                side_label=label if isinstance(label, str) else None,
                field="side_label",
                expected=None,
                current=label if isinstance(label, str) else None,
            )
        )
    return PlanIdentityVerification(
        ok=not mismatches,
        plan_sha256=plan.plan_sha256,
        mismatches=tuple(mismatches),
    )


__all__ = [
    "ALBUM_PUBLICATION_PLAN_SCHEMA",
    "PROFILE_ARCHIVAL_SOURCE",
    "PROFILE_CORRECTED_LOSSLESS",
    "PROFILE_PORTABLE",
    "PROFILE_RESTORED_SIDE",
    "RESTORATION_NO_DERIVATIVE_SCHEMA",
    "RESTORATION_RENDER_SCHEMA",
    "RESTORATION_SCAN_SCHEMA",
    "AlbumPublicationPlan",
    "IdentityMismatch",
    "PlanIdentityVerification",
    "ProcessingInput",
    "ProcessingNode",
    "ProfileOutput",
    "PublicationSide",
    "RestorationNoDerivativeBinding",
    "RestorationRenderBinding",
    "SideIdentity",
    "SpeedSelection",
    "ToolBinding",
    "load_album_publication_plan",
    "load_album_publication_plan_with_sha256",
    "save_album_publication_plan",
    "verify_album_publication_plan_identity",
]
