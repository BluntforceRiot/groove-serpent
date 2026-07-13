"""Conservative, array-only detection and repair of isolated vinyl clicks.

Detection and repair are intentionally separate operations.  A caller can show
the proposed :class:`ClickInterval` objects for review, then pass only approved
half-open intervals to :func:`repair_click_intervals`.  Repair never widens an
interval and starts from a copy, so every sample outside the approved windows is
bit-for-bit identical to the input array.

This module is deliberately narrow.  It is not a broadband denoiser and it
cannot distinguish every one-sample musical event from physical damage.  The
detector therefore favors precision over recall and its output remains a review
candidate rather than permission to alter a recording.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal, Sequence

import numpy as np

from .errors import ProjectValidationError
from .validation import strict_finite_number


MAX_REPAIR_SAMPLES = 128


@dataclass(frozen=True, slots=True)
class ClickInterval:
    """An exact, half-open sample interval proposed as an isolated click.

    ``start_sample`` is inclusive and ``end_sample`` is exclusive.  ``channels``
    contains zero-based channel indices that contributed at least one candidate
    sample.  Confidence describes the strength of the discontinuity evidence;
    it is not an estimate of whether an automatic repair is perceptually safe.
    """

    start_sample: int
    end_sample: int
    peak_sample: int
    confidence: float
    channels: tuple[int, ...]

    def __post_init__(self) -> None:
        integer_fields = (self.start_sample, self.end_sample, self.peak_sample)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_fields):
            raise TypeError("sample positions must be integers")
        if self.start_sample < 0 or self.end_sample <= self.start_sample:
            raise ValueError("click interval must be a non-empty half-open interval")
        if not self.start_sample <= self.peak_sample < self.end_sample:
            raise ValueError("peak_sample must lie inside the click interval")
        try:
            confidence = strict_finite_number(self.confidence, "Confidence")
        except ProjectValidationError as exc:
            raise ValueError("confidence must be finite and between 0 and 1") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be finite and between 0 and 1")
        if not self.channels or any(
            isinstance(channel, bool) or not isinstance(channel, int) or channel < 0
            for channel in self.channels
        ):
            raise ValueError("channels must contain non-negative integer indices")
        if tuple(sorted(set(self.channels))) != self.channels:
            raise ValueError("channels must be unique and sorted")

    @property
    def length_samples(self) -> int:
        return self.end_sample - self.start_sample


def _as_frame_channel_array(audio: np.ndarray) -> tuple[np.ndarray, bool]:
    array = np.asarray(audio)
    if array.ndim not in (1, 2):
        raise ValueError("audio must have shape (frames,) or (frames, channels)")
    if not np.issubdtype(array.dtype, np.number) or np.issubdtype(
        array.dtype, np.complexfloating
    ):
        raise TypeError("audio must contain real numeric samples")
    if array.shape[0] == 0:
        raise ValueError("audio must contain at least one frame")
    frames_channels = array[:, np.newaxis] if array.ndim == 1 else array
    if frames_channels.shape[1] == 0:
        raise ValueError("audio must contain at least one channel")
    values = frames_channels.astype(np.float64, copy=False)
    if not np.all(np.isfinite(values)):
        raise ValueError("audio samples must all be finite")
    return values, array.ndim == 1


def group_click_candidates(
    candidate_mask: np.ndarray,
    *,
    scores: np.ndarray | None = None,
    max_gap_samples: int = 0,
) -> list[ClickInterval]:
    """Group candidate samples into exact half-open intervals.

    A one-dimensional mask represents mono audio; a two-dimensional mask uses
    ``(frames, channels)`` order.  ``max_gap_samples`` can deliberately bridge a
    small number of unflagged frames, in which case those bridged frames become
    part of the returned interval.  The default performs no such expansion.
    """

    if isinstance(max_gap_samples, bool) or not isinstance(max_gap_samples, int):
        raise TypeError("max_gap_samples must be an integer")
    if max_gap_samples < 0:
        raise ValueError("max_gap_samples must not be negative")

    mask = np.asarray(candidate_mask)
    if mask.ndim not in (1, 2):
        raise ValueError("candidate_mask must have one or two dimensions")
    mask_2d = mask[:, np.newaxis] if mask.ndim == 1 else mask
    mask_2d = mask_2d.astype(np.bool_, copy=False)
    if mask_2d.shape[1] == 0:
        raise ValueError("candidate_mask must contain at least one channel")

    if scores is None:
        score_2d = mask_2d.astype(np.float64)
    else:
        raw_scores = np.asarray(scores, dtype=np.float64)
        expected_shape = mask.shape
        if raw_scores.shape != expected_shape:
            raise ValueError("scores must have the same shape as candidate_mask")
        if not np.all(np.isfinite(raw_scores)):
            raise ValueError("scores must all be finite")
        score_2d = raw_scores[:, np.newaxis] if raw_scores.ndim == 1 else raw_scores
        score_2d = np.clip(score_2d, 0.0, 1.0)

    active = np.flatnonzero(np.any(mask_2d, axis=1))
    if active.size == 0:
        return []

    groups: list[tuple[int, int]] = []
    start = int(active[0])
    previous = start
    for raw_index in active[1:]:
        index = int(raw_index)
        if index - previous - 1 > max_gap_samples:
            groups.append((start, previous + 1))
            start = index
        previous = index
    groups.append((start, previous + 1))

    intervals: list[ClickInterval] = []
    for start, end in groups:
        frame_scores = np.max(score_2d[start:end], axis=1)
        peak = start + int(np.argmax(frame_scores))
        channels = tuple(
            int(channel)
            for channel in np.flatnonzero(np.any(mask_2d[start:end], axis=0))
        )
        intervals.append(
            ClickInterval(
                start_sample=start,
                end_sample=end,
                peak_sample=peak,
                confidence=float(frame_scores[peak - start]),
                channels=channels,
            )
        )
    return intervals


def _group_candidates_per_channel(
    candidate_mask: np.ndarray,
    scores: np.ndarray,
    *,
    max_gap_samples: int,
) -> list[ClickInterval]:
    """Keep unequal channel windows separate; merge only identical bounds."""

    merged: dict[tuple[int, int], ClickInterval] = {}
    for channel in range(candidate_mask.shape[1]):
        intervals = group_click_candidates(
            candidate_mask[:, channel],
            scores=scores[:, channel],
            max_gap_samples=max_gap_samples,
        )
        for interval in intervals:
            key = (interval.start_sample, interval.end_sample)
            previous = merged.get(key)
            if previous is None:
                merged[key] = ClickInterval(
                    start_sample=interval.start_sample,
                    end_sample=interval.end_sample,
                    peak_sample=interval.peak_sample,
                    confidence=interval.confidence,
                    channels=(channel,),
                )
                continue
            use_new_peak = interval.confidence > previous.confidence or (
                interval.confidence == previous.confidence
                and interval.peak_sample < previous.peak_sample
            )
            merged[key] = ClickInterval(
                start_sample=previous.start_sample,
                end_sample=previous.end_sample,
                peak_sample=(
                    interval.peak_sample if use_new_peak else previous.peak_sample
                ),
                confidence=max(previous.confidence, interval.confidence),
                channels=previous.channels + (channel,),
            )
    return sorted(
        merged.values(),
        key=lambda item: (item.start_sample, item.end_sample, item.channels),
    )


def _moving_median_and_mad(values: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Return interpolated robust local statistics with bounded temporary memory.

    Exact per-sample sliding medians are unnecessarily expensive for an impulse
    detector. Statistics sampled every quarter-window remain local at audio
    scale, resist the same isolated outliers, and make album scans practical.
    """

    radius = window // 2
    mode: Literal["reflect", "edge"] = "reflect" if values.size > 1 else "edge"
    padded = np.pad(values, (radius, radius), mode=mode)
    windows = np.lib.stride_tricks.sliding_window_view(padded, window)
    stride = max(1, window // 4)
    anchors = np.arange(0, values.size, stride, dtype=np.int64)
    if anchors[-1] != values.size - 1:
        anchors = np.append(anchors, values.size - 1)
    anchor_medians = np.empty(anchors.size, dtype=np.float64)
    anchor_deviations = np.empty(anchors.size, dtype=np.float64)

    # Bound each materialized advanced-index block to roughly 16 MiB.
    block_anchors = max(1, 2_000_000 // window)
    for start in range(0, anchors.size, block_anchors):
        end = min(anchors.size, start + block_anchors)
        selected = windows[anchors[start:end]]
        medians = np.median(selected, axis=1)
        anchor_medians[start:end] = medians
        anchor_deviations[start:end] = np.median(
            np.abs(selected - medians[:, np.newaxis]), axis=1
        )
    positions = np.arange(values.size, dtype=np.float64)
    return (
        np.interp(positions, anchors, anchor_medians),
        np.interp(positions, anchors, anchor_deviations),
    )


def detect_impulsive_clicks(
    audio: np.ndarray,
    *,
    threshold_sigma: float = 10.0,
    local_window_samples: int = 65,
    min_slope_ratio: float = 0.25,
    max_click_samples: int = 16,
    max_gap_samples: int = 0,
) -> list[ClickInterval]:
    """Propose short, two-sided impulsive discontinuities in mono or stereo audio.

    The detector compares each sample with both immediate neighbors.  A click
    normally produces large slopes of opposite sign (an abrupt excursion and
    return), while a legitimate attack or edit often produces one large one-way
    slope.  Curvature is measured against a moving median and median absolute
    deviation so the threshold follows changing musical density.

    Groups longer than ``max_click_samples`` are rejected rather than truncated.
    This prevents a broad musical transient from being silently converted into a
    small repair window.
    """

    values, _was_mono = _as_frame_channel_array(audio)
    try:
        rendered_threshold_sigma = strict_finite_number(
            threshold_sigma, "Impulse threshold sigma"
        )
    except ProjectValidationError as exc:
        raise ValueError("threshold_sigma must be finite and greater than zero") from exc
    if rendered_threshold_sigma <= 0.0:
        raise ValueError("threshold_sigma must be finite and greater than zero")
    if (
        isinstance(local_window_samples, bool)
        or not isinstance(local_window_samples, int)
        or local_window_samples < 5
        or local_window_samples % 2 == 0
    ):
        raise ValueError("local_window_samples must be an odd integer of at least 5")
    try:
        rendered_slope_ratio = strict_finite_number(
            min_slope_ratio, "Minimum slope ratio"
        )
    except ProjectValidationError as exc:
        raise ValueError("min_slope_ratio must be finite and between 0 and 1") from exc
    if not 0.0 <= rendered_slope_ratio <= 1.0:
        raise ValueError("min_slope_ratio must be finite and between 0 and 1")
    if (
        isinstance(max_click_samples, bool)
        or not isinstance(max_click_samples, int)
        or not 1 <= max_click_samples <= MAX_REPAIR_SAMPLES
    ):
        raise ValueError(f"max_click_samples must be between 1 and {MAX_REPAIR_SAMPLES}")
    if isinstance(max_gap_samples, bool) or not isinstance(max_gap_samples, int):
        raise TypeError("max_gap_samples must be an integer")
    if max_gap_samples < 0:
        raise ValueError("max_gap_samples must not be negative")
    if values.shape[0] < 3:
        return []

    candidate_mask = np.zeros(values.shape, dtype=np.bool_)
    confidence = np.zeros(values.shape, dtype=np.float64)
    for channel in range(values.shape[1]):
        samples = values[:, channel]
        left_slope = samples[1:-1] - samples[:-2]
        right_slope = samples[2:] - samples[1:-1]
        curvature = np.abs(left_slope - right_slope)
        local_median, local_mad = _moving_median_and_mad(
            curvature, local_window_samples
        )
        numerical_floor = (
            np.finfo(np.float64).eps * max(1.0, float(np.max(np.abs(samples)))) * 32.0
        )
        robust_scale = np.maximum(1.4826 * local_mad, numerical_floor)
        threshold = local_median + threshold_sigma * robust_scale

        larger_slope = np.maximum(np.abs(left_slope), np.abs(right_slope))
        slope_ratio = np.divide(
            np.minimum(np.abs(left_slope), np.abs(right_slope)),
            larger_slope,
            out=np.zeros_like(larger_slope),
            where=larger_slope > numerical_floor,
        )
        impulsive = (
            (left_slope * right_slope < 0.0)
            & (slope_ratio >= min_slope_ratio)
            & (curvature > threshold)
        )
        ratio = np.divide(
            threshold,
            curvature,
            out=np.ones_like(curvature),
            where=curvature > 0.0,
        )
        channel_confidence = np.clip(1.0 - ratio, 0.0, 1.0)
        candidate_mask[1:-1, channel] = impulsive
        confidence[1:-1, channel] = np.where(impulsive, channel_confidence, 0.0)

    grouped = _group_candidates_per_channel(
        candidate_mask,
        confidence,
        max_gap_samples=max_gap_samples,
    )
    return [interval for interval in grouped if interval.length_samples <= max_click_samples]


def detect_clipped_runs(
    audio: np.ndarray,
    *,
    threshold_ratio: float = 0.9999,
    max_clip_samples: int = MAX_REPAIR_SAMPLES,
    max_gap_samples: int = 16,
) -> list[ClickInterval]:
    """Propose short full-scale plateaus that the curvature detector can miss.

    A hard-clipped pop may remain at full scale for dozens of samples, leaving
    only its entrance and exit as high-curvature points.  This detector records
    the exact saturated run.  Musical clipping remains possible, so every result
    still requires audition and explicit approval.
    """

    source = np.asarray(audio)
    values, _was_mono = _as_frame_channel_array(source)
    try:
        rendered_threshold_ratio = strict_finite_number(
            threshold_ratio, "Clipping threshold ratio"
        )
    except ProjectValidationError as exc:
        raise ValueError("threshold_ratio must be finite and between 0.5 and 1.0") from exc
    if not 0.5 <= rendered_threshold_ratio <= 1.0:
        raise ValueError("threshold_ratio must be finite and between 0.5 and 1.0")
    if (
        isinstance(max_clip_samples, bool)
        or not isinstance(max_clip_samples, int)
        or not 1 <= max_clip_samples <= MAX_REPAIR_SAMPLES
    ):
        raise ValueError(f"max_clip_samples must be between 1 and {MAX_REPAIR_SAMPLES}")
    if isinstance(max_gap_samples, bool) or not isinstance(max_gap_samples, int):
        raise TypeError("max_gap_samples must be an integer")
    if max_gap_samples < 0:
        raise ValueError("max_gap_samples must not be negative")

    if np.issubdtype(source.dtype, np.integer):
        limits = np.iinfo(source.dtype)
        positive_limit = float(limits.max)
        negative_limit = float(limits.min)
    else:
        positive_limit = 1.0
        negative_limit = -1.0
    clipped = (values >= positive_limit * threshold_ratio) | (
        values <= negative_limit * threshold_ratio
    )
    magnitude = np.abs(values)
    scale = max(abs(positive_limit), abs(negative_limit), np.finfo(np.float64).eps)
    confidence = np.where(clipped, np.clip(magnitude / scale, 0.0, 1.0), 0.0)
    grouped = _group_candidates_per_channel(
        clipped,
        confidence,
        max_gap_samples=max_gap_samples,
    )
    return [interval for interval in grouped if interval.length_samples <= max_clip_samples]


def _lpc_predict(context: np.ndarray, count: int, max_order: int) -> np.ndarray:
    """Extrapolate from one side with regularized linear prediction."""

    order = min(max_order, (context.size - 1) // 3)
    if order < 2:
        raise ValueError("not enough clean context for linear prediction")

    center = float(np.mean(context))
    centered = context - center
    windows = np.lib.stride_tricks.sliding_window_view(centered, order + 1)
    predictors = windows[:, :order][:, ::-1]
    targets = windows[:, -1]
    gram = predictors.T @ predictors
    scale = float(np.trace(gram)) / order
    regularization = max(np.finfo(np.float64).eps, scale * 1e-8)
    gram.flat[:: order + 1] += regularization
    coefficients = np.linalg.solve(gram, predictors.T @ targets)

    history = list(centered[-order:])
    context_peak = float(np.max(np.abs(centered)))
    prediction_limit = max(np.finfo(np.float64).eps, context_peak * 4.0)
    prediction = np.empty(count, dtype=np.float64)
    for index in range(count):
        value = float(np.dot(coefficients, np.asarray(history[-order:][::-1])))
        if not math.isfinite(value) or abs(value) > prediction_limit:
            raise ValueError("linear prediction became unstable")
        prediction[index] = value + center
        history.append(value)
    return prediction


def _hermite_fill(samples: np.ndarray, start: int, end: int) -> np.ndarray:
    """Stable low-context fallback with boundary slope continuity."""

    length = end - start
    left_value = float(samples[start - 1])
    right_value = float(samples[end])
    left_context = samples[max(0, start - 5) : start]
    right_context = samples[end : min(samples.size, end + 5)]
    fallback_slope = (right_value - left_value) / (length + 1)
    left_slope = (
        float(np.median(np.diff(left_context))) if left_context.size >= 2 else fallback_slope
    )
    right_slope = (
        float(np.median(np.diff(right_context))) if right_context.size >= 2 else fallback_slope
    )

    position = np.arange(1, length + 1, dtype=np.float64) / (length + 1)
    position_squared = position * position
    position_cubed = position_squared * position
    span = length + 1
    return (
        (2.0 * position_cubed - 3.0 * position_squared + 1.0) * left_value
        + (position_cubed - 2.0 * position_squared + position) * span * left_slope
        + (-2.0 * position_cubed + 3.0 * position_squared) * right_value
        + (position_cubed - position_squared) * span * right_slope
    )


def _repair_channel(
    samples: np.ndarray,
    start: int,
    end: int,
    *,
    context_samples: int,
    lpc_order: int,
) -> np.ndarray:
    length = end - start
    left = samples[max(0, start - context_samples) : start]
    right = samples[end : min(samples.size, end + context_samples)]
    try:
        forward = _lpc_predict(left, length, lpc_order)
        backward = _lpc_predict(right[::-1], length, lpc_order)[::-1]
        position = np.arange(1, length + 1, dtype=np.float64) / (length + 1)
        right_weight = 0.5 - 0.5 * np.cos(np.pi * position)
        fill = forward * (1.0 - right_weight) + backward * right_weight
        # Preserve the LPC detail in the center while forcing a stable,
        # slope-matched approach to both clean boundaries. Unconstrained LPC
        # can otherwise reconstruct the interior well but introduce a new click
        # at the first repaired sample.
        boundary_fill = _hermite_fill(samples, start, end)
        detail_weight = np.ones(length, dtype=np.float64)
        fade_count = min(2, length // 2)
        if fade_count > 0:
            edge_weight = np.linspace(0.0, 1.0, fade_count, dtype=np.float64)
            detail_weight[:fade_count] = edge_weight
            detail_weight[-fade_count:] = edge_weight[::-1]
        fill = boundary_fill + detail_weight * (fill - boundary_fill)
    except (ValueError, np.linalg.LinAlgError):
        fill = _hermite_fill(samples, start, end)

    # Spread each transition from clean audio over several reconstructed
    # samples. This makes the repair continuous at both exact boundaries even
    # when a clipped ramp has contaminated the nearest slope estimate.
    if length < 4:
        fill = np.linspace(
            float(samples[start - 1]),
            float(samples[end]),
            length + 2,
            dtype=np.float64,
        )[1:-1]
    else:
        edge_count = min(2, length // 4)
        left_target = float(fill[edge_count])
        fill[:edge_count] = np.linspace(
            float(samples[start - 1]),
            left_target,
            edge_count + 2,
            dtype=np.float64,
        )[1:-1]
        right_target = float(fill[-edge_count - 1])
        fill[-edge_count:] = np.linspace(
            right_target,
            float(samples[end]),
            edge_count + 2,
            dtype=np.float64,
        )[1:-1]

    clean_context = np.concatenate((left, right))
    peak = float(np.max(np.abs(clean_context))) if clean_context.size else 0.0
    if peak == 0.0:
        return np.zeros(length, dtype=np.float64)
    # A final stability guard only constrains an extrapolator that has exceeded
    # every nearby clean sample by an implausibly large margin.
    return np.clip(fill, -4.0 * peak, 4.0 * peak)


def _normalize_repair_intervals(
    intervals: Sequence[ClickInterval | tuple[int, int]],
    frame_count: int,
    channel_count: int,
) -> list[tuple[int, int, tuple[int, ...] | None]]:
    normalized: list[tuple[int, int, tuple[int, ...] | None]] = []
    for interval in intervals:
        if isinstance(interval, ClickInterval):
            start, end = interval.start_sample, interval.end_sample
            channels: tuple[int, ...] | None = interval.channels
        else:
            if isinstance(interval, (str, bytes)) or len(interval) != 2:
                raise TypeError("each interval must be ClickInterval or a (start, end) pair")
            start, end = interval
            channels = None
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, np.integer))
            or not isinstance(end, (int, np.integer))
        ):
            raise TypeError("repair interval positions must be integers")
        start, end = int(start), int(end)
        if start < 1 or end >= frame_count or end <= start:
            raise ValueError(
                "repair intervals must be non-empty and have a clean sample on both sides"
            )
        if end - start > MAX_REPAIR_SAMPLES:
            raise ValueError(
                f"repair intervals may contain at most {MAX_REPAIR_SAMPLES} samples"
            )
        if channels is not None and any(channel >= channel_count for channel in channels):
            raise ValueError("repair interval contains a channel outside the audio array")
        normalized.append((start, end, channels))

    normalized.sort(key=lambda item: (item[0], item[1]))
    for index, previous in enumerate(normalized):
        for current in normalized[index + 1 :]:
            if current[0] > previous[1]:
                break
            previous_channels = previous[2]
            current_channels = current[2]
            channels_are_disjoint = (
                previous_channels is not None
                and current_channels is not None
                and set(previous_channels).isdisjoint(current_channels)
            )
            if not channels_are_disjoint:
                raise ValueError(
                    "repair intervals must not overlap or touch in the same channel; "
                    "group them first"
                )
    return normalized


def repair_click_intervals(
    audio: np.ndarray,
    intervals: Sequence[ClickInterval | tuple[int, int]],
    *,
    context_samples: int = 512,
    lpc_order: int = 24,
) -> np.ndarray:
    """Repair only explicitly supplied intervals and return a new array.

    Approved channels are inpainted independently using regularized, bidirectional
    linear prediction and a raised-cosine crossfade.  This reconstructs local
    periodic detail substantially better than straight-line interpolation.  A
    slope-matched cubic Hermite fill is used when too little clean context is
    available or a predictor is unstable.

    A ``ClickInterval`` repairs only its listed channels; a plain ``(start,
    end)`` pair explicitly repairs every channel. Intervals are never expanded
    and may be at most 128 samples.  The returned
    array has the same shape and dtype as ``audio``; outside those intervals its
    values are copied without arithmetic and are therefore bit-for-bit equal.
    """

    values, was_mono = _as_frame_channel_array(audio)
    if (
        isinstance(context_samples, bool)
        or not isinstance(context_samples, int)
        or context_samples < 4
    ):
        raise ValueError("context_samples must be an integer of at least 4")
    if isinstance(lpc_order, bool) or not isinstance(lpc_order, int) or not 2 <= lpc_order <= 64:
        raise ValueError("lpc_order must be an integer between 2 and 64")

    normalized = _normalize_repair_intervals(
        intervals, values.shape[0], values.shape[1]
    )
    source = np.asarray(audio)
    result = np.array(source, copy=True)
    result_2d = result[:, np.newaxis] if was_mono else result

    for start, end, approved_channels in normalized:
        channels = (
            range(values.shape[1])
            if approved_channels is None
            else approved_channels
        )
        for channel in channels:
            fill = _repair_channel(
                values[:, channel],
                start,
                end,
                context_samples=context_samples,
                lpc_order=lpc_order,
            )
            if np.issubdtype(result.dtype, np.integer):
                limits = np.iinfo(result.dtype)
                encoded = np.clip(np.rint(fill), limits.min, limits.max).astype(result.dtype)
            else:
                encoded = fill.astype(result.dtype, copy=False)
            result_2d[start:end, channel] = encoded
    return result


__all__ = [
    "ClickInterval",
    "MAX_REPAIR_SAMPLES",
    "detect_clipped_runs",
    "detect_impulsive_clicks",
    "group_click_candidates",
    "repair_click_intervals",
]
