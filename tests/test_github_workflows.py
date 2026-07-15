from __future__ import annotations

import hashlib
import json
import re
import textwrap
from pathlib import Path

import pytest


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


def test_ci_uploads_the_source_archive_emitted_by_the_release_builder() -> None:
    text = _ci_workflow_text()
    source_jobs = text.split("\n  source-archive:", maxsplit=1)[1]

    assert "dist/groove-serpent-1.0.0-source.zip" in text
    assert "dist/groove-serpent-0.5.0-alpha.1-source.zip" not in text
    assert "dist/groove-serpent-1.0.0-source.commit.json" in source_jobs
    build = source_jobs.index("- name: Build normalized source archive")
    verify = source_jobs.index("- name: Verify source archive commit marker")
    upload = source_jobs.index("- name: Upload archive, manifest, and commit marker")
    assert build < verify < upload
    assert "python scripts/build_public_archive.py --verify" in source_jobs
    compare = source_jobs.split("\n  compare-source-archives:", maxsplit=1)[1]
    assert "- name: Check out source" in compare
    assert "- name: Verify every downloaded commit marker" in compare
    assert "--marker \"$directory/groove-serpent-1.0.0-source.commit.json\"" in compare
    assert "Require byte-identical archives, manifests, and markers" in compare
    assert "path: ${{ runner.temp }}/source-comparison" in compare
    assert "path: comparison" not in compare


def test_distribution_scanner_rejects_json_escaped_windows_paths() -> None:
    text = _ci_workflow_text()
    body = text.split("          private_patterns = (\n", maxsplit=1)[1].split(
        "          )\n\n",
        maxsplit=1,
    )[0]
    namespace: dict[str, object] = {"re": re}
    exec("private_patterns = (\n" + textwrap.dedent(body) + ")\n", namespace)
    patterns = namespace["private_patterns"]
    assert isinstance(patterns, tuple)
    payloads = (
        b'{"path":"X:' b'\\\\Users\\\\neutral\\\\file"}',
        b'{"path":"X:' b'\\\\HomelabForge\\\\release"}',
    )
    assert all(any(pattern.search(payload) for pattern in patterns) for payload in payloads)


def test_ci_uses_the_audited_deterministic_python_distribution_builder() -> None:
    text = _ci_workflow_text()
    package_job = text.split("\n  package:", maxsplit=1)[1].split(
        "\n  source-archive:", maxsplit=1
    )[0]

    assert "python scripts/build_python_distributions.py" in package_job
    assert "run: uv build" not in package_job
    assert "twine check --strict dist/*.whl dist/*.tar.gz" in package_job
    assert "twine check dist/*\n" not in package_job
    reconcile = package_job.index(
        "- name: Reconcile final distributions against the build receipt"
    )
    upload = package_job.index("- name: Upload audited Python distributions and receipt")
    assert reconcile < upload
    assert "- name:" not in package_job[reconcile:upload].split("run: |", maxsplit=1)[1]
    assert 'expected_names = {receipt_name, wheel_name, sdist_name}' in package_job
    assert 'hashlib.sha256(payload).hexdigest()' in package_job
    assert 'receipt.get("result") != "passed"' in package_job
    assert "Upload audited Python distributions and receipt" in package_job
    assert (
        "uses: actions/upload-artifact@"
        "ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2"
    ) in package_job
    assert "dist/groove_serpent-1.0.0-py3-none-any.whl" in package_job
    assert "dist/groove_serpent-1.0.0.tar.gz" in package_job
    assert "dist/PYTHON_DISTRIBUTIONS_RECEIPT.json" in package_job
    assert "if-no-files-found: error" in package_job
    assert "retention-days: 7" in package_job


def test_ci_final_distribution_reconciliation_executes_and_rejects_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _ci_workflow_text()
    marker = "      - name: Reconcile final distributions against the build receipt\n"
    step = text.split(marker, maxsplit=1)[1].split("\n      - name:", maxsplit=1)[0]
    code = textwrap.dedent(step.split("        run: |\n", maxsplit=1)[1])
    compile(code, "ci-distribution-reconciliation", "exec")

    dist = tmp_path / "dist"
    dist.mkdir()
    wheel_name = "groove_serpent-1.0.0-py3-none-any.whl"
    sdist_name = "groove_serpent-1.0.0.tar.gz"
    payloads = {wheel_name: b"wheel", sdist_name: b"sdist"}
    for name, payload in payloads.items():
        (dist / name).write_bytes(payload)
    receipt = {
        "schema": "groove-serpent/python-distribution-build-receipt/1",
        "result": "passed",
        "project": {"name": "groove-serpent", "version": "1.0.0"},
        "outputs": [
            {
                "role": role,
                "filename": name,
                "bytes": len(payloads[name]),
                "sha256": hashlib.sha256(payloads[name]).hexdigest(),
            }
            for role, name in (("wheel", wheel_name), ("sdist", sdist_name))
        ],
    }
    (dist / "PYTHON_DISTRIBUTIONS_RECEIPT.json").write_text(
        json.dumps(receipt),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    exec(code, {})
    (dist / wheel_name).write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="does not match"):
        exec(code, {})


def test_public_bug_template_prompts_for_the_current_release_version() -> None:
    bug_template = WORKFLOWS.parent / "ISSUE_TEMPLATE" / "bug.yml"
    text = bug_template.read_text(encoding="utf-8")

    assert "placeholder: 1.0.0" in text
    assert "placeholder: 0.5.0a1" not in text


def test_quality_gate_syntax_checks_every_browser_spec() -> None:
    text = (ROOT / "scripts" / "check_quality.py").read_text(encoding="utf-8")

    assert '"tests/browser/album-workbench.spec.mjs"' in text
    assert '"tests/browser/fixture-crash-probe.mjs"' in text
    assert '"tests/browser/fixture-process.mjs"' in text
    assert '"tests/browser/fixture-process.test.mjs"' in text
    assert '"tests/browser/side-review-accessibility.spec.mjs"' in text
