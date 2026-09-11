"""Durable, non-authorizing owner decisions for restoration candidates.

The journal records exact owner-channel choices while a scan is still under
review.  It is intentionally not a restoration recipe: pending approvals do
not survive as render authority, and every later recipe must bind this exact
journal plus fresh in-process approval capabilities.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, cast

from . import __version__
from .errors import ProjectValidationError
from .migration_commit import prepare_replacement, read_plain_bound
from .models import utc_now_iso
from .publication import canonical_json_sha256
from .strict_json import decode_strict_json
from .transaction_lock import exclusive_target_write_lease


DECISION_JOURNAL_SCHEMA = "groove-serpent.restoration-decision-journal/1"
DECISION_JOURNAL_AUTHORITY = {
    "channel": "same-origin-owner-cookie",
    "scope": "non-authorizing-partial-decisions",
    "claim": "owner-channel-action-not-human-perception",
}
MAX_DECISION_JOURNAL_BYTES = 2_000_000
_DECISIONS = {"rejected", "protected", "pending-approval", "undecided"}
_PROTECTED_CLASSIFICATIONS = {
    "needle-drop",
    "needle-pickup",
    "handling-event",
    "other-structural-event",
}


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ProjectValidationError(f"{label} has unsupported or missing fields.")
    return cast(dict[str, Any], value)


def _text(value: Any, label: str, *, maximum: int = 4_096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProjectValidationError(f"{label} is invalid.")
    return value


def _digest(value: Any, label: str) -> str:
    rendered = _text(value, label, maximum=64)
    if len(rendered) != 64 or any(character not in "0123456789abcdef" for character in rendered):
        raise ProjectValidationError(f"{label} must be a lowercase SHA-256 digest.")
    return rendered


def _token(value: Any, prefix: str, label: str) -> str:
    rendered = _text(value, label, maximum=80)
    expected = f"{prefix}-"
    suffix = rendered[len(expected) :] if rendered.startswith(expected) else ""
    if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
        raise ProjectValidationError(f"{label} is invalid.")
    return rendered


def _positive_integer(value: Any, label: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise ProjectValidationError(f"{label} must be an integer of at least {minimum}.")
    return value


def validate_decision_journal(value: Any) -> dict[str, Any]:
    """Validate and normalize one strict, self-hashed decision journal."""

    journal = _exact(
        value,
        {
            "schema",
            "updated_at",
            "app_version",
            "project",
            "source",
            "scan",
            "decisions",
            "authority",
            "body_sha256",
        },
        "Restoration decision journal",
    )
    if journal["schema"] != DECISION_JOURNAL_SCHEMA:
        raise ProjectValidationError("The restoration decision journal schema is unsupported.")
    _text(journal["updated_at"], "Decision journal timestamp", maximum=64)
    _text(journal["app_version"], "Decision journal app version", maximum=200)

    project = _exact(
        journal["project"],
        {"path", "revision", "state_sha256", "sha256"},
        "Decision journal project binding",
    )
    project_path = _text(project["path"], "Decision journal project path", maximum=255)
    if Path(project_path).name != project_path or project_path in {".", ".."}:
        raise ProjectValidationError("The decision journal project path is unsafe.")
    _positive_integer(project["revision"], "Decision journal project revision", allow_zero=True)
    _digest(project["state_sha256"], "Decision journal project state SHA-256")
    _digest(project["sha256"], "Decision journal project SHA-256")

    source = _exact(
        journal["source"],
        {
            "path",
            "sha256",
            "size_bytes",
            "sample_rate",
            "channels",
            "bits_per_raw_sample",
            "sample_count",
            "codec_name",
        },
        "Decision journal source binding",
    )
    source_path = _text(source["path"], "Decision journal source path", maximum=255)
    if Path(source_path).name != source_path or source_path in {".", ".."}:
        raise ProjectValidationError("The decision journal source path is unsafe.")
    _digest(source["sha256"], "Decision journal source SHA-256")
    for key in (
        "size_bytes",
        "sample_rate",
        "channels",
        "bits_per_raw_sample",
        "sample_count",
    ):
        _positive_integer(source[key], f"Decision journal source {key}")
    _text(source["codec_name"], "Decision journal source codec", maximum=100)

    scan = _exact(
        journal["scan"],
        {"token", "sha256"},
        "Decision journal scan binding",
    )
    _token(scan["token"], "scan", "Decision journal scan token")
    _digest(scan["sha256"], "Decision journal scan SHA-256")

    decisions = journal["decisions"]
    if type(decisions) is not list or len(decisions) > 10_000:
        raise ProjectValidationError("Decision journal entries must be a bounded array.")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in decisions:
        decision_value = raw.get("decision") if type(raw) is dict else None
        keys = {
            "candidate_id",
            "candidate_sha256",
            "decision",
            "preview",
        }
        if decision_value == "protected":
            keys.add("classification")
        entry = _exact(raw, keys, "Decision journal entry")
        candidate_id = _text(entry["candidate_id"], "Decision candidate ID", maximum=160)
        if not candidate_id.startswith("clk-") or candidate_id in seen:
            raise ProjectValidationError(
                "Decision journal candidate IDs are invalid or duplicated."
            )
        seen.add(candidate_id)
        _digest(entry["candidate_sha256"], "Decision candidate SHA-256")
        if decision_value not in _DECISIONS:
            raise ProjectValidationError("The decision journal contains an unsupported choice.")
        if decision_value == "protected" and entry.get("classification") not in (
            _PROTECTED_CLASSIFICATIONS
        ):
            raise ProjectValidationError("A protected decision needs a structural classification.")
        preview = _exact(
            entry["preview"],
            {"token", "sha256"},
            "Decision preview binding",
        )
        _token(preview["token"], "preview", "Decision preview token")
        _digest(preview["sha256"], "Decision preview SHA-256")
        normalized.append(dict(entry))
    if normalized != sorted(normalized, key=lambda item: cast(str, item["candidate_id"])):
        raise ProjectValidationError("Decision journal entries must use canonical candidate order.")

    if journal["authority"] != DECISION_JOURNAL_AUTHORITY:
        raise ProjectValidationError("The decision journal authority claim is unsupported.")
    body_sha256 = _digest(journal["body_sha256"], "Decision journal body SHA-256")
    body = {key: value for key, value in journal.items() if key != "body_sha256"}
    if canonical_json_sha256(body) != body_sha256:
        raise ProjectValidationError("The decision journal body hash does not match its contents.")
    return dict(journal)


def seal_decision_journal(
    *,
    project: Mapping[str, Any],
    source: Mapping[str, Any],
    scan: Mapping[str, Any],
    decisions: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create one canonical self-hashed snapshot of partial owner decisions."""

    body: dict[str, Any] = {
        "schema": DECISION_JOURNAL_SCHEMA,
        "updated_at": utc_now_iso(),
        "app_version": __version__,
        "project": dict(project),
        "source": dict(source),
        "scan": dict(scan),
        "decisions": sorted(
            (dict(item) for item in decisions),
            key=lambda item: cast(str, item["candidate_id"]),
        ),
        "authority": dict(DECISION_JOURNAL_AUTHORITY),
    }
    sealed = {**body, "body_sha256": canonical_json_sha256(body)}
    return validate_decision_journal(sealed)


def decision_journal_bytes(journal: Mapping[str, Any]) -> bytes:
    validated = validate_decision_journal(dict(journal))
    return (
        json.dumps(validated, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")


def read_decision_journal(path: Path) -> tuple[dict[str, Any], str]:
    """Read a fixed journal path without following links or accepting drift."""

    raw, _identity = read_plain_bound(path, MAX_DECISION_JOURNAL_BYTES)
    try:
        value = decode_strict_json(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectValidationError("The restoration decision journal is invalid JSON.") from exc
    return validate_decision_journal(value), hashlib.sha256(raw).hexdigest()


def write_decision_journal(
    path: Path,
    journal: Mapping[str, Any],
    *,
    expected_file_sha256: str | None,
) -> str:
    """Atomically replace one cooperative journal after an exact compare step."""

    raw = decision_journal_bytes(journal)
    digest = hashlib.sha256(raw).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.resolve() != path.parent.absolute():
        raise ProjectValidationError(
            "The restoration decision workspace became a link or reparse point."
        )
    with exclusive_target_write_lease(path) as lease:
        exists = os.path.lexists(path)
        if exists:
            current_raw, _identity = read_plain_bound(path, MAX_DECISION_JOURNAL_BYTES)
            current_sha256 = hashlib.sha256(current_raw).hexdigest()
        else:
            current_sha256 = None
        if current_sha256 != expected_file_sha256:
            raise ProjectValidationError(
                "The restoration decision journal changed in another process; reload it."
            )
        prepared = prepare_replacement(
            path,
            raw,
            maximum=MAX_DECISION_JOURNAL_BYTES,
            purpose="restoration-decision-journal",
        )
        try:
            lease.assert_current()
            os.replace(prepared.path, path)
            if not prepared.matches_target(path):
                raise ProjectValidationError(
                    "The descriptor-bound restoration decision journal was not installed."
                )
            lease.assert_current()
        finally:
            prepared.discard()
    reread, installed_sha256 = read_decision_journal(path)
    if reread != dict(journal) or installed_sha256 != digest:
        raise ProjectValidationError(
            "The installed restoration decision journal differs from the requested state."
        )
    return digest


def decision_journal_token(file_sha256: str) -> str:
    _digest(file_sha256, "Decision journal file SHA-256")
    return f"decision-{file_sha256[:32]}"


__all__ = [
    "DECISION_JOURNAL_AUTHORITY",
    "DECISION_JOURNAL_SCHEMA",
    "MAX_DECISION_JOURNAL_BYTES",
    "decision_journal_token",
    "read_decision_journal",
    "seal_decision_journal",
    "validate_decision_journal",
    "write_decision_journal",
]
