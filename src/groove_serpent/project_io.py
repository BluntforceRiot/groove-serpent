from __future__ import annotations

import json
import os
import tempfile
from hashlib import sha256
from pathlib import Path

from .errors import ProjectValidationError
from .models import Project, utc_now_iso


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def save_project(project: Project, path: Path) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    project.validate()
    next_revision = project.revision + 1 if path.exists() else project.revision
    next_updated_at = utc_now_iso()
    serialized = project.to_dict()
    serialized["revision"] = next_revision
    serialized["updated_at"] = next_updated_at
    payload = (
        json.dumps(serialized, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        project.revision = next_revision
        project.updated_at = next_updated_at
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def load_project_with_sha256(path: Path) -> tuple[Project, str]:
    path = path.expanduser().resolve()
    try:
        raw = path.read_bytes()
        data = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
        if not isinstance(data, dict):
            raise ValueError("The project root must be a JSON object.")
        return Project.from_dict(data), sha256(raw).hexdigest()
    except ProjectValidationError:
        raise
    except (
        AttributeError,
        KeyError,
        OverflowError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise ProjectValidationError(f"Project file is invalid: {exc}") from exc


def load_project(path: Path) -> Project:
    return load_project_with_sha256(path)[0]
