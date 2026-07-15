from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _run(command: list[str]) -> None:
    """Run one repository quality gate and stop at its first failure."""

    print("+", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    """Check typing on Windows/Linux targets plus the declared style gates."""

    _run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "src/groove_serpent",
        ]
    )
    _run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--platform",
            "linux",
            "--no-incremental",
            "src/groove_serpent",
        ]
    )
    _run([sys.executable, "-m", "flake8", "src", "tests", "scripts"])
    ruff = shutil.which("ruff")
    node = shutil.which("node")
    if ruff is None:
        raise RuntimeError("Ruff is missing; run this command through the dev environment.")
    if node is None:
        raise RuntimeError("Node.js is missing; JavaScript syntax was not checked.")
    _run([ruff, "check", "src", "tests", "scripts"])
    for script in (
        "src/groove_serpent/web/app.js",
        "src/groove_serpent/web/album.js",
        "playwright.config.mjs",
        "tests/browser/fixture-crash-probe.mjs",
        "tests/browser/fixture-process.mjs",
        "tests/browser/fixture-process.test.mjs",
        "tests/browser/album-workbench.spec.mjs",
        "tests/browser/side-review-accessibility.spec.mjs",
    ):
        _run([node, "--check", script])
    print("All quality gates passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
