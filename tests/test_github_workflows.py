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


def _ci_workflow_text() -> str:
    workflow = WORKFLOWS / "ci.yml"
    assert workflow.is_file(), f"CI workflow is missing: {workflow}"
    return workflow.read_text(encoding="utf-8")


def test_ci_uses_canonical_runner_temp_without_weakening_path_checks() -> None:
    text = _ci_workflow_text()
    test_job = text.split("\n  package:", maxsplit=1)[0]

    assert "if: runner.os == 'macOS'" in test_job
    assert 'echo "TMPDIR=$RUNNER_TEMP" >> "$GITHUB_ENV"' in test_job


def test_windows_ci_installs_pinned_full_ffmpeg_with_libsoxr_smoke() -> None:
    text = _ci_workflow_text()

    assert (
        "choco install ffmpeg-full --version=8.1.2 --yes --no-progress" in text
    )
    assert "choco install ffmpeg --yes --no-progress" not in text
    assert (
        'aresample=44100:resampler=soxr:precision=33:cutoff=0.99' in text
    )


def test_macos_ci_pins_libsoxr_enabled_ffmpeg_formula() -> None:
    text = _ci_workflow_text()

    assert "brew uninstall --ignore-dependencies ffmpeg || true" in text
    assert "brew tap homebrew-ffmpeg/ffmpeg" in text
    assert (
        'git -C "$(brew --repo homebrew-ffmpeg/ffmpeg)" checkout --detach '
        "c771da5a0a6bd5ddde6c07cb014570f872e851da" in text
    )
    assert (
        "brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-libsoxr" in text
    )
