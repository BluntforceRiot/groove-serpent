from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "public" / ".github" / "workflows"
if not WORKFLOWS.is_dir():
    WORKFLOWS = ROOT / ".github" / "workflows"

USES_LINE = re.compile(
    r"^\s*uses:\s*[^@\s]+@(?P<sha>[0-9a-f]{40})\s+#\s+v\d+(?:\.\d+){1,2}\s*$"
)


def test_action_references_are_immutable_and_version_annotated() -> None:
    workflow_files = sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))
    assert workflow_files, f"No GitHub Actions workflows found in {WORKFLOWS}"

    references: list[str] = []
    invalid: list[str] = []
    for workflow in workflow_files:
        for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1):
            if re.match(r"^\s*uses\s*:", line):
                references.append(line.strip())
                if USES_LINE.fullmatch(line) is None:
                    invalid.append(f"{workflow.name}:{number}: {line.strip()}")

    assert references, "No action references found"
    message = "Action references must use 40-hex SHAs and version comments:\n"
    assert not invalid, message + "\n".join(invalid)
