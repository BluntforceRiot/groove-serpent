"""Strict JSON decoding for state-changing local interfaces."""

from __future__ import annotations

import json
import math
from typing import Any, NoReturn


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> NoReturn:
    raise ValueError(f"Invalid non-finite JSON number: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Invalid non-finite JSON number: {value}")
    return parsed


def decode_strict_json(payload: bytes) -> object:
    """Decode UTF-8 JSON while rejecting ambiguous or non-standard values.

    The standard-library decoder otherwise accepts duplicate object fields and
    JavaScript-style ``NaN``/``Infinity`` constants. Both are inappropriate at
    mutation boundaries because two implementations can interpret the same
    request differently.
    """

    value: object = json.loads(
        payload.decode("utf-8", errors="strict"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonfinite_constant,
        parse_float=_parse_finite_float,
    )
    return value


__all__ = ["decode_strict_json"]
