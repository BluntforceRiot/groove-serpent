from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import stat
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN = ROOT / "toolchains" / "windows-media"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SMOKE = _load_module("windows_media_capability_smoke", TOOLCHAIN / "capability_smoke.py")
VERIFY = _load_module("windows_media_verify_artifact", TOOLCHAIN / "verify_artifact.py")
LAUNCHER = _load_module("windows_media_build_launcher", TOOLCHAIN / "build.py")


def test_recipe_pins_authenticated_minimal_lgpl_profile() -> None:
    launcher = (TOOLCHAIN / "build.py").read_text(encoding="utf-8")
    build = (TOOLCHAIN / "build.sh").read_text(encoding="utf-8")
    readme = (TOOLCHAIN / "README.md").read_text(encoding="utf-8")
    artifact_verifier = (TOOLCHAIN / "verify_artifact.py").read_text(encoding="utf-8")

    for digest in (
        "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c",
        "9d33482e56a1389a37a0d6742c376139fa43e3b8a63d29003222b93db2cb40da",
        "b111c15fdc8c029989330ff559184198c161100a59312f5dc19ddeb9b5a15889",
        "d7a0654783a4da529d1bb793b7ad9c3318020af77667bcae35f95d0e42a792f3",
    ):
        assert digest in build

    assert "FCF986EA15E6E293A5644F10B4322F04D67658D8" in build
    assert "5ED46A6721D365587791E2AA783FCD8E58BCAFBA" in build
    assert '"--disable-static"' in build
    assert '"--enable-shared"' in build
    assert '"--disable-network"' in build
    assert "--enable-small" not in build
    assert "-DFFT_LIB=kissfft" in build
    assert "-DZLIB_BUILD_SHARED=OFF" in build
    assert "--enable-gpl" not in build
    assert "--enable-nonfree" not in build
    assert "--enable-version3" not in build
    assert "fftw" not in build.casefold()
    assert "Independent JPEG Group" in " ".join(build.split())
    assert "not legal advice" in readme
    assert build.startswith("#!/usr/bin/bash -p\n")
    assert "os.execve(" in launcher
    assert '"--noprofile", "--norc", "-p"' in launcher
    assert "sys.flags.isolated != 1" in launcher
    assert "sys.flags.dont_write_bytecode != 1" in launcher
    assert "/usr/bin/python3.12 -I -B ./build.py" in readme
    assert "/usr/bin/python3.12 -I -B ./verify_artifact.py" in readme
    assert re.search(r"/usr/bin/python3\.12 -I(?! -B)", build) is None
    assert "Invoking the internal recipe explicitly with Bash is unsupported" in readme
    assert "treat `--execute-smoke` as code execution" in " ".join(readme.split())
    assert "independently trusted source" in readme
    assert 'deterministic_zip "$RUNTIME_STAGE" "$STAGED_RUNTIME_ARCHIVE"' in build
    assert 'deterministic_zip "$SOURCE_STAGE" "$STAGED_SOURCE_ARCHIVE"' in build
    assert "--publish-directory-no-replace" in build
    assert '"$PUBLISH_STAGE" "$DIST_DIR" \\' in build
    assert '"$STAGED_RUNTIME_SHA256" "$STAGED_SOURCE_SHA256"' in build
    assert "/usr/bin/printf '%s  %s\\n%s  %s\\n'" in build
    assert "/usr/bin/sha256sum --check --strict --status SHA256SUMS" in build
    assert 'sha256sum "$RUNTIME_ARCHIVE_NAME" "$SOURCE_ARCHIVE_NAME" > SHA256SUMS' not in build
    assert "/usr/bin/chmod 0644 \\" in build
    assert '/usr/bin/chmod 0755 "$PUBLISH_STAGE"' in build
    assert "/usr/bin/flock --exclusive --nonblock 9" in build
    assert "clean_work_root_contents" in build
    assert "export LANG=C" in build
    assert "export LC_ALL=C" in build
    assert "export TZ=UTC" in build
    assert "export ZIPOPT=" in build
    assert "umask 0022" in build
    assert "/usr/bin/env -i" in build.split("SCRIPT_DIR=", 1)[0]
    assert "/usr/bin/bash --noprofile --norc" in build.split("SCRIPT_DIR=", 1)[0]
    assert "set(os.environ) <= allowed" in build.split("SCRIPT_DIR=", 1)[0]
    assert "PYTHONPATH" in build.split("SCRIPT_DIR=", 1)[0]
    assert "GNUMAKEFLAGS" in build.split("SCRIPT_DIR=", 1)[0]
    assert "CFLAGS" in build.split("SCRIPT_DIR=", 1)[0]
    assert "CXXFLAGS" in build.split("SCRIPT_DIR=", 1)[0]
    assert "MAKEFLAGS" in build.split("SCRIPT_DIR=", 1)[0]
    assert "PKG_CONFIG_PATH" in build.split("SCRIPT_DIR=", 1)[0]
    assert "normalize_staged_modes" in build
    assert "--create-recipe-snapshot" in build
    assert "--verify-recipe-snapshot" in build
    assert "GROOVE_SERPENT_LAUNCH_AUTHORITY_SHA256" in launcher
    assert "GROOVE_SERPENT_LAUNCH_AUTHORITY_SHA256" in build
    assert "_trusted_provider(PYTHON)" in launcher
    assert "_trusted_provider(BASH)" in launcher
    assert set(LAUNCHER.TOOLCHAIN_AUTHORITY_FILES) == VERIFY.TOOLCHAIN_AUTHORITY_FILES
    assert tuple(sorted(LAUNCHER.TOOLCHAIN_AUTHORITY_FILES)) == tuple(
        sorted(VERIFY.TOOLCHAIN_AUTHORITY_FILES)
    )
    build_flow = build.split("readonly DIST_DIR", 1)[1]
    before_snapshot = build_flow.split("--create-recipe-snapshot", 1)[0]
    after_snapshot = build_flow.split("readonly RECIPE_AUTHORITY", 1)[1]
    assert "verify_build_host.sh" not in before_snapshot
    assert '"$SCRIPT_DIR/' not in after_snapshot
    assert "--verify-signatures" in build
    assert "--execute-smoke" in build
    assert '--verify-build-layout "$DIST_DIR" "$WORK_ROOT"' in build
    assert "renameat2(RENAME_NOREPLACE)" in readme
    assert 'deterministic_zip "$RUNTIME_STAGE" "$DIST_DIR/' not in build
    assert "shutil.which" not in artifact_verifier
    assert 'GPG_PROVIDER = Path("/usr/bin/gpg")' in artifact_verifier
    cleanup = build.split("cleanup_build() {", 1)[1].split("\n}", 1)[0]
    assert "status=0" not in cleanup


def test_bootstrap_installs_only_exact_build_host_packages() -> None:
    inventory = (TOOLCHAIN / "ubuntu-24.04-packages.txt").read_text(encoding="utf-8")
    bootstrap = (TOOLCHAIN / "bootstrap-ubuntu-24.04.sh").read_text(encoding="utf-8")
    verify = (TOOLCHAIN / "verify_build_host.sh").read_text(encoding="utf-8")
    readme = (TOOLCHAIN / "README.md").read_text(encoding="utf-8")
    assert bootstrap.startswith("#!/usr/bin/bash -p\n")
    assert "/usr/bin/bash --noprofile --norc -p ./bootstrap-ubuntu-24.04.sh" in readme
    for variable in (
        "APT_CONFIG",
        "BASH_ENV",
        "DEBCONF_DB_OVERRIDE",
        "DPKG_ROOT",
        "HTTPS_PROXY",
        "LD_PRELOAD",
    ):
        assert variable in bootstrap.split("SCRIPT_DIR=", 1)[0]
    entries = [line for line in inventory.splitlines() if line]
    assert entries == sorted(entries)
    assert all(re.fullmatch(r"[a-z0-9+.-]+=[^=\s]+", line) for line in entries)
    package_names = {line.split("=", 1)[0] for line in entries}
    assert {
        "apt",
        "bash",
        "ca-certificates",
        "coreutils",
        "curl",
        "dpkg",
        "findutils",
        "gawk",
        "gnupg",
        "gpg",
        "grep",
        "pkgconf-bin",
        "python3",
        "python3.12-minimal",
        "sed",
        "sudo",
        "tar",
        "util-linux",
        "xz-utils",
        "zip",
    } <= package_names
    assert re.search(
        r"/usr/bin/apt-get install\s+\\\n\s+-y --no-install-recommends\s+\\\n"
        r'\s+"\$\{pinned_packages\[@\]\}"',
        bootstrap,
    )
    assert bootstrap.index("bootstrap_contract=(") < bootstrap.index(
        "/usr/bin/sudo /usr/bin/apt-get update"
    )
    for recipe in (bootstrap, verify):
        lines = recipe.splitlines()
        assert lines[2] == "export PATH=/usr/bin:/bin"
        assert lines.index("export PATH=/usr/bin:/bin") < next(
            index for index, line in enumerate(lines) if "SCRIPT_DIR=" in line
        )
    build = (TOOLCHAIN / "build.sh").read_text(encoding="utf-8")
    assert "export PATH=/usr/bin:/bin" in build.split("SCRIPT_DIR=", 1)[0]
    assert "provider_contract=(" in verify
    assert 'dpkg-query -S "$resolved"' in verify
    assert "root-owned, non-writable provider" in verify
    for provider in (
        '"awk|/usr/bin/gawk|gawk"',
        '"chmod|/usr/bin/chmod|coreutils"',
        '"env|/usr/bin/env|coreutils"',
        '"flock|/usr/bin/flock|util-linux"',
        '"pkg-config|/usr/bin/pkgconf|pkgconf-bin"',
        '"python3|/usr/bin/python3.12|python3.12-minimal"',
        '"sudo|/usr/bin/sudo|sudo"',
    ):
        assert provider in verify
    for command in (
        "awk",
        "bash",
        "curl",
        "find",
        "flock",
        "gpg",
        "grep",
        "python3",
        "sed",
        "sha256sum",
        "sort",
        "tar",
        "xargs",
        "xz",
        "zip",
    ):
        assert re.search(rf"\b{re.escape(command)}\b", verify)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux build lock")
def test_kernel_work_root_lock_excludes_a_second_builder(tmp_path: Path) -> None:
    fcntl = importlib.import_module("fcntl")

    work_root = tmp_path / "work-root"
    work_root.mkdir(mode=0o700)
    descriptor = os.open(work_root, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked = subprocess.run(
            [
                "/usr/bin/flock",
                "--exclusive",
                "--nonblock",
                str(work_root),
                "/usr/bin/true",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert blocked.returncode != 0
    finally:
        os.close(descriptor)
    released = subprocess.run(
        [
            "/usr/bin/flock",
            "--exclusive",
            "--nonblock",
            str(work_root),
            "/usr/bin/true",
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert released.returncode == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux recipe environment")
def test_hostile_build_environment_is_normalized_before_artifact_creation(
    tmp_path: Path,
) -> None:
    build = (TOOLCHAIN / "build.sh").read_text(encoding="utf-8")
    header = build.split("SCRIPT_DIR=", 1)[0]
    script = (
        header
        + r"""
PROBE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
mkdir -p "$PROBE_ROOT/source/nested"
printf 'upper\n' > "$PROBE_ROOT/source/UPPER.txt"
printf 'lower\n' > "$PROBE_ROOT/source/lower.txt"
printf 'nested\n' > "$PROBE_ROOT/source/nested/payload.txt"
(
  cd "$PROBE_ROOT/source"
  find . -type f -printf '%P\n' | sort | zip -X -q "$PROBE_ROOT/result.zip" -@
)
printf 'umask=%s\n' "$(umask)"
printf 'locale=%s/%s\n' "$LANG" "$LC_ALL"
printf 'zipopt=<%s>\n' "$ZIPOPT"
printf 'flags=%s/%s/%s/%s\n' "${CFLAGS-unset}" "${CXXFLAGS-unset}" \
  "${MAKEFLAGS-unset}" "${PKG_CONFIG_PATH-unset}"
printf 'wsl-distro=%s\n' "${WSL_DISTRO_NAME-unset}"
stat -c 'mode=%a' "$PROBE_ROOT/source/UPPER.txt"
{
  /usr/bin/printf 'all:\n'
  /usr/bin/printf '\t@/usr/bin/printf make-ran > "%s"\n' "$PROBE_ROOT/make-ran"
} > "$PROBE_ROOT/Makefile"
/usr/bin/make -f "$PROBE_ROOT/Makefile"
/usr/bin/python3.12 -c 'print("python=clean")'
printf 'shell-printf=clean\n'
"""
    )
    probe = tmp_path / "environment-probe.sh"
    probe.write_text(script, encoding="utf-8", newline="\n")
    probe.chmod(0o755)
    poison = tmp_path / "python-poison"
    poison.mkdir()
    poison_marker = tmp_path / "python-poison-ran"
    (poison / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(poison_marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment.update(
        {
            "LANG": "hostile_LOCALE",
            "LC_ALL": "hostile_LOCALE",
            "ZIPOPT": "-j",
            "CFLAGS": "-DHOSTILE_CFLAGS",
            "CXXFLAGS": "-DHOSTILE_CXXFLAGS",
            "MAKEFLAGS": "--eval=hostile",
            "GNUMAKEFLAGS": "-n",
            "GROOVE_SERPENT_WINDOWS_MEDIA_CLEAN_ENV": "1",
            "PKG_CONFIG_PATH": "/hostile/pkgconfig",
            "PYTHONPATH": str(poison),
            "WSL_DISTRO_NAME": "qa-distro",
            "BASH_FUNC_printf%%": "() { /usr/bin/printf 'poisoned\\n'; }",
        }
    )
    previous_umask = os.umask(0o077)
    try:
        completed = subprocess.run(
            ["/usr/bin/bash", str(probe)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            encoding="utf-8",
        )
    finally:
        os.umask(previous_umask)

    assert "umask=0022" in completed.stdout
    assert "locale=C/C" in completed.stdout
    assert "zipopt=<>" in completed.stdout
    assert "flags=unset/unset/unset/unset" in completed.stdout
    assert "wsl-distro=qa-distro" in completed.stdout
    assert "mode=644" in completed.stdout
    assert "python=clean" in completed.stdout
    assert "shell-printf=clean" in completed.stdout
    assert "poisoned" not in completed.stdout
    assert (tmp_path / "make-ran").read_text(encoding="utf-8") == "make-ran"
    assert not poison_marker.exists()
    with zipfile.ZipFile(tmp_path / "result.zip") as archive:
        assert archive.namelist() == [
            "UPPER.txt",
            "lower.txt",
            "nested/payload.txt",
        ]
        assert all(stat.S_IMODE(info.external_attr >> 16) == 0o644 for info in archive.infolist())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux launcher boundary")
def test_isolated_launcher_sanitizes_before_bash_startup(tmp_path: Path) -> None:
    probe = tmp_path / "launcher-probe"
    probe.mkdir()
    launcher = probe / "build.py"
    launcher.write_bytes((TOOLCHAIN / "build.py").read_bytes())
    recipe = probe / "build.sh"
    recipe.write_text(
        r"""#!/usr/bin/bash -p
set -euo pipefail
[[ "${GROOVE_SERPENT_WINDOWS_MEDIA_CLEAN_ENV:-}" == "1" ]]
[[ ":$SHELLOPTS:" != *:noexec:* ]]
[[ -z "${BASH_ENV+x}" ]]
[[ -z "${PYTHONPATH+x}" ]]
[[ -z "${GNUMAKEFLAGS+x}" ]]
if declare -F printf >/dev/null; then
  exit 91
fi
/usr/bin/printf 'launcher-ran\n' > "${BASH_SOURCE[0]%/*}/launcher-ran"
""",
        encoding="utf-8",
        newline="\n",
    )
    for name in VERIFY.TOOLCHAIN_AUTHORITY_FILES - {"build.py", "build.sh"}:
        target = probe / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((TOOLCHAIN / name).read_bytes())
    poison = probe / "bash-env-poison"
    poison.write_text(
        f"/usr/bin/printf poison > {str(probe / 'bash-env-ran')!r}\n",
        encoding="utf-8",
        newline="\n",
    )
    python_poison = probe / "python-poison"
    python_poison.mkdir()
    (python_poison / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(probe / 'python-env-ran')!r}).write_text('x')\n",
        encoding="utf-8",
        newline="\n",
    )
    environment = dict(os.environ)
    environment.update(
        {
            "BASH_ENV": str(poison),
            "BASH_FUNC_printf%%": "() { /usr/bin/printf 'poisoned\\n'; }",
            "DIST_DIR": "/tmp/groove-serpent-launcher-probe-output",
            "GNUMAKEFLAGS": "-n",
            "GROOVE_SERPENT_WINDOWS_MEDIA_CLEAN_ENV": "1",
            "HOME": "/hostile/home",
            "JOBS": "2",
            "PYTHONPATH": str(python_poison),
            "SHELLOPTS": "noexec",
            "WSL_DISTRO_NAME": "neuroforge",
        }
    )

    completed = subprocess.run(
        ["/usr/bin/python3.12", "-I", "-B", str(launcher)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    assert (probe / "launcher-ran").read_text(encoding="utf-8") == "launcher-ran\n"
    assert not (probe / "bash-env-ran").exists()
    assert not (probe / "python-env-ran").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux launcher boundary")
@pytest.mark.parametrize("interpreter_flags", [[], ["-I"]])
def test_launcher_refuses_missing_isolation_or_no_bytecode_before_bash(
    tmp_path: Path,
    interpreter_flags: list[str],
) -> None:
    probe = tmp_path / "unisolated-launcher"
    probe.mkdir()
    launcher = probe / "build.py"
    launcher.write_bytes((TOOLCHAIN / "build.py").read_bytes())
    (probe / "build.sh").write_text(
        '#!/usr/bin/bash -p\n/usr/bin/printf ran > "${BASH_SOURCE[0]%/*}/ran"\n',
        encoding="utf-8",
        newline="\n",
    )
    environment = {
        "DIST_DIR": "/tmp/groove-serpent-unisolated-probe-output",
        "JOBS": "2",
        "WSL_DISTRO_NAME": "neuroforge",
    }

    completed = subprocess.run(
        ["/usr/bin/python3.12", *interpreter_flags, str(launcher)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 1
    assert (
        "invoke this launcher with the exact isolated, no-bytecode interpreter options"
        in completed.stderr
    )
    assert not (probe / "ran").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="privileged Bash boundary")
def test_bootstrap_supported_bash_boundary_ignores_prestartup_poison(
    tmp_path: Path,
) -> None:
    poison = tmp_path / "bash-env"
    poisoned_marker = tmp_path / "bash-env-ran"
    poison.write_text(
        '/usr/bin/printf poisoned > "${POISONED_MARKER}"\n',
        encoding="utf-8",
        newline="\n",
    )
    probe = tmp_path / "bootstrap-boundary-probe.sh"
    ran = tmp_path / "ran"
    probe.write_text(
        '#!/usr/bin/bash -p\n/usr/bin/printf ran > "${PROBE_RAN}"\n',
        encoding="utf-8",
        newline="\n",
    )
    environment = dict(os.environ)
    environment.update(
        {
            "BASH_ENV": str(poison),
            "BASH_FUNC_printf%%": "() { /usr/bin/false; }",
            "POISONED_MARKER": str(poisoned_marker),
            "PROBE_RAN": str(ran),
            "SHELLOPTS": "noexec",
        }
    )

    completed = subprocess.run(
        ["/usr/bin/bash", "--noprofile", "--norc", "-p", str(probe)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    assert ran.read_text(encoding="utf-8") == "ran"
    assert not poisoned_marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="WSL UNC conversion runs on the Linux host")
def test_capability_smoke_constructs_wsl_unc_paths_without_wslpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "path with spaces" / "capture.flac"
    monkeypatch.setenv("WSL_DISTRO_NAME", "neuroforge")

    expected = "\\\\wsl.localhost\\neuroforge\\" + source.resolve().as_posix().removeprefix(
        "/"
    ).replace("/", "\\")
    assert SMOKE._tool_path(source) == expected
    assert "wslpath" not in (TOOLCHAIN / "capability_smoke.py").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="WSL UNC conversion runs on the Linux host")
def test_capability_smoke_rejects_unsafe_wsl_share_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WSL_DISTRO_NAME", "unsafe\\share")
    with pytest.raises(SMOKE.SmokeFailure, match="safe UNC share"):
        SMOKE._tool_path(tmp_path / "capture.flac")


@pytest.mark.parametrize(
    "spelling",
    (
        "same",
        "direct-child",
        "normalized-child",
    ),
)
def test_build_output_must_be_disjoint_from_work_root(
    tmp_path: Path,
    spelling: str,
) -> None:
    work_root = tmp_path / "deterministic-work"
    candidates = {
        "same": work_root,
        "direct-child": work_root / "published",
        "normalized-child": work_root / "intermediate" / ".." / "published",
    }

    with pytest.raises(VERIFY.PublicationFailure, match="disjoint"):
        VERIFY._assert_build_paths_disjoint(candidates[spelling], work_root)

    VERIFY._assert_build_paths_disjoint(tmp_path / "published", work_root)


def test_artifact_verifier_extracts_the_bound_archives_when_inputs_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = tmp_path / "runtime.zip"
    source = tmp_path / "source.zip"
    runtime_a = b"validated-runtime-a"
    runtime_b = b"replacement-runtime-b"
    source_a = b"validated-source-a"
    runtime.write_bytes(runtime_a)
    source.write_bytes(source_a)
    extracted: list[bytes] = []
    bound_paths: list[Path] = []

    def fake_extract(archive: Path, _destination: Path) -> list[str]:
        if not extracted:
            runtime.write_bytes(runtime_b)
        bound_paths.append(archive)
        extracted.append(archive.read_bytes())
        return []

    def fake_verify_runtime(_root: Path) -> dict[str, object]:
        return {"payload_files": 1}

    def fake_verify_source(_root: Path) -> dict[str, object]:
        return {"payload_files": 1, "pinned_inputs": len(VERIFY.EXPECTED_INPUTS)}

    monkeypatch.setattr(VERIFY, "_extract_checked", fake_extract)
    monkeypatch.setattr(VERIFY, "_verify_runtime", fake_verify_runtime)
    monkeypatch.setattr(VERIFY, "_verify_source", fake_verify_source)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_artifact.py",
            "--runtime-zip",
            str(runtime),
            "--source-zip",
            str(source),
            "--runtime-sha256",
            hashlib.sha256(runtime_a).hexdigest(),
            "--source-sha256",
            hashlib.sha256(source_a).hexdigest(),
        ],
    )

    assert VERIFY.main() == 0
    result = json.loads(capsys.readouterr().out)

    assert extracted == [runtime_a, source_a]
    assert runtime.read_bytes() == runtime_b
    assert all(path not in {runtime, source} for path in bound_paths)
    assert all(not path.exists() for path in bound_paths)
    assert result["runtime_archive"]["sha256"] == hashlib.sha256(runtime_a).hexdigest()
    assert result["source_archive"]["sha256"] == hashlib.sha256(source_a).hexdigest()


def test_synthetic_pcm_containers_have_exact_geometry(tmp_path: Path) -> None:
    wav = tmp_path / "capture.wav"
    aiff = tmp_path / "capture.aiff"
    SMOKE._write_wav(wav, sample_rate=44_100, frames=1_337, channels=2, bits=24)
    SMOKE._write_aiff(aiff, sample_rate=96_000, frames=733, channels=1, bits=16)

    wav_bytes = wav.read_bytes()
    assert wav_bytes[:12] == b"RIFF" + struct.pack("<I", len(wav_bytes) - 8) + b"WAVE"
    assert wav_bytes[22:24] == struct.pack("<H", 2)
    assert wav_bytes[24:28] == struct.pack("<I", 44_100)
    assert wav_bytes[34:36] == struct.pack("<H", 24)
    assert len(wav_bytes) == 44 + 1_337 * 2 * 3

    aiff_bytes = aiff.read_bytes()
    assert aiff_bytes[:12] == b"FORM" + struct.pack(">I", len(aiff_bytes) - 8) + b"AIFF"
    assert b"COMM" in aiff_bytes
    assert b"SSND" in aiff_bytes
    assert len(aiff_bytes) % 2 == 0


def test_smoke_work_directory_safety_rejects_unscoped_paths(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "ffmpeg.exe").touch()
    (runtime / "ffprobe.exe").touch()
    with pytest.raises(SMOKE.SmokeFailure, match="unsafe name"):
        SMOKE.run_smoke(runtime, tmp_path / "work")


@pytest.mark.parametrize(
    "members",
    [
        {"../escape.txt": b"x"},
        {"A.txt": b"a", "a.TXT": b"b"},
        {"empty/": b""},
    ],
)
def test_archive_verifier_rejects_unsafe_members(tmp_path: Path, members: dict[str, bytes]) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    with zipfile.ZipFile(archive_path) as archive:
        with pytest.raises(VERIFY.VerificationFailure):
            VERIFY._safe_members(archive)


@pytest.mark.parametrize(
    "text",
    ['{"schema": 1, "schema": 2}', '{"value": NaN}', "[]"],
)
def test_archive_verifier_uses_strict_json(tmp_path: Path, text: str) -> None:
    path = tmp_path / "evidence.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(VERIFY.VerificationFailure):
        VERIFY._load_json(path)


def test_source_inventory_has_exact_required_pins() -> None:
    assert VERIFY.EXPECTED_INPUTS == {
        "inputs/chromaprint-1.6.0.tar.gz": (
            "9d33482e56a1389a37a0d6742c376139fa43e3b8a63d29003222b93db2cb40da"
        ),
        "inputs/ffmpeg-8.1.2.tar.xz": (
            "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
        ),
        "inputs/ffmpeg-8.1.2.tar.xz.asc": (
            "0a0963fccd70597838073f3e31b20f4a4d8cc2b5e577472c9a5a1f22624246f8"
        ),
        "inputs/soxr-0.1.3-Source.tar.xz": (
            "b111c15fdc8c029989330ff559184198c161100a59312f5dc19ddeb9b5a15889"
        ),
        "inputs/zlib-1.3.2.tar.xz": (
            "d7a0654783a4da529d1bb793b7ad9c3318020af77667bcae35f95d0e42a792f3"
        ),
        "inputs/zlib-1.3.2.tar.xz.asc": (
            "03ce710347e2f84fa7ed0a6ae6a93467b08031a3022fc296da40220a83b96667"
        ),
        "recipe/keys/ffmpeg-release-signing-key.asc": (
            "397b3becedcd5a98769967ff1ff8501ddc89f8368b8f766e4701377d7dbaabe5"
        ),
        "recipe/keys/zlib-mark-adler.asc": (
            "27f818fd93326e4531c6b094f0edc4c331a1c77ec6449675a3929ae3274d85ac"
        ),
    }


def test_source_verifier_rejects_receipt_bound_extra_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sums = dict(VERIFY.EXPECTED_INPUTS)
    sums.update({name: "0" * 64 for name in VERIFY.REQUIRED_RECIPE})
    sums["recipe/untracked-helper.py"] = "1" * 64
    monkeypatch.setattr(VERIFY, "_verify_sums", lambda _root: sums)

    with pytest.raises(VERIFY.VerificationFailure, match="inventory is not exact"):
        VERIFY._verify_source(tmp_path)


def test_runtime_verifier_rejects_coherent_extra_text_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sums = {name: "0" * 64 for name in VERIFY.EXPECTED_RUNTIME_FILES}
    sums["UNTRACKED-NOTE.txt"] = "1" * 64
    monkeypatch.setattr(VERIFY, "_verify_sums", lambda _root: sums)

    with pytest.raises(VERIFY.VerificationFailure, match="inventory is not exact"):
        VERIFY._verify_runtime(tmp_path)


def _write_recipe_authority_fixture(root: Path) -> str:
    for name in VERIFY.TOOLCHAIN_AUTHORITY_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"authority:{name}\n".encode())
    digests = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in VERIFY.TOOLCHAIN_AUTHORITY_FILES
    }
    return VERIFY._content_authority_sha256(digests)


def test_launcher_and_snapshot_creator_share_the_actual_content_authority() -> None:
    recipe, launcher_authority = LAUNCHER._plain_toolchain_authority()
    observed = VERIFY._recipe_authority(TOOLCHAIN, exact_inventory=False)
    digests = {str(item["path"]): str(item["sha256"]) for item in observed["files"]}

    assert recipe == TOOLCHAIN / "build.sh"
    assert launcher_authority == VERIFY._content_authority_sha256(digests)


def test_private_recipe_snapshot_binds_identity_and_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    authority_sha256 = _write_recipe_authority_fixture(source)
    snapshot = tmp_path / "snapshot"

    authority = VERIFY._create_recipe_snapshot(source, snapshot, authority_sha256)
    VERIFY._verify_recipe_snapshot(snapshot, authority)

    target = snapshot / "capability_smoke.py"
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b"mutation\n")
    with pytest.raises(VERIFY.PublicationFailure, match="changed after authority binding"):
        VERIFY._verify_recipe_snapshot(snapshot, authority)


def test_private_recipe_snapshot_rejects_launcher_authority_digest_mismatch(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _write_recipe_authority_fixture(source)

    with pytest.raises(VERIFY.PublicationFailure, match="launcher-bound authority digest"):
        VERIFY._create_recipe_snapshot(source, tmp_path / "snapshot", "0" * 64)


def test_private_recipe_snapshot_rejects_concurrent_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    authority_sha256 = _write_recipe_authority_fixture(source)
    real_write = VERIFY._write_exclusive_snapshot_file
    writes = 0

    def mutate_during_copy(path: Path, content: bytes) -> None:
        nonlocal writes
        real_write(path, content)
        writes += 1
        if writes == 1:
            target = source / "verify_build_host.sh"
            target.write_bytes(target.read_bytes() + b"concurrent mutation\n")

    monkeypatch.setattr(VERIFY, "_write_exclusive_snapshot_file", mutate_during_copy)

    with pytest.raises(VERIFY.PublicationFailure, match="changed before snapshot copy"):
        VERIFY._create_recipe_snapshot(
            source,
            tmp_path / "snapshot",
            authority_sha256,
        )


def test_smoke_report_serialization_is_canonical() -> None:
    payload = {"z": [2, 1], "a": {"result": "passed"}}
    first = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    second = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    assert first == second


def _write_publication_stage(
    stage: Path,
    *,
    runtime: bytes = b"runtime-archive",
    source: bytes = b"source-archive",
) -> None:
    stage.mkdir(exist_ok=True)
    runtime_name = VERIFY.RUNTIME_ARCHIVE_NAME
    source_name = VERIFY.SOURCE_ARCHIVE_NAME
    (stage / runtime_name).write_bytes(runtime)
    (stage / source_name).write_bytes(source)
    receipt = (
        f"{hashlib.sha256(runtime).hexdigest()}  {runtime_name}\n"
        f"{hashlib.sha256(source).hexdigest()}  {source_name}\n"
    )
    (stage / "SHA256SUMS").write_text(receipt, encoding="ascii", newline="\n")
    stage.chmod(0o755)
    for name in VERIFY.PUBLICATION_FILENAMES:
        (stage / name).chmod(0o644)


def test_publication_rejects_post_verification_digest_laundering(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stage = tmp_path / "stage"
    published = tmp_path / "published"
    original_runtime = b"verified-runtime"
    source = b"verified-source"
    _write_publication_stage(stage, runtime=original_runtime, source=source)
    verified_runtime_sha256 = hashlib.sha256(original_runtime).hexdigest()
    verified_source_sha256 = hashlib.sha256(source).hexdigest()

    mutated_runtime = b"mutated-after-verification"
    (stage / VERIFY.RUNTIME_ARCHIVE_NAME).write_bytes(mutated_runtime)
    laundering_receipt = (
        f"{hashlib.sha256(mutated_runtime).hexdigest()}  {VERIFY.RUNTIME_ARCHIVE_NAME}\n"
        f"{verified_source_sha256}  {VERIFY.SOURCE_ARCHIVE_NAME}\n"
    )
    (stage / "SHA256SUMS").write_text(
        laundering_receipt,
        encoding="ascii",
        newline="\n",
    )

    result = VERIFY._publish_directory_cli(
        [
            str(stage),
            str(published),
            verified_runtime_sha256,
            verified_source_sha256,
        ]
    )

    assert result == 1
    assert "no longer matches its verified digest" in capsys.readouterr().err
    assert stage.is_dir()
    assert not published.exists()


def test_directory_publication_is_one_atomic_no_replace_commit(tmp_path: Path) -> None:
    if sys.platform == "darwin":
        pytest.skip("Atomic no-replace directory publication is exercised on Linux and Windows.")
    stage = tmp_path / "stage"
    published = tmp_path / "published"
    _write_publication_stage(stage)
    staged_identity = stage.stat().st_ino

    VERIFY._publish_directory_no_replace(stage, published)

    assert not stage.exists()
    assert published.stat().st_ino == staged_identity
    VERIFY._snapshot_publication_directory(published)


def test_directory_publication_never_replaces_concurrent_winner(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    published = tmp_path / "published"
    _write_publication_stage(stage)
    published.mkdir()
    (published / "winner.txt").write_bytes(b"concurrent-winner")

    with pytest.raises(FileExistsError):
        VERIFY._publish_directory_no_replace(stage, published)

    assert stage.is_dir()
    assert (published / "winner.txt").read_bytes() == b"concurrent-winner"


def test_directory_publication_loses_rename_race_without_touching_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "darwin":
        pytest.skip("Atomic no-replace directory publication is exercised on Linux and Windows.")
    stage = tmp_path / "stage"
    published = tmp_path / "published"
    _write_publication_stage(stage)
    real_rename = VERIFY._rename_directory_no_replace

    def racing_rename(staged: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "winner.txt").write_bytes(b"racing-winner")
        real_rename(staged, destination)

    monkeypatch.setattr(VERIFY, "_rename_directory_no_replace", racing_rename)
    with pytest.raises(FileExistsError):
        VERIFY._publish_directory_no_replace(stage, published)

    assert stage.is_dir()
    assert (published / "winner.txt").read_bytes() == b"racing-winner"


def test_directory_publication_treats_interrupt_after_commit_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "darwin":
        pytest.skip("Atomic no-replace directory publication is exercised on Linux and Windows.")
    stage = tmp_path / "stage"
    published = tmp_path / "published"
    _write_publication_stage(stage)
    real_rename = VERIFY._rename_directory_no_replace

    def rename_then_interrupt(staged: Path, destination: Path) -> None:
        real_rename(staged, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        VERIFY,
        "_rename_directory_no_replace",
        rename_then_interrupt,
    )
    VERIFY._publish_directory_no_replace(stage, published)

    assert not stage.exists()
    VERIFY._snapshot_publication_directory(published)


def test_directory_publication_detects_post_snapshot_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "darwin":
        pytest.skip("Stable publication identity is exercised on Linux and Windows.")
    stage = tmp_path / "stage"
    published = tmp_path / "published"
    _write_publication_stage(stage)
    real_rename = VERIFY._rename_directory_no_replace

    def mutate_then_rename(staged: Path, destination: Path) -> None:
        runtime = staged / VERIFY.RUNTIME_ARCHIVE_NAME
        runtime.write_bytes(b"changed-archive")
        real_rename(staged, destination)

    monkeypatch.setattr(
        VERIFY,
        "_rename_directory_no_replace",
        mutate_then_rename,
    )
    with pytest.raises(VERIFY.PublicationFailure):
        VERIFY._publish_directory_no_replace(stage, published)

    assert not stage.exists()
    assert (published / VERIFY.RUNTIME_ARCHIVE_NAME).read_bytes() == b"changed-archive"


def test_directory_publication_rejects_coherent_post_commit_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform == "darwin":
        pytest.skip("Stable publication identity is exercised on Linux and Windows.")
    stage = tmp_path / "stage"
    published = tmp_path / "published"
    _write_publication_stage(stage, runtime=b"runtime-a", source=b"source-a")
    real_rename = VERIFY._rename_directory_no_replace

    def rename_then_substitute(staged: Path, destination: Path) -> None:
        real_rename(staged, destination)
        _write_publication_stage(
            destination,
            runtime=b"self-consistent-runtime-b",
            source=b"self-consistent-source-b",
        )

    monkeypatch.setattr(
        VERIFY,
        "_rename_directory_no_replace",
        rename_then_substitute,
    )
    with pytest.raises(VERIFY.PublicationFailure, match="staged snapshot"):
        VERIFY._publish_directory_no_replace(stage, published)

    assert VERIFY._verify_publication_directory_cli([str(published)]) == 0
    assert (published / VERIFY.RUNTIME_ARCHIVE_NAME).read_bytes().endswith(b"-b")


@pytest.mark.parametrize("defect", ["extra", "receipt"])
def test_directory_publication_rejects_invalid_staged_set(
    tmp_path: Path,
    defect: str,
) -> None:
    stage = tmp_path / "stage"
    published = tmp_path / "published"
    _write_publication_stage(stage)
    if defect == "extra":
        (stage / "unexpected.txt").write_bytes(b"unexpected")
    else:
        (stage / "SHA256SUMS").write_bytes(b"0" * 64 + b"  wrong.zip\n")

    with pytest.raises(VERIFY.PublicationFailure):
        VERIFY._publish_directory_no_replace(stage, published)

    assert stage.is_dir()
    assert not published.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication-mode contract")
def test_directory_publication_rejects_wrong_modes(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    published = tmp_path / "published"
    _write_publication_stage(stage)
    stage.chmod(0o700)
    for name in VERIFY.PUBLICATION_FILENAMES:
        (stage / name).chmod(0o600)

    with pytest.raises(VERIFY.PublicationFailure):
        VERIFY._publish_directory_no_replace(stage, published)

    assert stage.is_dir()
    assert not published.exists()


def test_publication_snapshot_rechecks_inventory_after_member_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    _write_publication_stage(stage)
    real_scandir = os.scandir
    calls = 0

    class InjectAfterScan:
        def __init__(self, path: os.PathLike[str] | str) -> None:
            self._entries = real_scandir(path)

        def __enter__(self) -> Any:
            return self._entries.__enter__()

        def __exit__(self, *args: object) -> None:
            nonlocal calls
            self._entries.__exit__(*args)
            calls += 1
            if calls == 1:
                (stage / "late-extra.txt").write_bytes(b"late")

    monkeypatch.setattr(VERIFY.os, "scandir", InjectAfterScan)
    with pytest.raises(VERIFY.PublicationFailure):
        VERIFY._snapshot_publication_directory(stage)

    assert (stage / "late-extra.txt").read_bytes() == b"late"


def test_publication_stage_creation_probes_real_destination_filesystem(
    tmp_path: Path,
) -> None:
    if sys.platform == "darwin":
        pytest.skip("Atomic no-replace directory publication is exercised on Linux and Windows.")
    published = tmp_path / "published"

    stage = VERIFY._create_publication_stage(published)

    assert stage.parent == tmp_path
    assert stage.name.endswith(".ready")
    if os.name != "nt":
        assert stat.S_IMODE(stage.stat().st_mode) == 0o700
    assert not published.exists()
    _write_publication_stage(stage, runtime=b"runtime", source=b"source")
    VERIFY._publish_directory_no_replace(stage, published)
    VERIFY._snapshot_publication_directory(published)


def test_publication_stage_cli_reports_retained_probe_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def unsupported(_staged: Path, _published: Path) -> None:
        raise VERIFY.PublicationFailure("filesystem primitive unavailable")

    monkeypatch.setattr(VERIFY, "_rename_directory_no_replace", unsupported)
    result = VERIFY._create_publication_stage_cli([str(tmp_path / "published")])

    assert result == 1
    diagnostic = capsys.readouterr().err
    assert "filesystem primitive unavailable" in diagnostic
    assert "Empty capability-probe stage retained at:" in diagnostic
    retained = tuple(tmp_path.glob(".groove-serpent-windows-media-stage-*"))
    assert len(retained) == 1
    retained[0].rmdir()


def test_signature_verifier_ignores_fake_gpg_on_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_gpg = fake_bin / ("gpg.exe" if os.name == "nt" else "gpg")
    fake_gpg.write_text("fake provider must never execute\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setattr(
        VERIFY,
        "_verified_gpg_provider",
        lambda: Path("/usr/bin/gpg"),
    )
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []
    fingerprint = "A" * 40

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        commands.append(command)
        environments.append(_kwargs["env"])
        if "--with-colons" in command:
            stdout: str | bytes = f"fpr:::::::::{fingerprint}:\n"
        elif "--verify" in command:
            stdout = f"[GNUPG:] VALIDSIG {fingerprint} trusted proof\n"
        else:
            stdout = b""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(VERIFY.subprocess, "run", fake_run)
    source = tmp_path / "source"
    source.mkdir()
    for name in ("archive.tar.xz", "archive.tar.xz.asc", "key.asc"):
        (source / name).write_bytes(b"fixture")

    VERIFY._gpg_validsig(
        source,
        archive="archive.tar.xz",
        signature="archive.tar.xz.asc",
        key="key.asc",
        fingerprint=fingerprint,
    )

    assert len(commands) == 3
    assert all(command[0] == "/usr/bin/gpg" for command in commands)
    assert all(str(fake_gpg) not in command for command in commands)
    assert (
        environments
        == [{"HOME": environments[0]["HOME"], "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}]
        * 3
    )


def test_signature_verifier_rejects_untrusted_provider_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_gpg = tmp_path / "gpg"
    fake_gpg.write_text("untrusted\n", encoding="utf-8")
    fake_gpg.chmod(0o777)
    monkeypatch.setattr(VERIFY, "GPG_PROVIDER", fake_gpg)
    with pytest.raises(VERIFY.VerificationFailure, match="root-owned"):
        VERIFY._verified_gpg_provider()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="The pinned provider is on the Linux build host.",
)
def test_signature_verifier_accepts_exact_root_owned_gpg_provider() -> None:
    assert VERIFY._verified_gpg_provider() == Path("/usr/bin/gpg")


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="The pinned provider is on the Linux build host.",
)
def test_signature_provider_discovery_ignores_fake_earlier_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_gpg = fake_bin / "gpg"
    fake_gpg.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gpg.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    assert VERIFY._verified_gpg_provider() == Path("/usr/bin/gpg")
