"""Cross-version stable filesystem creation identity."""

from __future__ import annotations

import os


def stable_creation_time_ns(value: os.stat_result) -> int | None:
    """Return a stable creation timestamp where the runtime exposes one.

    Python 3.12 exposes Windows creation time as ``st_birthtime_ns``. Earlier
    supported Windows runtimes expose the same stable value as ``st_ctime_ns``.
    POSIX ctime is mutable and is therefore never used as an incarnation token.
    """

    birth = getattr(value, "st_birthtime_ns", None)
    if birth is not None:
        return int(birth)
    if os.name == "nt":
        return int(value.st_ctime_ns)
    return None


__all__ = ["stable_creation_time_ns"]
