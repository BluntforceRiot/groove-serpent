from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Iterable

import numpy as np

from .audio_snapshot import VerifiedAudioSnapshot, verified_audio_snapshot
from .capture_envelope import require_supported_capture
from .errors import ProjectValidationError
from .media import decode_rms_envelope, probe_audio
from .models import (
    AnalyzerBaseline,
    AnalysisSettings,
    AnalysisSummary,
    BoundaryCandidate,
    MAX_SOURCE_SAMPLE_COUNT,
    MAX_SUPPORTED_DURATION_SECONDS,
    MAX_TRACKS,
    Project,
    Track,
)
from .tracklist import TrackSeed
from .validation import strict_finite_number


def _validate_expected_track_count(expected_track_count: int | None) -> None:
    if expected_track_count is not None and (
        type(expected_track_count) is not int or not 1 <= expected_track_count <= MAX_TRACKS
    ):
        raise ValueError(f"expected_track_count must be at least 1 and no more than {MAX_TRACKS}")


def _sample_from_seconds(seconds: float, sample_rate: int, label: str) -> int:
    """Derive one bounded sample coordinate without leaking float overflow."""

    numeric = strict_finite_number(seconds, label)
    if numeric < 0 or numeric > MAX_SOURCE_SAMPLE_COUNT / sample_rate:
        raise ProjectValidationError(f"{label} is outside the supported range.")
    try:
        scaled = numeric * sample_rate
        if not math.isfinite(scaled):
            raise OverflowError("non-finite derived sample coordinate")
        result = int(round(scaled))
    except (OverflowError, TypeError, ValueError) as exc:
        raise ProjectValidationError(f"{label} is outside the supported range.") from exc
    if not 0 <= result <= MAX_SOURCE_SAMPLE_COUNT:
        raise ProjectValidationError(f"{label} is outside the supported range.")
    return result


def _validate_track_seed_durations(track_seeds: list[TrackSeed] | None) -> None:
    if track_seeds is None:
        return
    for index, seed in enumerate(track_seeds, start=1):
        if seed.duration_seconds is None:
            continue
        duration = strict_finite_number(seed.duration_seconds, f"Track seed {index} duration")
        if not 0 < duration <= MAX_SUPPORTED_DURATION_SECONDS:
            raise ProjectValidationError(
                f"Track seed {index} duration is outside the supported range."
            )


def smooth_envelope(values: Iterable[float], windows: int) -> np.ndarray:
    array = np.asarray(list(values), dtype=np.float64)
    if array.size == 0:
        raise ValueError("The envelope is empty.")
    if windows <= 1 or array.size < windows:
        return array
    windows = windows if windows % 2 else windows + 1
    kernel = np.ones(windows, dtype=np.float64) / windows
    padded = np.pad(array, (windows // 2, windows // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def estimate_thresholds(envelope_db: np.ndarray, margin_db: float) -> tuple[float, float, float]:
    finite = envelope_db[np.isfinite(envelope_db)]
    if finite.size == 0:
        return -60.0, -54.0, -42.0

    # Ignore exact or near-exact digital silence when estimating the vinyl floor.
    audible = finite[finite > -90.0]
    if audible.size < max(20, finite.size // 200):
        audible = finite

    noise_floor = float(np.percentile(audible, 3.0))
    median_level = float(np.percentile(audible, 50.0))
    upper_level = float(np.percentile(audible, 75.0))

    silence_threshold = min(noise_floor + margin_db, median_level - 9.0)
    silence_threshold = float(np.clip(silence_threshold, -68.0, -24.0))

    active_threshold = min(silence_threshold + 12.0, upper_level - 4.0)
    active_threshold = max(active_threshold, silence_threshold + 4.0)
    active_threshold = float(np.clip(active_threshold, -58.0, -12.0))
    return noise_floor, silence_threshold, active_threshold


def _first_sustained(mask: np.ndarray, count: int) -> int | None:
    if mask.size == 0:
        return None
    count = max(1, min(count, mask.size))
    # A cumulative sum is both linear-time and safe for runs longer than the
    # 32,767 maximum represented by the int16 convolution used previously.
    cumulative = np.empty(mask.size + 1, dtype=np.int64)
    cumulative[0] = 0
    np.cumsum(mask.astype(np.bool_, copy=False), dtype=np.int64, out=cumulative[1:])
    hits = cumulative[count:] - cumulative[:-count]
    indices = np.flatnonzero(hits >= count)
    return int(indices[0]) if indices.size else None


def find_music_bounds(
    envelope_db: np.ndarray,
    *,
    window_seconds: float,
    active_threshold_db: float,
    active_run_seconds: float,
    lead_in_seconds: float,
    tail_seconds: float,
    duration_seconds: float,
    boundary_threshold_db: float | None = None,
) -> tuple[float, float] | None:
    active = envelope_db > active_threshold_db
    run_count = max(1, math.ceil(active_run_seconds / window_seconds))
    core_first = _first_sustained(active, run_count)
    core_reverse_first = _first_sustained(active[::-1], run_count)

    if core_first is None or core_reverse_first is None:
        return None

    # A louder threshold establishes that the recording contains real music.
    # Once that core exists, a lower hysteresis threshold preserves quiet
    # introductions, fades, and later swells separated by a low-level passage.
    # Requiring the same sustained run keeps isolated vinyl clicks from
    # extending the bounds on their own.
    first = core_first
    reverse_first = core_reverse_first
    if boundary_threshold_db is not None and math.isfinite(boundary_threshold_db):
        boundary_threshold = min(active_threshold_db, boundary_threshold_db)
        boundary_active = envelope_db > boundary_threshold
        boundary_first = _first_sustained(boundary_active, run_count)
        boundary_reverse_first = _first_sustained(boundary_active[::-1], run_count)
        if boundary_first is not None and boundary_reverse_first is not None:
            first = boundary_first
            reverse_first = boundary_reverse_first

    last_start = envelope_db.size - reverse_first - run_count
    start = max(0.0, first * window_seconds - lead_in_seconds)
    end = min(duration_seconds, (last_start + run_count) * window_seconds + tail_seconds)
    if end <= start:
        return None
    return start, end


def _bridge_short_interruptions(mask: np.ndarray, maximum_false_windows: int) -> np.ndarray:
    if maximum_false_windows <= 0 or mask.size < 3:
        return mask.copy()
    result = mask.copy()
    index = 0
    while index < result.size:
        if result[index]:
            index += 1
            continue
        start = index
        while index < result.size and not result[index]:
            index += 1
        end = index
        if (
            start > 0
            and end < result.size
            and result[start - 1]
            and result[end]
            and end - start <= maximum_false_windows
        ):
            result[start:end] = True
    return result


def detect_candidates(
    envelope_db: np.ndarray,
    *,
    source_sample_rate: int,
    window_seconds: float,
    music_start_seconds: float,
    music_end_seconds: float,
    silence_threshold_db: float,
    settings: AnalysisSettings,
) -> list[BoundaryCandidate]:
    start_index = max(0, int(math.floor(music_start_seconds / window_seconds)))
    end_index = min(envelope_db.size, int(math.ceil(music_end_seconds / window_seconds)))
    mask = envelope_db < silence_threshold_db
    mask[:start_index] = False
    mask[end_index:] = False
    mask = _bridge_short_interruptions(mask, max(1, round(0.25 / window_seconds)))

    candidates: list[BoundaryCandidate] = []
    index = start_index
    minimum_windows = max(1, math.ceil(settings.min_gap_seconds / window_seconds))
    context_windows = max(1, round(1.0 / window_seconds))

    while index < end_index:
        if not mask[index]:
            index += 1
            continue
        run_start = index
        while index < end_index and mask[index]:
            index += 1
        run_end = index
        if run_end - run_start < minimum_windows:
            continue

        gap = envelope_db[run_start:run_end]
        gap_duration = (run_end - run_start) * window_seconds
        gap_minimum = float(np.min(gap))
        gap_mean = float(np.mean(gap))
        near_minimum = np.flatnonzero(gap <= gap_minimum + 3.0)
        gap_midpoint = (gap.size - 1) / 2.0
        local_index = int(
            near_minimum[np.argmin(np.abs(near_minimum - gap_midpoint))]
            if near_minimum.size
            else round(gap_midpoint)
        )
        cut_index = run_start + local_index
        cut_seconds = min(music_end_seconds, (cut_index + 0.5) * window_seconds)
        if (
            cut_seconds - music_start_seconds < settings.min_track_seconds
            or music_end_seconds - cut_seconds < settings.min_track_seconds
        ):
            continue

        before = envelope_db[max(start_index, run_start - context_windows) : run_start]
        after = envelope_db[run_end : min(end_index, run_end + context_windows)]
        context_parts = [part for part in (before, after) if part.size]
        context_level = (
            float(np.mean(np.concatenate(context_parts))) if context_parts else silence_threshold_db
        )
        contrast = max(0.0, context_level - gap_mean)

        useful_duration = min(gap_duration, settings.max_gap_seconds)
        duration_component = float(
            np.clip(
                (useful_duration - settings.min_gap_seconds)
                / max(0.5, 3.0 - settings.min_gap_seconds),
                0.0,
                1.0,
            )
        )
        minimum_depth = float(np.clip((silence_threshold_db - gap_minimum) / 20.0, 0.0, 1.0))
        mean_depth = float(np.clip((silence_threshold_db - gap_mean) / 12.0, 0.0, 1.0))
        contrast_component = float(np.clip(contrast / 20.0, 0.0, 1.0))
        score = float(
            np.clip(
                0.15
                + 0.25 * duration_component
                + 0.20 * minimum_depth
                + 0.20 * mean_depth
                + 0.20 * contrast_component,
                0.0,
                1.0,
            )
        )

        candidates.append(
            BoundaryCandidate(
                start_seconds=run_start * window_seconds,
                end_seconds=min(music_end_seconds, run_end * window_seconds),
                cut_seconds=cut_seconds,
                cut_sample=_sample_from_seconds(
                    cut_seconds,
                    source_sample_rate,
                    "Boundary candidate cut time",
                ),
                duration_seconds=gap_duration,
                minimum_db=gap_minimum,
                mean_db=gap_mean,
                contrast_db=contrast,
                score=score,
            )
        )
    return candidates


def _target_boundaries(
    music_start: float,
    music_end: float,
    expected_track_count: int,
    expected_durations: list[float | None] | None,
) -> list[float]:
    body = music_end - music_start
    if expected_track_count <= 1:
        return []
    if expected_durations and len(expected_durations) == expected_track_count:
        concrete = [duration for duration in expected_durations if duration and duration > 0]
        try:
            total = math.fsum(concrete)
        except OverflowError:
            total = math.inf
        if len(concrete) == expected_track_count and math.isfinite(total) and total > 0:
            cumulative = 0.0
            result: list[float] = []
            for duration in concrete[:-1]:
                cumulative += duration
                result.append(music_start + body * (cumulative / total))
            return result
    return [
        music_start + body * (index / expected_track_count)
        for index in range(1, expected_track_count)
    ]


def _virtual_candidate(time_seconds: float, sample_rate: int) -> BoundaryCandidate:
    return BoundaryCandidate(
        start_seconds=time_seconds,
        end_seconds=time_seconds,
        cut_seconds=time_seconds,
        cut_sample=_sample_from_seconds(time_seconds, sample_rate, "Virtual boundary cut time"),
        duration_seconds=0.0,
        minimum_db=0.0,
        mean_db=0.0,
        contrast_db=0.0,
        score=0.08,
        selected=True,
    )


def _candidate_distance(point: BoundaryCandidate, target: float) -> float:
    """Measure alignment against a whole quiet region, not only its midpoint."""

    if point.duration_seconds <= 0.0 or point.end_seconds <= point.start_seconds:
        return abs(point.cut_seconds - target)
    if target < point.start_seconds:
        return point.start_seconds - target
    if target > point.end_seconds:
        return target - point.end_seconds
    return 0.0


def _align_candidate(
    point: BoundaryCandidate, target: float, sample_rate: int
) -> BoundaryCandidate:
    """Place a measured cut at the duration target when it lies in the quiet region.

    A gap midpoint can fall inside a quiet song introduction.  Reference durations
    provide a better estimate of the actual join, while clamping to the measured
    low-energy region keeps the marker grounded in the source audio.
    """

    if point.duration_seconds <= 0.0 or point.end_seconds <= point.start_seconds:
        return replace(point, selected=True)
    edge_guard = min(0.5, max(0.0, (point.end_seconds - point.start_seconds) / 4.0))
    if not (point.start_seconds + edge_guard <= target <= point.end_seconds - edge_guard):
        return replace(point, selected=True)
    cut_seconds = target
    return replace(
        point,
        cut_seconds=cut_seconds,
        cut_sample=_sample_from_seconds(cut_seconds, sample_rate, "Aligned boundary cut time"),
        selected=True,
    )


def _side_groups(
    expected_track_count: int, expected_sides: list[str] | None
) -> list[tuple[int, int]]:
    if expected_sides is None or len(expected_sides) != expected_track_count:
        return []
    normalized = [str(side).strip().upper() for side in expected_sides]
    if any(not side for side in normalized):
        return []

    groups: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(normalized) + 1):
        if index == len(normalized) or normalized[index] != normalized[start]:
            groups.append((start, index))
            start = index
    # Repeated, non-contiguous side labels are ambiguous and should fall back to
    # the ordinary whole-recording duration model.
    labels = [normalized[start] for start, _end in groups]
    if len(groups) < 2 or len(labels) != len(set(labels)):
        return []
    return groups


def _coalesce_side_change_candidates(
    candidates: list[BoundaryCandidate],
    *,
    sample_rate: int,
    maximum_interruption_seconds: float = 0.75,
) -> list[BoundaryCandidate]:
    """Join low-energy side-change regions separated by a needle-drop transient."""

    ordered = sorted(candidates, key=lambda item: item.start_seconds)
    if not ordered:
        return []
    groups: list[list[BoundaryCandidate]] = [[ordered[0]]]
    for candidate in ordered[1:]:
        previous = groups[-1][-1]
        if candidate.start_seconds - previous.end_seconds <= maximum_interruption_seconds:
            groups[-1].append(candidate)
        else:
            groups.append([candidate])

    result: list[BoundaryCandidate] = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
            continue
        start = group[0].start_seconds
        end = group[-1].end_seconds
        strongest = max(group, key=lambda item: item.score)
        total_measured = sum(item.duration_seconds for item in group)
        mean_db = (
            sum(item.mean_db * item.duration_seconds for item in group) / total_measured
            if total_measured > 0
            else strongest.mean_db
        )
        result.append(
            BoundaryCandidate(
                start_seconds=start,
                end_seconds=end,
                cut_seconds=strongest.cut_seconds,
                cut_sample=_sample_from_seconds(
                    strongest.cut_seconds,
                    sample_rate,
                    "Coalesced boundary cut time",
                ),
                duration_seconds=end - start,
                minimum_db=min(item.minimum_db for item in group),
                mean_db=mean_db,
                contrast_db=max(item.contrast_db for item in group),
                score=max(item.score for item in group),
            )
        )
    return result


def _select_exact_count(
    candidates: list[BoundaryCandidate],
    *,
    music_start: float,
    music_end: float,
    sample_rate: int,
    expected_track_count: int,
    expected_durations: list[float | None] | None,
    expected_sides: list[str] | None,
    min_track_seconds: float,
    _allow_side_partition: bool = True,
) -> list[BoundaryCandidate]:
    needed = expected_track_count - 1
    if needed <= 0:
        return []

    groups = _side_groups(expected_track_count, expected_sides) if _allow_side_partition else []
    if groups:
        side_durations: list[float | None] | None = None
        if expected_durations and len(expected_durations) == expected_track_count:
            summed: list[float | None] = []
            for start, end in groups:
                values = expected_durations[start:end]
                if any(value is None or value <= 0 for value in values):
                    break
                summed.append(sum(float(value) for value in values if value is not None))
            if len(summed) == len(groups):
                side_durations = summed

        anchor_candidates = _coalesce_side_change_candidates(candidates, sample_rate=sample_rate)
        anchors = _select_exact_count(
            anchor_candidates,
            music_start=music_start,
            music_end=music_end,
            sample_rate=sample_rate,
            expected_track_count=len(groups),
            expected_durations=side_durations,
            expected_sides=None,
            min_track_seconds=min_track_seconds,
            _allow_side_partition=False,
        )
        if len(anchors) == len(groups) - 1:
            partitioned: list[BoundaryCandidate] = list(anchors)
            partition_valid = True
            for group_index, (track_start, track_end) in enumerate(groups):
                segment_start = (
                    music_start if group_index == 0 else anchors[group_index - 1].end_seconds
                )
                segment_end = (
                    music_end
                    if group_index == len(groups) - 1
                    else anchors[group_index].start_seconds
                )
                group_count = track_end - track_start
                if segment_end <= segment_start or (
                    segment_end - segment_start < group_count * min_track_seconds
                ):
                    partition_valid = False
                    break
                if group_count <= 1:
                    continue
                group_candidates = [
                    candidate
                    for candidate in candidates
                    if segment_start < candidate.cut_seconds < segment_end
                ]
                group_durations = (
                    expected_durations[track_start:track_end]
                    if expected_durations and len(expected_durations) == expected_track_count
                    else None
                )
                selected = _select_exact_count(
                    group_candidates,
                    music_start=segment_start,
                    music_end=segment_end,
                    sample_rate=sample_rate,
                    expected_track_count=group_count,
                    expected_durations=group_durations,
                    expected_sides=None,
                    min_track_seconds=min_track_seconds,
                    _allow_side_partition=False,
                )
                if len(selected) != group_count - 1:
                    partition_valid = False
                    break
                partitioned.extend(selected)
            if partition_valid:
                return sorted(partitioned, key=lambda item: item.cut_seconds)

    targets = _target_boundaries(music_start, music_end, expected_track_count, expected_durations)
    real = [
        replace(candidate, selected=False)
        for candidate in candidates
        if candidate.cut_seconds >= music_start + min_track_seconds
        and candidate.cut_seconds <= music_end - min_track_seconds
    ]
    virtual = [_virtual_candidate(target, sample_rate) for target in targets]
    points = sorted(real + virtual, key=lambda item: (item.cut_seconds, -item.score))

    # Collapse nearly identical choices, preferring a real, higher-confidence gap.
    collapsed: list[BoundaryCandidate] = []
    for point in points:
        if collapsed and abs(point.cut_seconds - collapsed[-1].cut_seconds) < 0.05:
            if point.score > collapsed[-1].score:
                collapsed[-1] = point
        else:
            collapsed.append(point)
    points = collapsed

    count = len(points)
    negative_infinity = float("-inf")
    dp = np.full((needed, count), negative_infinity, dtype=np.float64)
    previous = np.full((needed, count), -1, dtype=np.int32)
    average_track = max(1.0, (music_end - music_start) / expected_track_count)
    tolerance = max(8.0, min(45.0, average_track * 0.15))

    def point_value(boundary_index: int, point: BoundaryCandidate) -> float:
        target = targets[boundary_index]
        deviation = _candidate_distance(point, target) / tolerance
        return point.score - 0.75 * deviation

    for index, point in enumerate(points):
        if point.cut_seconds - music_start >= min_track_seconds:
            dp[0, index] = point_value(0, point)

    for boundary_index in range(1, needed):
        for index, point in enumerate(points):
            best_value = negative_infinity
            best_previous = -1
            for previous_index in range(index):
                if point.cut_seconds - points[previous_index].cut_seconds < min_track_seconds:
                    continue
                value = dp[boundary_index - 1, previous_index]
                if value > best_value:
                    best_value = value
                    best_previous = previous_index
            if best_previous >= 0:
                dp[boundary_index, index] = best_value + point_value(boundary_index, point)
                previous[boundary_index, index] = best_previous

    valid_final = [
        index
        for index, point in enumerate(points)
        if music_end - point.cut_seconds >= min_track_seconds
        and math.isfinite(float(dp[needed - 1, index]))
    ]
    if not valid_final:
        # The requested count can be physically impossible when the available
        # duration cannot accommodate the minimum track length. In that case,
        # keep the largest feasible boundary set and prefer measured gaps over
        # invented targets before comparing their confidence/alignment value.
        fallback_values = np.full((needed, count), negative_infinity, dtype=np.float64)
        fallback_real_counts = np.full((needed, count), -1, dtype=np.int32)
        fallback_previous = np.full((needed, count), -1, dtype=np.int32)

        def fallback_point_value(point: BoundaryCandidate) -> float:
            deviation = min(abs(point.cut_seconds - target) for target in targets) / tolerance
            return point.score - 0.75 * deviation

        for index, point in enumerate(points):
            if point.cut_seconds - music_start >= min_track_seconds:
                fallback_values[0, index] = fallback_point_value(point)
                fallback_real_counts[0, index] = int(point.duration_seconds > 0.0)

        for boundary_index in range(1, needed):
            for index, point in enumerate(points):
                best_real_count = -1
                best_value = negative_infinity
                best_previous = -1
                for previous_index in range(index):
                    if point.cut_seconds - points[previous_index].cut_seconds < min_track_seconds:
                        continue
                    previous_real_count = int(
                        fallback_real_counts[boundary_index - 1, previous_index]
                    )
                    if previous_real_count < 0:
                        continue
                    real_count = previous_real_count + int(point.duration_seconds > 0.0)
                    value = float(fallback_values[boundary_index - 1, previous_index])
                    if real_count > best_real_count or (
                        real_count == best_real_count and value > best_value
                    ):
                        best_real_count = real_count
                        best_value = value
                        best_previous = previous_index
                if best_previous >= 0:
                    fallback_values[boundary_index, index] = best_value + fallback_point_value(
                        point
                    )
                    fallback_real_counts[boundary_index, index] = best_real_count
                    fallback_previous[boundary_index, index] = best_previous

        fallback_boundary_index = -1
        fallback_final_indices: list[int] = []
        for boundary_index in range(needed - 1, -1, -1):
            fallback_final_indices = [
                index
                for index, point in enumerate(points)
                if music_end - point.cut_seconds >= min_track_seconds
                and fallback_real_counts[boundary_index, index] >= 0
            ]
            if fallback_final_indices:
                fallback_boundary_index = boundary_index
                break
        if fallback_boundary_index < 0:
            return []

        final_index = max(
            fallback_final_indices,
            key=lambda index: (
                int(fallback_real_counts[fallback_boundary_index, index]),
                float(fallback_values[fallback_boundary_index, index]),
            ),
        )
        chosen_indices = [final_index]
        for boundary_index in range(fallback_boundary_index, 0, -1):
            final_index = int(fallback_previous[boundary_index, final_index])
            chosen_indices.append(final_index)
        chosen_indices.reverse()
        return [replace(points[index], selected=True) for index in chosen_indices]

    final_index = max(valid_final, key=lambda index: float(dp[needed - 1, index]))
    chosen_indices = [final_index]
    for boundary_index in range(needed - 1, 0, -1):
        final_index = int(previous[boundary_index, final_index])
        chosen_indices.append(final_index)
    chosen_indices.reverse()
    return [
        _align_candidate(points[index], targets[boundary_index], sample_rate)
        for boundary_index, index in enumerate(chosen_indices)
    ]


def _select_automatic(
    candidates: list[BoundaryCandidate],
    *,
    music_start: float,
    music_end: float,
    settings: AnalysisSettings,
) -> list[BoundaryCandidate]:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.score >= settings.auto_boundary_score
        and candidate.cut_seconds - music_start >= settings.min_track_seconds
        and music_end - candidate.cut_seconds >= settings.min_track_seconds
    ]
    ordered = sorted(eligible, key=lambda item: (item.cut_seconds, -item.score))
    # Weighted interval scheduling with a lexicographic objective: first choose
    # as many feasible boundaries as possible, then maximize total confidence.
    # A local greedy replacement can lose two compatible outer boundaries to a
    # single, slightly higher-scoring middle boundary.
    best_sequences: list[list[BoundaryCandidate]] = [[]]
    best_scores = [0.0]
    for index, candidate in enumerate(ordered):
        compatible_index = index - 1
        while (
            compatible_index >= 0
            and candidate.cut_seconds - ordered[compatible_index].cut_seconds
            < settings.min_track_seconds
        ):
            compatible_index -= 1

        with_candidate = [*best_sequences[compatible_index + 1], candidate]
        with_score = best_scores[compatible_index + 1] + candidate.score
        without_candidate = best_sequences[index]
        without_score = best_scores[index]
        if len(with_candidate) > len(without_candidate) or (
            len(with_candidate) == len(without_candidate) and with_score > without_score
        ):
            best_sequences.append(with_candidate)
            best_scores.append(with_score)
        else:
            best_sequences.append(without_candidate)
            best_scores.append(without_score)

    return [replace(candidate, selected=True) for candidate in best_sequences[-1]]


def select_boundaries(
    candidates: list[BoundaryCandidate],
    *,
    music_start: float,
    music_end: float,
    sample_rate: int,
    settings: AnalysisSettings,
    expected_track_count: int | None,
    expected_durations: list[float | None] | None = None,
    expected_sides: list[str] | None = None,
) -> list[BoundaryCandidate]:
    _validate_expected_track_count(expected_track_count)
    if expected_track_count is not None:
        return _select_exact_count(
            candidates,
            music_start=music_start,
            music_end=music_end,
            sample_rate=sample_rate,
            expected_track_count=expected_track_count,
            expected_durations=expected_durations,
            expected_sides=expected_sides,
            min_track_seconds=settings.min_track_seconds,
        )
    return _select_automatic(
        candidates,
        music_start=music_start,
        music_end=music_end,
        settings=settings,
    )


def build_tracks(
    *,
    selected: list[BoundaryCandidate],
    music_start: float,
    music_end: float,
    sample_rate: int,
    track_seeds: list[TrackSeed] | None,
    metadata: dict[str, str],
) -> list[Track]:
    start_sample = _sample_from_seconds(music_start, sample_rate, "Music start time")
    end_sample = _sample_from_seconds(music_end, sample_rate, "Music end time")
    if end_sample <= start_sample:
        raise ProjectValidationError("Music bounds must contain at least one source sample.")
    score_by_sample: dict[int, float] = {}
    if end_sample - start_sample > 1:
        for candidate in selected:
            boundary_sample = max(start_sample + 1, min(end_sample - 1, candidate.cut_sample))
            if start_sample < boundary_sample < end_sample:
                score_by_sample[boundary_sample] = max(
                    score_by_sample.get(boundary_sample, 0.0), candidate.score
                )
    boundary_samples = sorted(score_by_sample)
    all_samples = [start_sample, *boundary_samples, end_sample]
    tracks: list[Track] = []

    for index, (track_start, track_end) in enumerate(zip(all_samples, all_samples[1:]), start=1):
        seed = track_seeds[index - 1] if track_seeds and index <= len(track_seeds) else None
        left_score = 1.0 if index == 1 else score_by_sample.get(track_start, 0.08)
        right_score = 1.0 if index == len(all_samples) - 1 else score_by_sample.get(track_end, 0.08)
        confidence = min(left_score, right_score)
        tracks.append(
            Track(
                number=index,
                title=seed.title if seed else f"Track {index:02d}",
                start_sample=track_start,
                end_sample=track_end,
                start_seconds=track_start / sample_rate,
                end_seconds=track_end / sample_rate,
                confidence=float(confidence),
                artist=(seed.artist if seed and seed.artist else metadata.get("artist", "")),
                album=metadata.get("album", ""),
                album_artist=metadata.get("album_artist", metadata.get("artist", "")),
                year=metadata.get("year", ""),
                genre=metadata.get("genre", ""),
                side=(seed.side if seed and seed.side else metadata.get("side", "")),
                expected_duration_seconds=seed.duration_seconds if seed else None,
            )
        )
    return tracks


def summarize_waveform(envelope_db: np.ndarray, target_points: int) -> list[float]:
    if target_points <= 0:
        return []
    normalized = np.clip((envelope_db + 72.0) / 66.0, 0.0, 1.0)
    if normalized.size <= target_points:
        return [round(float(value), 5) for value in normalized]
    edges = np.linspace(0, normalized.size, target_points + 1, dtype=np.int64)
    points = [
        float(np.max(normalized[edges[index] : edges[index + 1]]))
        for index in range(target_points)
        if edges[index + 1] > edges[index]
    ]
    return [round(value, 5) for value in points]


def _analyze_audio_snapshot(
    input_path: Path,
    *,
    source_snapshot: VerifiedAudioSnapshot,
    stored_source_path: str,
    settings: AnalysisSettings,
    expected_track_count: int | None = None,
    track_seeds: list[TrackSeed] | None = None,
    metadata: dict[str, str] | None = None,
) -> Project:
    _validate_expected_track_count(expected_track_count)
    metadata = dict(metadata or {})
    settings.validate()
    source = probe_audio(source_snapshot.path, stored_path=stored_source_path)
    source = replace(
        source,
        path=stored_source_path,
        filename=input_path.name,
        size_bytes=source_snapshot.size_bytes,
        modified_ns=source_snapshot.live_receipt.modified_ns,
        sha256=source_snapshot.sha256,
    )
    require_supported_capture(
        source,
        analysis_rate=settings.analysis_rate,
        analysis_window_ms=settings.window_ms,
    )
    _validate_track_seed_durations(track_seeds)
    raw_envelope, window_seconds = decode_rms_envelope(
        source_snapshot.path,
        analysis_rate=settings.analysis_rate,
        window_ms=settings.window_ms,
    )
    envelope = smooth_envelope(raw_envelope, settings.smoothing_windows)
    noise_floor, silence_threshold, active_threshold = estimate_thresholds(
        envelope, settings.threshold_margin_db
    )
    core_music_bounds = find_music_bounds(
        envelope,
        window_seconds=window_seconds,
        active_threshold_db=active_threshold,
        active_run_seconds=settings.active_run_seconds,
        lead_in_seconds=0.0,
        tail_seconds=0.0,
        duration_seconds=source.duration_seconds,
        boundary_threshold_db=silence_threshold,
    )
    if core_music_bounds is None:
        # Preserve a valid, reviewable one-track project, but do not reinterpret
        # an all-silent envelope as one enormous, high-confidence inter-track gap.
        music_start, music_end = 0.0, source.duration_seconds
        candidates: list[BoundaryCandidate] = []
        selected: list[BoundaryCandidate] = []
    else:
        core_music_start, core_music_end = core_music_bounds
        music_start = max(0.0, core_music_start - settings.lead_in_seconds)
        music_end = min(source.duration_seconds, core_music_end + settings.tail_seconds)
        candidates = detect_candidates(
            envelope,
            source_sample_rate=source.sample_rate,
            window_seconds=window_seconds,
            music_start_seconds=music_start,
            music_end_seconds=music_end,
            silence_threshold_db=silence_threshold,
            settings=settings,
        )
        expected_durations = (
            [seed.duration_seconds for seed in track_seeds] if track_seeds else None
        )
        expected_sides = [seed.side for seed in track_seeds] if track_seeds else None
        selected = select_boundaries(
            candidates,
            # Retained edge ambience belongs in exported tracks, but it must not
            # stretch canonical duration targets toward the file boundaries.
            music_start=core_music_start,
            music_end=core_music_end,
            sample_rate=source.sample_rate,
            settings=settings,
            expected_track_count=expected_track_count,
            expected_durations=expected_durations,
            expected_sides=expected_sides,
        )
    marked_candidates = [replace(candidate, selected=False) for candidate in candidates]
    # Replace a measured candidate with its duration-aligned cut, avoiding a
    # duplicate midpoint marker in the review UI. Include virtual boundaries so
    # the project still explains every low-confidence cut.
    for boundary in selected:
        match_index = next(
            (
                index
                for index, item in enumerate(marked_candidates)
                if boundary.duration_seconds > 0.0
                and abs(boundary.start_seconds - item.start_seconds) < 0.025
                and abs(boundary.end_seconds - item.end_seconds) < 0.025
            ),
            None,
        )
        if match_index is None:
            marked_candidates.append(boundary)
        else:
            marked_candidates[match_index] = boundary
    marked_candidates.sort(key=lambda item: item.cut_seconds)

    tracks = build_tracks(
        selected=selected,
        music_start=music_start,
        music_end=music_end,
        sample_rate=source.sample_rate,
        track_seeds=track_seeds,
        metadata=metadata,
    )
    summary = AnalysisSummary(
        music_start_seconds=music_start,
        music_end_seconds=music_end,
        noise_floor_db=noise_floor,
        silence_threshold_db=silence_threshold,
        active_threshold_db=active_threshold,
        envelope_window_seconds=window_seconds,
        candidates=marked_candidates,
        waveform=summarize_waveform(envelope, settings.waveform_points),
    )
    project = Project(
        source=source,
        settings=settings,
        analysis=summary,
        tracks=tracks,
        metadata=metadata,
        analyzer_baseline=AnalyzerBaseline.capture(tracks, metadata, source.sha256),
    )
    project.validate()
    return project


def analyze_audio(
    input_path: Path,
    *,
    stored_source_path: str,
    settings: AnalysisSettings,
    expected_track_count: int | None = None,
    track_seeds: list[TrackSeed] | None = None,
    metadata: dict[str, str] | None = None,
    source_snapshot: VerifiedAudioSnapshot | None = None,
) -> Project:
    """Analyze audio whose every probe and decode uses one verified snapshot."""

    _validate_expected_track_count(expected_track_count)
    settings.validate()
    input_path = input_path.expanduser().resolve()

    if source_snapshot is not None:
        if source_snapshot.live_path != input_path:
            raise ValueError("The analysis snapshot belongs to a different source path.")
        source_snapshot.assert_snapshot_unchanged()
        project = _analyze_audio_snapshot(
            input_path,
            source_snapshot=source_snapshot,
            stored_source_path=stored_source_path,
            settings=settings,
            expected_track_count=expected_track_count,
            track_seeds=track_seeds,
            metadata=metadata,
        )
        source_snapshot.assert_snapshot_unchanged()
        source_snapshot.assert_live_unchanged()
        return project

    with verified_audio_snapshot(input_path, label="Source audio") as snapshot:
        return _analyze_audio_snapshot(
            input_path,
            source_snapshot=snapshot,
            stored_source_path=stored_source_path,
            settings=settings,
            expected_track_count=expected_track_count,
            track_seeds=track_seeds,
            metadata=metadata,
        )
