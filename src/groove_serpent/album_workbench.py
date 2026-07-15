"""Deterministic, read-only state for the album review workbench."""

from __future__ import annotations

import copy
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from .album import AlbumProject, inspect_album_project, project_speed_state
from .album_identification_catalog import (
    discover_album_identification_proposal_catalog,
)
from .album_publication_catalog import (
    PUBLICATION_PLAN_FILENAME_SUFFIX,
    discover_album_publication_plan_catalog,
)
from .album_publication_plan import (
    PROFILE_ARCHIVAL_SOURCE,
    PROFILE_CORRECTED_LOSSLESS,
    PROFILE_PORTABLE,
    PROFILE_RESTORED_SIDE,
)
from .album_publication_operations import discover_album_publication_operations
from .errors import ProjectValidationError
from .project_io import load_project_with_sha256
from .recognition import NoRecognitionProvider, RecognitionReadiness


ALBUM_WORKBENCH_SCHEMA = "groove-serpent.album-workbench/4"

_METADATA_FIELDS = ("artist", "album", "album_artist", "year", "genre")
_DRIFT_TYPES = {
    "side is unpinned": "side_unpinned",
    "project revision changed": "project_revision_changed",
    "project file changed": "project_file_changed",
    "editable project state changed": "editable_project_state_changed",
    "source audio changed": "source_audio_changed",
    "reviewed project speed state changed": "reviewed_project_speed_state_changed",
    "album speed selection changed": "album_speed_selection_changed",
    "source no longer matches the side project": "source_project_mismatch",
}
_DRIFT_TITLES = {
    "side_unpinned": "Side is not pinned",
    "project_revision_changed": "Side project revision changed",
    "project_file_changed": "Side project file changed",
    "editable_project_state_changed": "Editable side state changed",
    "source_audio_changed": "Source audio changed",
    "reviewed_project_speed_state_changed": "Reviewed project speed changed",
    "album_speed_selection_changed": "Album speed selection changed",
    "source_project_mismatch": "Source does not match its side project",
}
_DRIFT_FIELDS = {
    "project_revision_changed": "project_revision",
    "project_file_changed": "project_sha256",
    "editable_project_state_changed": "editable_state_sha256",
    "source_audio_changed": "source_sha256",
    "reviewed_project_speed_state_changed": "project_speed_state_sha256",
    "album_speed_selection_changed": "speed_state_sha256",
}

_PUBLICATION_PROFILES = (
    {
        "id": PROFILE_ARCHIVAL_SOURCE,
        "label": "Archival source objects",
        "description": (
            "One verified byte-identical full-capture object per unique exact source "
            "identity, with deterministic side bindings in the publication receipt."
        ),
        "requires_reviewed_restoration": False,
    },
    {
        "id": PROFILE_RESTORED_SIDE,
        "label": "Reviewed restored sides",
        "description": "Continuous reviewed restoration derivatives when available.",
        "requires_reviewed_restoration": True,
    },
    {
        "id": PROFILE_CORRECTED_LOSSLESS,
        "label": "Corrected lossless tracks",
        "description": "Exact reviewed tracks with the selected side speed applied.",
        "requires_reviewed_restoration": False,
    },
    {
        "id": PROFILE_PORTABLE,
        "label": "Portable AAC tracks",
        "description": "Portable tracks derived from the corrected-lossless timeline.",
        "requires_reviewed_restoration": False,
    },
)


def _default_plan_filename(album_path: Path, album_sha256: str) -> str:
    safe_stem = (
        "-".join(
            part
            for part in "".join(
                character if character.isascii() and character.isalnum() else "-"
                for character in album_path.stem
            ).split("-")
            if part
        )[:80]
        or "album"
    )
    return f"{safe_stem}-{album_sha256[:12]}{PUBLICATION_PLAN_FILENAME_SUFFIX}"


def _default_publication_directory(album_path: Path, album_sha256: str) -> str:
    safe_stem = (
        "-".join(
            part
            for part in "".join(
                character if character.isascii() and character.isalnum() else "-"
                for character in album_path.stem.removesuffix(".groove-album")
            ).split("-")
            if part
        )[:72]
        or "album"
    )
    return f"{safe_stem}-publication-{album_sha256[:12]}"


def _normalized_metadata(value: str) -> str:
    """Normalize presentation-only differences before comparing metadata."""

    return unicodedata.normalize("NFC", " ".join(value.split())).casefold()


def _first_nonblank(metadata: Mapping[str, str], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key, "")
        if _normalized_metadata(value):
            return value
    return ""


def _metadata_value(metadata: Mapping[str, str], field: str) -> str:
    if field == "album":
        return _first_nonblank(metadata, "album", "title")
    return _first_nonblank(metadata, field)


def _exception(
    *,
    exception_id: str,
    exception_type: str,
    severity: str,
    title: str,
    message: str,
    side_order: int | None,
    side_label: str | None,
    field: str | None,
    evidence: Mapping[str, Any],
    actions: list[str],
) -> dict[str, Any]:
    return {
        "id": exception_id,
        "type": exception_type,
        "severity": severity,
        "title": title,
        "message": message,
        "side_order": side_order,
        "side_label": side_label,
        "field": field,
        "evidence": dict(evidence),
        "actions": list(actions),
    }


def _drift_evidence(
    exception_type: str,
    side: Mapping[str, Any],
    project_source_sha256: str,
) -> dict[str, Any]:
    pin = side.get("pin")
    current = side["current_identity"]
    if not isinstance(current, dict):
        raise ProjectValidationError("Album inspection returned an invalid identity.")
    if exception_type == "side_unpinned":
        return {"pinned": None, "current": dict(current)}
    if exception_type == "source_project_mismatch":
        return {
            "pinned": project_source_sha256,
            "current": current["source_sha256"],
        }
    field = _DRIFT_FIELDS[exception_type]
    if exception_type == "album_speed_selection_changed":
        pinned = pin.get(field) if isinstance(pin, dict) else None
        return {
            "pinned": pinned,
            "current": side["selected_speed_state_sha256"],
        }
    pinned = pin.get(field) if isinstance(pin, dict) else None
    return {"pinned": pinned, "current": current[field]}


def _drift_actions(exception_type: str) -> list[str]:
    if exception_type == "source_project_mismatch":
        return ["inspect_source"]
    return ["review_side", "repin_side"]


def _missing_metadata_exceptions(metadata: Mapping[str, str]) -> list[dict[str, Any]]:
    exceptions: list[dict[str, Any]] = []
    required = (
        (
            "album_artist",
            _first_nonblank(metadata, "album_artist", "artist"),
            "Album artist is missing",
        ),
        (
            "album_title",
            _first_nonblank(metadata, "album", "title"),
            "Album title is missing",
        ),
    )
    for field, value, title in required:
        if _normalized_metadata(value):
            continue
        exceptions.append(
            _exception(
                exception_id=f"album:missing-{field.replace('_', '-')}",
                exception_type="missing_album_metadata",
                severity="blocker",
                title=title,
                message=f"Add the {field.replace('_', ' ')} before exporting the album.",
                side_order=None,
                side_label=None,
                field=field,
                evidence={"current": value},
                actions=["edit_album_metadata"],
            )
        )
    return exceptions


def build_album_workbench_state(
    album: AlbumProject,
    album_path: Path,
    *,
    recognition_readiness: RecognitionReadiness | None = None,
) -> dict[str, Any]:
    """Return the stable review state without changing the project or its files.

    ``inspect_album_project`` remains the authority for album validation, artwork
    verification, and every pinned/current identity comparison.  The workbench
    operates on a deep copy so even validation-time canonicalization cannot mutate
    the caller's in-memory project.
    """

    inspected = inspect_album_project(copy.deepcopy(album), album_path)
    metadata = dict(inspected["metadata"])
    exceptions = _missing_metadata_exceptions(metadata)
    side_summaries: list[dict[str, Any]] = []

    raw_sides = inspected["sides"]
    if not isinstance(raw_sides, list):
        raise ProjectValidationError("Album inspection returned invalid side data.")
    for raw_side in raw_sides:
        if not isinstance(raw_side, dict):
            raise ProjectValidationError("Album inspection returned an invalid side.")
        side = dict(raw_side)
        current = side.pop("current", None)
        if not isinstance(current, dict):
            raise ProjectValidationError("Album inspection returned an invalid identity.")
        side["current_identity"] = dict(current)
        side_summaries.append(side)

        order = side["order"]
        label = side["label"]
        if type(order) is not int or not isinstance(label, str):
            raise ProjectValidationError("Album inspection returned invalid side context.")
        resolved_project = side["resolved_project"]
        if not isinstance(resolved_project, str):
            raise ProjectValidationError("Album inspection returned an invalid project path.")
        project, project_sha256 = load_project_with_sha256(Path(resolved_project))
        if project_sha256 != current["project_sha256"]:
            raise ProjectValidationError(
                f"Side {label} project changed while the workbench state was being built."
            )

        raw_drift = side["drift"]
        if not isinstance(raw_drift, list) or not all(
            isinstance(reason, str) for reason in raw_drift
        ):
            raise ProjectValidationError("Album inspection returned invalid drift data.")
        for reason in raw_drift:
            exception_type = _DRIFT_TYPES.get(reason)
            if exception_type is None:
                raise ProjectValidationError(
                    f"Album inspection returned an unsupported drift reason: {reason}."
                )
            exceptions.append(
                _exception(
                    exception_id=f"side:{order:03d}:{exception_type.replace('_', '-')}",
                    exception_type=exception_type,
                    severity="blocker",
                    title=_DRIFT_TITLES[exception_type],
                    message=f"Side {label}: {reason}.",
                    side_order=order,
                    side_label=label,
                    field=_DRIFT_FIELDS.get(exception_type),
                    evidence=_drift_evidence(exception_type, side, project.source.sha256.lower()),
                    actions=_drift_actions(exception_type),
                )
            )

        if bool(side["speed_override_differs_from_project"]):
            current_project_speed = project_speed_state(project)
            exceptions.append(
                _exception(
                    exception_id=f"side:{order:03d}:speed-override-differs-from-project",
                    exception_type="speed_override_differs_from_project",
                    severity="review",
                    title="Explicit speed override differs from the side project",
                    message=(
                        f"Side {label} uses an explicit speed state instead of the "
                        "reviewed side-project speed."
                    ),
                    side_order=order,
                    side_label=label,
                    field="speed_state_sha256",
                    evidence={
                        "selected_speed_state_sha256": side["selected_speed_state_sha256"],
                        "project_speed_state_sha256": side["project_speed_state_sha256"],
                        "selected_effective_speed_factor": side["effective_speed_factor"],
                        "project_effective_speed_factor": (
                            current_project_speed.effective_speed_factor
                        ),
                    },
                    actions=["review_speed", "inherit_project_speed"],
                )
            )

        for field in _METADATA_FIELDS:
            album_value = _metadata_value(metadata, field)
            side_value = _metadata_value(project.metadata, field)
            if (
                not _normalized_metadata(album_value)
                or not _normalized_metadata(side_value)
                or _normalized_metadata(album_value) == _normalized_metadata(side_value)
            ):
                continue
            exceptions.append(
                _exception(
                    exception_id=f"side:{order:03d}:metadata:{field.replace('_', '-')}",
                    exception_type="album_side_metadata_conflict",
                    severity="review",
                    title=f"Side project {field.replace('_', ' ')} differs",
                    message=(
                        f"Side {label} and the album project have different "
                        f"{field.replace('_', ' ')} metadata."
                    ),
                    side_order=order,
                    side_label=label,
                    field=field,
                    evidence={"album": album_value, "side_project": side_value},
                    actions=["resolve_metadata"],
                )
            )

    blocker_count = sum(item["severity"] == "blocker" for item in exceptions)
    review_count = sum(item["severity"] == "review" for item in exceptions)
    sides_ready = sum(bool(side["ready_for_export"]) for side in side_summaries)
    summary = {
        "total": len(exceptions),
        "blockers": blocker_count,
        "reviews": review_count,
        "sides_ready": sides_ready,
        "sides_blocked": len(side_summaries) - sides_ready,
    }
    album_sha256 = inspected["album_project_sha256"]
    if not isinstance(album_sha256, str):
        raise ProjectValidationError("Album inspection returned an invalid SHA-256.")
    identification_catalog = discover_album_identification_proposal_catalog(
        album_path,
        expected_album_sha256=album_sha256,
    )
    identification_catalog_payload = identification_catalog.to_dict()
    identification_catalog_summary = identification_catalog_payload["summary"]
    if not isinstance(identification_catalog_summary, dict):
        raise RuntimeError("Identification catalog returned an invalid summary.")
    provider_readiness = recognition_readiness or NoRecognitionProvider().readiness()
    provider_payload = provider_readiness.to_dict()
    identification_reason_codes: list[str] = []
    if not identification_catalog.live_context_available:
        identification_reason_codes.append("current_album_context_unavailable")
    if not identification_catalog.scan_complete:
        identification_reason_codes.append("proposal_catalog_incomplete")
    if not provider_readiness.ready:
        identification_reason_codes.append("recognition_provider_not_ready")
    identification = {
        "readiness": {
            "can_scan": not identification_reason_codes,
            "reason_codes": identification_reason_codes,
        },
        "provider": provider_payload,
        "catalog": identification_catalog_payload,
        "authority": {
            "automatic_network_requests": False,
            "explicit_network_review_required": True,
            "automatic_metadata_application": False,
            "automatic_artwork_download_or_application": False,
            "may_modify_album_project": False,
            "may_modify_side_projects": False,
            "physical_pressing_proven": False,
            "human_review_required": True,
        },
    }
    publication_catalog = discover_album_publication_plan_catalog(
        album_path,
        expected_album_sha256=album_sha256,
    )
    catalog_payload = publication_catalog.to_dict()
    catalog_summary = catalog_payload["summary"]
    if not isinstance(catalog_summary, dict):
        raise RuntimeError("Publication catalog returned an invalid summary.")
    operation_catalog = discover_album_publication_operations(
        album_path,
        publication_catalog,
        expected_album_sha256=album_sha256,
    )
    operations_payload = operation_catalog.to_dict()
    operations_summary = operations_payload["summary"]
    if not isinstance(operations_summary, dict):
        raise RuntimeError("Publication operation catalog returned an invalid summary.")
    readiness_reasons: list[str] = []
    if blocker_count:
        readiness_reasons.append("album_has_blockers")
    if not publication_catalog.scan_complete:
        readiness_reasons.append("plan_catalog_incomplete")
    execution_reasons = list(readiness_reasons)
    if not operation_catalog.scan_complete:
        execution_reasons.append("operation_catalog_incomplete")
    if not catalog_summary["current"]:
        execution_reasons.append("no_current_plan")
    can_execute = not execution_reasons
    publication = {
        "readiness": {
            "can_create_plan": not readiness_reasons,
            "can_preflight_current_plan": bool(catalog_summary["current"]),
            "can_execute_current_plan": can_execute,
            "can_verify_publication": bool(
                operations_summary["current"] or operations_summary["stale"]
            ),
            "can_replay_current_publication": bool(can_execute and operations_summary["current"]),
            "can_recover_owned_orphan": bool(
                operation_catalog.scan_complete and operations_summary["actionable_orphans"]
            ),
            "reason_codes": readiness_reasons,
            "execution_reason_codes": execution_reasons,
        },
        "choices": {
            "profiles": [dict(item) for item in _PUBLICATION_PROFILES],
            "restoration_modes": [
                {
                    "id": "none",
                    "label": "No restoration input",
                    "description": (
                        "Use reviewed source endpoints and speed without a restoration derivative."
                    ),
                },
                {
                    "id": "reviewed",
                    "label": "Reviewed restoration outcomes",
                    "description": (
                        "Require each side's exact current reviewed restoration render "
                        "or reviewed clean outcome."
                    ),
                },
            ],
            "flac_compression": {"default": 8, "minimum": 0, "maximum": 12},
            "aac_bitrate_kbps": {"default": 256, "minimum": 64, "maximum": 512},
        },
        "default_plan_filename": _default_plan_filename(album_path, album_sha256),
        "default_destination_name": _default_publication_directory(
            album_path,
            album_sha256,
        ),
        "catalog": catalog_payload,
        "operations": operations_payload,
        "authority": {
            "automatic_plan_creation": False,
            "review_required": True,
            "automatic_execution": False,
            "owner_confirmation_required": True,
            "execution_available_here": True,
            "overwrite_allowed": False,
            "destinations_restricted_to_album_folder": True,
            "resume_available": False,
        },
    }
    return {
        "schema": ALBUM_WORKBENCH_SCHEMA,
        "album_project": inspected["album_project"],
        "album_project_sha256": inspected["album_project_sha256"],
        "album_revision": album.revision,
        "side_order_policy": {
            "approval_relevant": True,
            "reorder_invalidates_all_side_pins": True,
            "reason": (
                "Side order determines continuous album numbering and publication "
                "order, so every reordered side must be reviewed and repinned."
            ),
        },
        "metadata": metadata,
        "artwork": copy.deepcopy(inspected["artwork"]),
        "total_tracks": inspected["total_tracks"],
        "total_sides": len(side_summaries),
        "ready_for_export": blocker_count == 0,
        "summary": summary,
        "sides": side_summaries,
        "exceptions": exceptions,
        "identification": identification,
        "publication": publication,
    }
