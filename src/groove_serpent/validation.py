"""Shared strict validation primitives for public numeric inputs."""

from __future__ import annotations

import math
from typing import cast

from .errors import ProjectValidationError


def strict_finite_number(value: object, label: str) -> float:
    """Return one genuine finite JSON number without coercion or overflow.

    Python integers are unbounded, while the C double conversion used by
    :func:`math.isfinite` is not.  Converting inside the guarded block keeps an
    extreme-but-valid JSON integer on the same stable validation path as NaN,
    infinity, strings, booleans, and null.
    """

    if type(value) not in (int, float):
        raise ProjectValidationError(f"{label} must be a finite JSON number.")
    numeric = cast(int | float, value)
    try:
        rendered = float(numeric)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ProjectValidationError(
            f"{label} must be a finite JSON number."
        ) from exc
    if not math.isfinite(rendered):
        raise ProjectValidationError(f"{label} must be a finite JSON number.")
    return rendered
