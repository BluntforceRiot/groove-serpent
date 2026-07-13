"""Read-only discovery for the optional Audacity restoration backend."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


_MODULE_STATES = {
    "0": "disabled",
    "1": "enabled",
    "2": "ask",
    "3": "failed",
    "4": "new",
}


@dataclass(frozen=True, slots=True)
class AudacityStatus:
    installed: bool
    executable: str = ""
    script_module_installed: bool = False
    script_module_state: str = "unknown"
    preferences_path: str = ""
    message: str = ""

    @property
    def script_pipe_enabled(self) -> bool:
        return self.script_module_installed and self.script_module_state == "enabled"

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "executable": self.executable,
            "script_module_installed": self.script_module_installed,
            "script_module_state": self.script_module_state,
            "script_pipe_enabled": self.script_pipe_enabled,
            "preferences_path": self.preferences_path,
            "message": self.message,
        }


def _candidate_executables() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("GROOVE_SERPENT_AUDACITY", "").strip()
    if configured:
        candidates.append(Path(os.path.expandvars(configured)).expanduser())
    resolved = shutil.which("audacity") or shutil.which("Audacity")
    if resolved:
        candidates.append(Path(resolved))
    if os.name == "nt":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable, "").strip()
            if root:
                candidates.append(Path(root) / "Audacity" / "Audacity.exe")
        candidates.extend(
            [
                Path(r"C:\Program Files\Audacity\Audacity.exe"),
                Path(r"C:\Program Files (x86)\Audacity\Audacity.exe"),
            ]
        )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _module_preference() -> tuple[str, str]:
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return "unknown", ""
    paths = [
        Path(appdata) / "audacity" / "audacity.cfg",
        Path(appdata) / "Audacity" / "audacity.cfg",
    ]
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        section = ""
        for raw_line in lines:
            line = raw_line.strip()
            section_match = re.fullmatch(r"\[([^]]+)\]", line)
            if section_match:
                section = section_match.group(1).strip().casefold()
                continue
            if section != "module" or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().casefold() == "mod-script-pipe":
                return _MODULE_STATES.get(value.strip(), "unknown"), str(path)
        return "unknown", str(path)
    return "unknown", ""


def discover_audacity() -> AudacityStatus:
    """Describe Audacity scripting readiness without launching or enabling it."""

    executable = next((path.resolve() for path in _candidate_executables() if path.is_file()), None)
    if executable is None:
        return AudacityStatus(
            installed=False,
            message="Audacity was not found; the built-in splitter remains available.",
        )
    module_path = executable.parent / "modules" / "mod-script-pipe.dll"
    module_installed = module_path.is_file()
    state, preferences_path = _module_preference()
    if not module_installed:
        message = "Audacity is installed, but mod-script-pipe is not present."
    elif state == "enabled":
        message = (
            "Audacity scripting is enabled. Use only the fixed, approval-gated "
            "micro-repair adapter in a dedicated empty project."
        )
    elif state in {"new", "disabled", "ask", "unknown"}:
        message = (
            "Audacity and mod-script-pipe are installed, but scripting is not enabled. "
            "Enable it manually and restart Audacity only when choosing that optional backend."
        )
    else:
        message = "Audacity's scripting module previously failed to load."
    return AudacityStatus(
        installed=True,
        executable=str(executable),
        script_module_installed=module_installed,
        script_module_state=state,
        preferences_path=preferences_path,
        message=message,
    )


__all__ = ["AudacityStatus", "discover_audacity"]
