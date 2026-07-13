"""Reversible, metadata-driven track-topology proposals.

The functions in this module deliberately do not save or mutate a project.  A
proposal is a deterministic, hash-bound description of a possible replacement
topology.  Callers can persist the proposal and the prior project state, review
the evidence, and only then ask :func:`tracks_from_topology_proposal` for fresh
``Track`` objects.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, replace
import hashlib
import json
import math
import re
import uuid
from typing import Any

from .analysis import select_boundaries
from .errors import ProjectValidationError
from .models import BoundaryCandidate, Project, Track
from .validation import strict_finite_number


TOPOLOGY_PROPOSAL_SCHEMA = "groove-serpent.topology-proposal/1"

_MAX_TRACKS = 500
_MAX_TEXT = 500
_MAX_SIDE_TEXT = 32
_MAX_NUMBER_TEXT = 64
_MAX_DURATION_SECONDS = 24.0 * 60.0 * 60.0
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_PROVIDER_KEYS = {
    "position",
    "number",
    "title",
    "artist",
    "album",
    "album_artist",
    "year",
    "genre",
    "side",
    "side_position",
    "duration_ms",
    "duration_seconds",
    "expected_duration_seconds",
    "recording_id",
    "track_id",
    "musicbrainz_recording_id",
    "musicbrainz_track_id",
}

_PROPOSAL_KEYS = {
    "schema",
    "proposal_id",
    "proposal_sha256",
    "binding",
    "operation",
    "music_range",
    "minimum_track_seconds",
    "minimum_track_samples",
    "release_tracks",
    "boundaries",
    "tracks",
    "warnings",
    "uncertain",
}


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProjectValidationError(
            "Topology data must contain only finite JSON values."
        ) from exc


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_text(
    value: Any,
    label: str,
    *,
    required: bool = False,
    maximum: int = _MAX_TEXT,
) -> str:
    if type(value) is not str:
        raise ProjectValidationError(f"{label} must be text.")
    rendered = value.strip()
    if required and not rendered:
        raise ProjectValidationError(f"{label} must be non-empty text.")
    if len(rendered) > maximum:
        raise ProjectValidationError(
            f"{label} exceeds the supported {maximum}-character limit."
        )
    return rendered


def _optional_uuid(value: Any, label: str) -> str:
    rendered = _strict_text(value, label)
    if not rendered:
        return ""
    if rendered != value:
        raise ProjectValidationError(f"{label} must not contain surrounding whitespace.")
    try:
        parsed = uuid.UUID(rendered)
    except (ValueError, AttributeError) as exc:
        raise ProjectValidationError(f"{label} must be a valid MusicBrainz UUID.") from exc
    canonical = str(parsed)
    if rendered.lower() != canonical:
        raise ProjectValidationError(f"{label} must be a canonical MusicBrainz UUID.")
    return canonical


def _optional_duration(item: dict[str, Any], index: int) -> float | None:
    candidates: list[tuple[str, float]] = []
    for key in ("duration_seconds", "expected_duration_seconds"):
        value = item.get(key)
        if value is None:
            continue
        seconds = strict_finite_number(value, f"Release track {index} {key}")
        if not 0.0 < seconds <= _MAX_DURATION_SECONDS:
            raise ProjectValidationError(
                f"Release track {index} {key} is outside the supported range."
            )
        candidates.append((key, seconds))

    milliseconds = item.get("duration_ms")
    if milliseconds is not None:
        if type(milliseconds) is not int:
            raise ProjectValidationError(
                f"Release track {index} duration_ms must be a JSON integer."
            )
        if not 0 < milliseconds <= int(_MAX_DURATION_SECONDS * 1000):
            raise ProjectValidationError(
                f"Release track {index} duration_ms is outside the supported range."
            )
        candidates.append(("duration_ms", milliseconds / 1000.0))

    if not candidates:
        return None
    reference = candidates[0][1]
    if any(abs(value - reference) > 0.001000001 for _key, value in candidates[1:]):
        labels = ", ".join(key for key, _value in candidates)
        raise ProjectValidationError(
            f"Release track {index} has conflicting duration fields ({labels})."
        )
    return reference


def _aliased_uuid(
    item: dict[str, Any],
    index: int,
    short_key: str,
    long_key: str,
    noun: str,
) -> str:
    present: list[tuple[str, str]] = []
    for key in (short_key, long_key):
        if key not in item:
            continue
        present.append(
            (
                key,
                _optional_uuid(
                    item[key], f"Release track {index} MusicBrainz {noun} ID"
                ),
            )
        )
    if not present:
        return ""
    non_empty = {value for _key, value in present if value}
    if len(non_empty) > 1:
        raise ProjectValidationError(
            f"Release track {index} has conflicting MusicBrainz {noun} IDs."
        )
    return next(iter(non_empty), "")


def _normalize_release_tracks(release_tracks: Any) -> list[dict[str, Any]]:
    if type(release_tracks) is not list:
        raise ProjectValidationError("Release track metadata must be a JSON-style array.")
    if not release_tracks:
        raise ProjectValidationError("Release track metadata cannot be empty.")
    if len(release_tracks) > _MAX_TRACKS:
        raise ProjectValidationError(
            f"Release track metadata cannot exceed {_MAX_TRACKS} tracks."
        )

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(release_tracks, start=1):
        if type(raw) is not dict:
            raise ProjectValidationError(f"Release track {index} must be an object.")
        unsupported = set(raw) - _PROVIDER_KEYS
        if unsupported:
            raise ProjectValidationError(
                f"Release track {index} contains unsupported field(s): "
                + ", ".join(sorted(str(key) for key in unsupported))
                + "."
            )

        position = raw.get("position", index)
        if type(position) is not int or position != index:
            raise ProjectValidationError(
                f"Release track {index} position must be the JSON integer {index}."
            )
        number = _strict_text(
            raw.get("number", str(index)),
            f"Release track {index} number",
            required=True,
            maximum=_MAX_NUMBER_TEXT,
        )
        title = _strict_text(
            raw.get("title"), f"Release track {index} title", required=True
        )
        artist = _strict_text(raw.get("artist", ""), f"Release track {index} artist")
        album = _strict_text(raw.get("album", ""), f"Release track {index} album")
        album_artist = _strict_text(
            raw.get("album_artist", ""), f"Release track {index} album_artist"
        )
        year = _strict_text(raw.get("year", ""), f"Release track {index} year")
        genre = _strict_text(raw.get("genre", ""), f"Release track {index} genre")
        side = _strict_text(
            raw.get("side", ""),
            f"Release track {index} side",
            maximum=_MAX_SIDE_TEXT,
        )

        side_position = raw.get("side_position")
        if side_position is not None and (
            type(side_position) is not int or not 1 <= side_position <= _MAX_TRACKS
        ):
            raise ProjectValidationError(
                f"Release track {index} side_position must be a positive JSON integer."
            )

        normalized.append(
            {
                "position": position,
                "number": number,
                "title": title,
                "artist": artist,
                "album": album,
                "album_artist": album_artist,
                "year": year,
                "genre": genre,
                "side": side,
                "side_position": side_position,
                "duration_seconds": _optional_duration(raw, index),
                "recording_id": _aliased_uuid(
                    raw,
                    index,
                    "recording_id",
                    "musicbrainz_recording_id",
                    "recording",
                ),
                "track_id": _aliased_uuid(
                    raw,
                    index,
                    "track_id",
                    "musicbrainz_track_id",
                    "release-track",
                ),
            }
        )

    _validate_side_groups(normalized)
    non_empty_track_ids = [item["track_id"] for item in normalized if item["track_id"]]
    if len(non_empty_track_ids) != len(set(non_empty_track_ids)):
        raise ProjectValidationError(
            "Release track metadata contains a duplicate MusicBrainz release-track ID."
        )
    return normalized


def _validate_side_groups(tracks: list[dict[str, Any]]) -> None:
    sides = [item["side"].casefold() for item in tracks]
    if any(sides) and not all(sides):
        raise ProjectValidationError(
            "Side labels must be supplied for every release track or for none of them."
        )
    if not any(sides):
        if any(item["side_position"] is not None for item in tracks):
            raise ProjectValidationError("side_position requires a non-empty side label.")
        return

    completed: set[str] = set()
    active = sides[0]
    side_index = 0
    for index, (side, item) in enumerate(zip(sides, tracks), start=1):
        if side != active:
            completed.add(active)
            if side in completed:
                raise ProjectValidationError(
                    "Release side labels form a noncontiguous duplicate side grouping."
                )
            active = side
            side_index = 0
        side_index += 1
        if item["side_position"] is not None and item["side_position"] != side_index:
            raise ProjectValidationError(
                f"Release track {index} side_position must be {side_index} within side "
                f"{item['side']}."
            )

    if any(item["side_position"] is not None for item in tracks) and any(
        item["side_position"] is None for item in tracks
    ):
        raise ProjectValidationError(
            "side_position must be supplied for every side-labelled release track or none."
        )


def _strict_minimum_seconds(project: Project, value: Any) -> float:
    if value is None:
        value = project.settings.min_track_seconds
    result = strict_finite_number(value, "Minimum track spacing")
    if not 0.0 < result <= _MAX_DURATION_SECONDS:
        raise ProjectValidationError("Minimum track spacing is outside the supported range.")
    return result


def _validate_project_without_mutation(project: Project) -> None:
    """Run the model validator on an isolated copy.

    Project migration support may initialize a missing analyzer baseline during
    validation.  A topology proposal is read-only, so even that beneficial
    initialization must not leak back into the caller's object.
    """

    try:
        isolated = copy.deepcopy(project)
    except Exception as exc:  # pragma: no cover - defensive dataclass boundary
        raise ProjectValidationError("The project could not be safely validated.") from exc
    isolated.validate()


def _project_state_payload(project: Project) -> dict[str, Any]:
    return {
        "source": asdict(project.source),
        "revision": getattr(project, "revision", 1),
        "tracks": [asdict(track) for track in project.tracks],
        "metadata": dict(project.metadata),
        "settings": asdict(project.settings),
        "analysis": {
            "music_start_seconds": project.analysis.music_start_seconds,
            "music_end_seconds": project.analysis.music_end_seconds,
            "candidates": [asdict(candidate) for candidate in project.analysis.candidates],
        },
    }


def _binding(project: Project, release_tracks: list[dict[str, Any]]) -> dict[str, Any]:
    source = asdict(project.source)
    current_tracks = [asdict(track) for track in project.tracks]
    candidates = [asdict(candidate) for candidate in project.analysis.candidates]
    return {
        "project_revision": getattr(project, "revision", 1),
        "source_sha256": project.source.sha256.lower(),
        "source_identity_sha256": _hash_json(source),
        "current_tracks_sha256": _hash_json(current_tracks),
        "project_metadata_sha256": _hash_json(dict(project.metadata)),
        "analysis_candidates_sha256": _hash_json(candidates),
        "project_state_sha256": _hash_json(_project_state_payload(project)),
        "release_tracks_sha256": _hash_json(release_tracks),
    }


def _normalized_candidates(
    project: Project, music_start: int, music_end: int
) -> list[tuple[int, BoundaryCandidate]]:
    sample_rate = project.source.sample_rate
    result: list[tuple[int, BoundaryCandidate]] = []
    for index, candidate in enumerate(project.analysis.candidates):
        cut_sample = candidate.cut_sample
        if not music_start < cut_sample < music_end:
            continue
        if candidate.duration_seconds > 0.0:
            start_sample = max(music_start, round(candidate.start_seconds * sample_rate))
            end_sample = min(music_end, round(candidate.end_seconds * sample_rate))
            if end_sample <= start_sample:
                start_sample = cut_sample
                end_sample = cut_sample
                duration_seconds = 0.0
            else:
                duration_seconds = (end_sample - start_sample) / sample_rate
        else:
            start_sample = cut_sample
            end_sample = cut_sample
            duration_seconds = 0.0
        normalized = replace(
            candidate,
            start_seconds=start_sample / sample_rate,
            end_seconds=end_sample / sample_rate,
            cut_seconds=cut_sample / sample_rate,
            duration_seconds=duration_seconds,
            selected=False,
        )
        result.append((index, normalized))
    return result


def _effective_durations(
    tracks: list[dict[str, Any]],
    *,
    span_seconds: float,
    minimum_track_seconds: float,
) -> tuple[list[float | None], str]:
    """Use partial duration evidence without pretending it was complete."""

    durations = [item["duration_seconds"] for item in tracks]
    known = [float(value) for value in durations if value is not None]
    if not known:
        return durations, "none"
    if len(known) == len(durations):
        return [float(value) for value in known], "complete"

    missing_count = len(durations) - len(known)
    remaining = span_seconds - sum(known)
    if remaining >= missing_count * minimum_track_seconds:
        imputed = remaining / missing_count
    else:
        ordered = sorted(known)
        midpoint = len(ordered) // 2
        imputed = (
            ordered[midpoint]
            if len(ordered) % 2
            else (ordered[midpoint - 1] + ordered[midpoint]) / 2.0
        )
    return [imputed if value is None else float(value) for value in durations], "partial-imputed"


def _duration_targets(
    durations: list[float | None], music_start: int, music_end: int
) -> list[int | None]:
    if not durations or any(value is None for value in durations):
        return [None] * max(0, len(durations) - 1)
    concrete = [float(value) for value in durations if value is not None]
    total = sum(concrete)
    span = music_end - music_start
    cumulative = 0.0
    result: list[int | None] = []
    for duration in concrete[:-1]:
        cumulative += duration
        result.append(music_start + round(span * cumulative / total))
    return result


def _feasible_duration_targets(
    targets: list[int | None],
    *,
    music_start: int,
    music_end: int,
    minimum_samples: int,
    track_count: int,
) -> list[int | None]:
    if any(target is None for target in targets):
        return list(targets)
    projected: list[int | None] = []
    previous = music_start
    for index, target in enumerate(targets):
        assert target is not None
        remaining_tracks = track_count - index - 1
        lower = previous + minimum_samples
        upper = music_end - remaining_tracks * minimum_samples
        chosen = min(upper, max(lower, target))
        projected.append(chosen)
        previous = chosen
    return projected


def _selector_durations(
    targets: list[int | None],
    *,
    music_start: int,
    music_end: int,
    sample_rate: int,
) -> list[float | None] | None:
    concrete_targets: list[int] = []
    for target in targets:
        if target is None:
            return None
        concrete_targets.append(target)
    samples = [music_start, *concrete_targets, music_end]
    return [
        (end - start) / sample_rate for start, end in zip(samples, samples[1:])
    ]


def _side_change(tracks: list[dict[str, Any]], boundary_index: int) -> bool:
    left_side = tracks[boundary_index]["side"]
    right_side = tracks[boundary_index + 1]["side"]
    if not isinstance(left_side, str) or not isinstance(right_side, str):
        raise ProjectValidationError("Release track side values must be text.")
    return left_side.casefold() != right_side.casefold()


def _candidate_match(
    selected: BoundaryCandidate,
    candidates: list[tuple[int, BoundaryCandidate]],
    chosen_sample: int,
    sample_rate: int,
) -> dict[str, Any] | None:
    if selected.duration_seconds <= 0.0:
        return None
    tolerance = max(1.0 / sample_rate, 0.002)
    exact = [
        item
        for item in candidates
        if abs(item[1].start_seconds - selected.start_seconds) <= tolerance
        and abs(item[1].end_seconds - selected.end_seconds) <= tolerance
    ]
    matches = exact
    if not matches:
        matches = [
            item
            for item in candidates
            if item[1].duration_seconds > 0.0
            and item[1].start_seconds >= selected.start_seconds - tolerance
            and item[1].end_seconds <= selected.end_seconds + tolerance
        ]
    if not matches:
        return None
    strongest_index, strongest = max(
        matches,
        key=lambda item: (item[1].score, item[1].duration_seconds, -item[0]),
    )
    start_sample = min(round(item.start_seconds * sample_rate) for _idx, item in matches)
    end_sample = max(round(item.end_seconds * sample_rate) for _idx, item in matches)
    return {
        "candidate_index": strongest_index,
        "candidate_indexes": [index for index, _item in matches],
        "candidate_cut_sample": strongest.cut_sample,
        "measured_start_sample": start_sample,
        "measured_end_sample": end_sample,
        "gap_duration_seconds": round((end_sample - start_sample) / sample_rate, 9),
        "score": round(float(max(item.score for _idx, item in matches)), 6),
        "distance_samples": abs(chosen_sample - strongest.cut_sample),
        "aligned_within_gap": chosen_sample != strongest.cut_sample,
    }


def _proposal_digest(proposal: dict[str, Any]) -> str:
    payload = dict(proposal)
    payload.pop("proposal_id", None)
    payload.pop("proposal_sha256", None)
    return _hash_json(payload)


def propose_topology_refit(
    project: Project,
    release_tracks: list[dict[str, Any]],
    *,
    min_track_seconds: float | None = None,
) -> dict[str, Any]:
    """Return a deterministic, non-mutating track-topology proposal.

    ``release_tracks`` accepts Groove Serpent's canonical metadata fields and
    the compact MusicBrainz provider fields (``duration_ms``, ``recording_id``,
    and ``track_id``).  Numeric strings and booleans are intentionally refused.
    """

    if not isinstance(project, Project):
        raise ProjectValidationError("A validated Groove Serpent Project is required.")
    _validate_project_without_mutation(project)
    normalized_tracks = _normalize_release_tracks(release_tracks)
    minimum_seconds = _strict_minimum_seconds(project, min_track_seconds)

    sample_rate = project.source.sample_rate
    music_start = project.tracks[0].start_sample
    music_end = project.tracks[-1].end_sample
    minimum_samples = math.ceil(minimum_seconds * sample_rate)
    track_count = len(normalized_tracks)
    if music_end - music_start < track_count * minimum_samples:
        raise ProjectValidationError(
            "The requested release topology is impossible: the preserved music range "
            f"cannot fit {track_count} tracks with at least {minimum_samples} samples each."
        )

    candidate_pairs = _normalized_candidates(project, music_start, music_end)
    candidates = [candidate for _index, candidate in candidate_pairs]
    durations, duration_evidence = _effective_durations(
        normalized_tracks,
        span_seconds=(music_end - music_start) / sample_rate,
        minimum_track_seconds=minimum_seconds,
    )
    duration_targets = _duration_targets(durations, music_start, music_end)
    fitting_targets = _feasible_duration_targets(
        duration_targets,
        music_start=music_start,
        music_end=music_end,
        minimum_samples=minimum_samples,
        track_count=track_count,
    )
    fitting_durations = _selector_durations(
        fitting_targets,
        music_start=music_start,
        music_end=music_end,
        sample_rate=sample_rate,
    )
    selector_durations = fitting_durations if fitting_durations is not None else durations
    sides = [item["side"] for item in normalized_tracks]
    settings = replace(project.settings, min_track_seconds=minimum_seconds)
    selected = select_boundaries(
        candidates,
        music_start=music_start / sample_rate,
        music_end=music_end / sample_rate,
        sample_rate=sample_rate,
        settings=settings,
        expected_track_count=track_count,
        expected_durations=selector_durations,
        expected_sides=sides if any(sides) else None,
    )
    side_partition_fallback = False
    if len(selected) != track_count - 1 and any(sides):
        side_partition_fallback = True
        selected = select_boundaries(
            candidates,
            music_start=music_start / sample_rate,
            music_end=music_end / sample_rate,
            sample_rate=sample_rate,
            settings=settings,
            expected_track_count=track_count,
            expected_durations=selector_durations,
            expected_sides=None,
        )
    if len(selected) != track_count - 1:
        raise ProjectValidationError(
            "The requested release topology cannot satisfy the exact minimum track spacing."
        )
    selected.sort(key=lambda item: (item.cut_sample, item.cut_seconds))

    # Convert the selector's floating feasibility checks back onto one exact
    # sample grid.  The projection only moves a point when rounding would
    # otherwise violate the declared minimum by a sample or two.
    projected: list[tuple[BoundaryCandidate, int, bool]] = []
    previous_sample = music_start
    for index, boundary in enumerate(selected):
        remaining_tracks = track_count - index - 1
        lower = previous_sample + minimum_samples
        upper = music_end - remaining_tracks * minimum_samples
        if lower > upper:
            raise ProjectValidationError(
                "The requested release topology cannot satisfy the exact minimum track spacing."
            )
        chosen = min(upper, max(lower, boundary.cut_sample))
        projected.append((boundary, chosen, chosen != boundary.cut_sample))
        previous_sample = chosen

    targets = duration_targets
    boundaries: list[dict[str, Any]] = []
    proposal_warnings: list[str] = []
    if track_count != len(project.tracks):
        verb = "increases" if track_count > len(project.tracks) else "decreases"
        proposal_warnings.append(
            f"The proposal {verb} the track count from {len(project.tracks)} to {track_count}; "
            "retain the prior project state for reversal."
        )
    if side_partition_fallback:
        proposal_warnings.append(
            "Side-partition fitting was infeasible, so the proposal used whole-range fitting."
        )
    if duration_evidence == "partial-imputed":
        proposal_warnings.append(
            "Some release durations were missing; duration fitting imputed the unknown values."
        )
    if fitting_targets != duration_targets:
        proposal_warnings.append(
            "One or more release-duration targets were projected onto the exact "
            "minimum-spacing grid."
        )

    for index, (selected_boundary, chosen_sample, was_projected) in enumerate(projected):
        target_sample = targets[index]
        fitting_target_sample = fitting_targets[index]
        residual = (
            None
            if target_sample is None
            else (chosen_sample - target_sample) / sample_rate
        )
        match = _candidate_match(
            selected_boundary,
            candidate_pairs,
            chosen_sample,
            sample_rate,
        )
        is_side_change = _side_change(normalized_tracks, index)
        warnings: list[str] = []
        if match is None:
            warnings.append("No measured analyzer gap supports this boundary.")
        elif match["score"] < 0.55:
            warnings.append("The supporting analyzer gap has low confidence.")
        if is_side_change and match is None:
            warnings.append("The side change has no measured side-gap anchor.")
        if was_projected:
            warnings.append("The cut moved to enforce exact minimum sample spacing.")
        if fitting_target_sample != target_sample:
            warnings.append(
                "The release-duration target was too close to another edge and was projected."
            )
        if residual is not None:
            average_duration = (music_end - music_start) / sample_rate / track_count
            if abs(residual) > max(2.0, average_duration * 0.08):
                warnings.append(
                    "The measured cut differs materially from the cumulative release duration."
                )
        if duration_evidence == "partial-imputed":
            warnings.append("The duration target includes imputed provider durations.")

        if match is None:
            confidence = 0.18 if target_sample is not None else 0.08
        else:
            confidence = 0.52 + 0.43 * float(match["score"])
            if is_side_change:
                confidence += 0.03
            if residual is not None:
                confidence -= min(0.18, abs(residual) / 120.0)
        confidence = round(max(0.0, min(1.0, confidence)), 6)
        boundaries.append(
            {
                "boundary_number": index + 1,
                "left_track_number": index + 1,
                "right_track_number": index + 2,
                "chosen_sample": chosen_sample,
                "chosen_seconds": chosen_sample / sample_rate,
                "target_sample": target_sample,
                "fitting_target_sample": fitting_target_sample,
                "candidate_match": match,
                "confidence": confidence,
                "duration_residual_seconds": (
                    None if residual is None else round(residual, 9)
                ),
                "duration_evidence": duration_evidence,
                "side_change": is_side_change,
                "warnings": warnings,
                "uncertain": bool(warnings) or confidence < 0.6,
            }
        )

    samples = [music_start, *[item["chosen_sample"] for item in boundaries], music_end]
    project_metadata = project.metadata
    proposed_tracks: list[dict[str, Any]] = []
    for index, item in enumerate(normalized_tracks, start=1):
        left_confidence = 1.0 if index == 1 else boundaries[index - 2]["confidence"]
        right_confidence = (
            1.0 if index == track_count else boundaries[index - 1]["confidence"]
        )
        start_sample = samples[index - 1]
        end_sample = samples[index]
        proposed_tracks.append(
            {
                "number": index,
                "title": item["title"],
                "start_sample": start_sample,
                "end_sample": end_sample,
                "start_seconds": start_sample / sample_rate,
                "end_seconds": end_sample / sample_rate,
                "confidence": round(min(left_confidence, right_confidence), 6),
                "artist": item["artist"] or project_metadata.get("artist", ""),
                "album": item["album"] or project_metadata.get("album", ""),
                "album_artist": item["album_artist"]
                or project_metadata.get("album_artist", project_metadata.get("artist", "")),
                "year": item["year"] or project_metadata.get("year", ""),
                "genre": item["genre"] or project_metadata.get("genre", ""),
                "side": item["side"],
                "expected_duration_seconds": item["duration_seconds"],
                "musicbrainz_recording_id": item["recording_id"],
                "musicbrainz_track_id": item["track_id"],
            }
        )

    old_count = len(project.tracks)
    operation = "refit" if track_count == old_count else (
        "split" if track_count > old_count else "merge"
    )
    proposal: dict[str, Any] = {
        "schema": TOPOLOGY_PROPOSAL_SCHEMA,
        "proposal_id": "",
        "proposal_sha256": "",
        "binding": _binding(project, normalized_tracks),
        "operation": operation,
        "music_range": {
            "start_sample": music_start,
            "end_sample": music_end,
            "sample_rate": sample_rate,
        },
        "minimum_track_seconds": minimum_seconds,
        "minimum_track_samples": minimum_samples,
        "release_tracks": normalized_tracks,
        "boundaries": boundaries,
        "tracks": proposed_tracks,
        "warnings": proposal_warnings,
        "uncertain": any(item["uncertain"] for item in boundaries),
    }
    digest = _proposal_digest(proposal)
    proposal["proposal_sha256"] = digest
    proposal["proposal_id"] = f"topology-{digest[:24]}"
    return proposal


def tracks_from_topology_proposal(
    project: Project, proposal: dict[str, Any]
) -> list[Track]:
    """Validate a proposal against ``project`` and return new ``Track`` objects.

    Validation deterministically regenerates the proposal.  Consequently an
    edited cut is refused even if somebody also recomputes the unkeyed JSON
    hash.  The supplied project and its current tracks are never modified.
    """

    if not isinstance(project, Project):
        raise ProjectValidationError("A validated Groove Serpent Project is required.")
    _validate_project_without_mutation(project)
    if type(proposal) is not dict:
        raise ProjectValidationError("A topology proposal must be an object.")
    missing = _PROPOSAL_KEYS - set(proposal)
    extra = set(proposal) - _PROPOSAL_KEYS
    if missing:
        raise ProjectValidationError(
            "The topology proposal is missing field(s): " + ", ".join(sorted(missing)) + "."
        )
    if extra:
        raise ProjectValidationError(
            "The topology proposal contains unsupported field(s): "
            + ", ".join(sorted(str(key) for key in extra))
            + "."
        )
    if proposal.get("schema") != TOPOLOGY_PROPOSAL_SCHEMA:
        raise ProjectValidationError(
            f"Expected topology proposal schema {TOPOLOGY_PROPOSAL_SCHEMA}."
        )

    proposal_sha256 = proposal.get("proposal_sha256")
    if type(proposal_sha256) is not str or not _SHA256_RE.fullmatch(proposal_sha256):
        raise ProjectValidationError("The topology proposal SHA-256 value is invalid.")
    digest = _proposal_digest(proposal)
    if digest != proposal_sha256:
        raise ProjectValidationError("The topology proposal was edited or is corrupt.")
    expected_id = f"topology-{digest[:24]}"
    if proposal.get("proposal_id") != expected_id:
        raise ProjectValidationError("The topology proposal ID does not match its content.")

    normalized_tracks = _normalize_release_tracks(proposal.get("release_tracks"))
    expected_binding = _binding(project, normalized_tracks)
    if _canonical_json(proposal.get("binding")) != _canonical_json(expected_binding):
        raise ProjectValidationError(
            "The topology proposal is stale for the current source, tracks, metadata, or revision."
        )

    minimum_seconds = proposal.get("minimum_track_seconds")
    minimum_seconds = _strict_minimum_seconds(project, minimum_seconds)
    expected = propose_topology_refit(
        project,
        normalized_tracks,
        min_track_seconds=minimum_seconds,
    )
    if _canonical_json(expected) != _canonical_json(proposal):
        raise ProjectValidationError(
            "The topology proposal was edited and cannot be reproduced from its evidence."
        )

    result = [Track.from_dict(dict(item)) for item in expected["tracks"]]
    # Validate the replacement on an independent structural shell without ever
    # assigning it to the caller's project.
    for index, track in enumerate(result, start=1):
        if track.number != index:
            raise ProjectValidationError("Proposed track numbers are not consecutive.")
        if index > 1 and track.start_sample != result[index - 2].end_sample:
            raise ProjectValidationError("Proposed tracks are not contiguous.")
    if result[0].start_sample != project.tracks[0].start_sample or (
        result[-1].end_sample != project.tracks[-1].end_sample
    ):
        raise ProjectValidationError("The proposal does not preserve the project music range.")
    minimum_samples = proposal["minimum_track_samples"]
    if type(minimum_samples) is not int or minimum_samples <= 0:
        raise ProjectValidationError("The proposal minimum sample spacing is invalid.")
    if any(track.end_sample - track.start_sample < minimum_samples for track in result):
        raise ProjectValidationError("A proposed track violates the minimum sample spacing.")
    return result


__all__ = [
    "TOPOLOGY_PROPOSAL_SCHEMA",
    "propose_topology_refit",
    "tracks_from_topology_proposal",
]
