from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import zipfile
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


def test_ci_uploads_a_candidate_archive_without_claiming_release_authority() -> None:
    text = _ci_workflow_text()
    source_jobs = text.split("\n  source-archive:", maxsplit=1)[1]

    assert "dist/groove-serpent-1.1.0-source.zip" in text
    assert "dist/groove-serpent-0.5.0-alpha.1-source.zip" not in text
    assert "dist/groove-serpent-1.1.0-source.commit.json" in source_jobs
    build = source_jobs.index("- name: Build normalized candidate source archive")
    verify = source_jobs.index("- name: Verify candidate source archive commit marker")
    upload = source_jobs.index("- name: Upload archive, manifest, and commit marker")
    assert build < verify < upload
    assert "python scripts/build_public_archive.py --candidate" in source_jobs
    assert "python scripts/build_public_archive.py --verify-candidate" in source_jobs
    assert "python scripts/build_public_archive.py --verify\n" not in source_jobs
    compare = source_jobs.split("\n  compare-source-archives:", maxsplit=1)[1]
    assert "- name: Check out source" in compare
    assert "- name: Verify every downloaded candidate commit marker" in compare
    assert "python scripts/build_public_archive.py --verify-candidate" in compare
    assert "--marker \"$directory/groove-serpent-1.1.0-source.commit.json\"" in compare
    assert "Require byte-identical archives, manifests, and markers" in compare
    assert "path: ${{ runner.temp }}/source-comparison" in compare
    assert "path: comparison" not in compare


def test_distribution_scanner_rejects_json_escaped_windows_paths() -> None:
    from scripts._release_evidence import assert_public_payload_safe

    text = _ci_workflow_text()
    assert "from scripts._release_evidence import assert_public_payload_safe" in text
    assert 'assert_public_payload_safe(name, data, context="CI distribution member")' in text
    payload = json.dumps({"path": "\\".join(("X:", "Users", "synthetic", "file"))}).encode()
    with pytest.raises(RuntimeError, match="private material"):
        assert_public_payload_safe("sample.json", payload, context="CI regression")


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
    assert "dist/groove_serpent-1.1.0-py3-none-any.whl" in package_job
    assert "dist/groove_serpent-1.1.0.tar.gz" in package_job
    assert "dist/PYTHON_DISTRIBUTIONS_RECEIPT.json" in package_job
    assert "if-no-files-found: error" in package_job
    assert "retention-days: 7" in package_job


def test_hosted_browser_ci_requires_verified_native_audio_output() -> None:
    text = _ci_workflow_text()
    browser_job = text.split("\n  browser-e2e:", maxsplit=1)[1].split(
        "\n  package:", maxsplit=1
    )[0]
    assert "sudo apt-get install --yes ffmpeg pulseaudio pulseaudio-utils" in browser_job
    assert (
        "uv run --frozen --group dev python scripts/run_browser_acceptance.py "
        "--engine ${{ matrix.engine }}" in browser_job
    )
    assert "continue-on-error" not in browser_job
    assert "fail-fast: false" in browser_job
    assert "node --test tests/browser/fixture-process.test.mjs" in browser_job
    assert "startup-audio-monitor" not in browser_job
    assert "name: Verify browser fixture contracts" in browser_job


def test_distribution_scanner_runs_from_runner_temp_and_rejects_private_payload(
    tmp_path: Path,
) -> None:
    text = _ci_workflow_text()
    marker = "      - name: Audit package contents for private material\n"
    step = text.split(marker, maxsplit=1)[1].split("\n      - name:", maxsplit=1)[0]
    assert "        shell: python {0}\n" in step
    assert "        env:\n          PYTHONPATH: ${{ github.workspace }}\n" in step
    code = textwrap.dedent(step.split("        run: |\n", maxsplit=1)[1])

    runner_temp = tmp_path / "runner temp"
    runner_temp.mkdir()
    script = runner_temp / "audit.py"
    script.write_text(code, encoding="utf-8")
    workspace = tmp_path / "workspace"
    dist = workspace / "dist"
    dist.mkdir(parents=True)
    archive = dist / "synthetic.whl"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("groove_serpent/__init__.py", "__version__ = '1.1.0'\n")

    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, str(script)]
    missing_workspace = subprocess.run(
        command, cwd=workspace, env=env, capture_output=True, text=True, timeout=30,
    )
    assert missing_workspace.returncode != 0
    assert "No module named 'scripts'" in missing_workspace.stderr

    env["PYTHONPATH"] = str(ROOT)
    clean = subprocess.run(
        command, cwd=workspace, env=env, capture_output=True, text=True, timeout=30,
    )
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert "Audited 1 distributions without private-content matches." in clean.stdout

    with zipfile.ZipFile(archive, "a") as package:
        private = json.dumps({"path": "\\".join(("X:", "Users", "synthetic", "file"))})
        package.writestr("synthetic.json", private)
    rejected = subprocess.run(
        command, cwd=workspace, env=env, capture_output=True, text=True, timeout=30,
    )
    assert rejected.returncode != 0
    assert "private material" in rejected.stderr


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
    wheel_name = "groove_serpent-1.1.0-py3-none-any.whl"
    sdist_name = "groove_serpent-1.1.0.tar.gz"
    payloads = {wheel_name: b"wheel", sdist_name: b"sdist"}
    for name, payload in payloads.items():
        (dist / name).write_bytes(payload)
    receipt = {
        "schema": "groove-serpent/python-distribution-build-receipt/1",
        "result": "passed",
        "project": {"name": "groove-serpent", "version": "1.1.0"},
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

    assert "placeholder: 1.1.0" in text
    assert "placeholder: 0.5.0a1" not in text


def test_quality_gate_syntax_checks_every_browser_spec() -> None:
    text = (ROOT / "scripts" / "check_quality.py").read_text(encoding="utf-8")

    assert '"tests/browser/album-workbench.spec.mjs"' in text
    assert '"tests/browser/fixture-crash-probe.mjs"' in text
    assert '"tests/browser/fixture-process.mjs"' in text
    assert '"tests/browser/fixture-process.test.mjs"' in text
    assert "startup-audio-monitor" not in text
    assert '"tests/browser/side-review-accessibility.spec.mjs"' in text
