from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from groove_serpent.album_publication_executor import _directory_identity


def _load_acceptance_module() -> ModuleType:
    target = Path(__file__).resolve().parents[1] / "scripts" / "accept_n_drive_filesystem.py"
    spec = importlib.util.spec_from_file_location(
        "groove_serpent_n_drive_acceptance_under_test",
        target,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load acceptance module from {target}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


acceptance = _load_acceptance_module()


def test_acceptance_receipt_uses_authority_bound_schema_v2() -> None:
    assert acceptance.ACCEPTANCE_SCHEMA == ("groove-serpent.n-drive-filesystem-acceptance/2")


def test_promotion_flags_require_exact_isolated_modes() -> None:
    flags = SimpleNamespace(isolated=1, dont_write_bytecode=1, utf8_mode=1)

    assert acceptance._promotion_flags(flags) == {
        "isolated": 1,
        "dont_write_bytecode": 1,
        "utf8_mode": 1,
    }


@pytest.mark.parametrize(
    ("isolated", "dont_write_bytecode", "utf8_mode"),
    [(0, 1, 1), (1, 0, 1), (1, 1, 0)],
)
def test_promotion_flags_fail_closed_when_one_mode_is_missing(
    isolated: int,
    dont_write_bytecode: int,
    utf8_mode: int,
) -> None:
    flags = SimpleNamespace(
        isolated=isolated,
        dont_write_bytecode=dont_write_bytecode,
        utf8_mode=utf8_mode,
    )

    with pytest.raises(acceptance.AcceptanceError, match="-I -B -X utf8"):
        acceptance._promotion_flags(flags)


def test_frozen_environment_marker_is_exact() -> None:
    acceptance._require_frozen_environment({"UV_FROZEN": "1"})

    for environment in ({}, {"UV_FROZEN": "true"}, {"UV_FROZEN": "0"}):
        with pytest.raises(acceptance.AcceptanceError, match="UV_FROZEN=1"):
            acceptance._require_frozen_environment(environment)


def test_promotion_versions_require_exact_release_authority() -> None:
    assert acceptance._promotion_versions(
        python_version="3.13.14",
        app_version="1.0.0",
    ) == {"python": "3.13.14", "groove_serpent": "1.0.0"}


@pytest.mark.parametrize(
    ("python_version", "app_version", "message"),
    [
        ("3.13.13", "1.0.0", "requires Python 3.13.14"),
        ("3.13.14", "1.0.1", "requires Groove Serpent 1.0.0"),
    ],
)
def test_promotion_versions_reject_any_mismatch(
    python_version: str,
    app_version: str,
    message: str,
) -> None:
    with pytest.raises(acceptance.AcceptanceError, match=message):
        acceptance._promotion_versions(
            python_version=python_version,
            app_version=app_version,
        )


def test_isolated_child_command_uses_required_flags_and_interpreter() -> None:
    command = acceptance._isolated_child_command("pass", "argument")

    assert command[0] == acceptance.sys.executable
    assert command[1:6] == ["-I", "-B", "-X", "utf8", "-c"]
    assert command[6:] == ["pass", "argument"]


def test_child_environment_removes_python_contamination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in acceptance._PYTHON_CHILD_ENVIRONMENT_NAMES:
        monkeypatch.setenv(name, "contaminated")

    environment = acceptance._child_environment()

    expected_overrides = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    }
    for name in acceptance._PYTHON_CHILD_ENVIRONMENT_NAMES:
        if name in expected_overrides:
            assert environment[name] == expected_overrides[name]
        else:
            assert name not in environment


def test_uv_check_environment_removes_uv_contamination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "wrong-environment")
    monkeypatch.setenv("UV_INDEX_URL", "https://invalid.example")
    monkeypatch.setenv("UV_NO_SYNC", "1")

    environment = acceptance._uv_check_environment()

    assert "UV_PROJECT_ENVIRONMENT" not in environment
    assert "UV_INDEX_URL" not in environment
    assert "UV_NO_SYNC" not in environment
    assert "UV_FROZEN" not in environment
    assert "UV_LOCKED" not in environment
    assert environment["UV_OFFLINE"] == "1"
    assert environment["UV_PYTHON_DOWNLOADS"] == "never"


def test_installed_distribution_inventory_is_canonical() -> None:
    module_file = acceptance.__file__
    assert module_file is not None
    repository_root = Path(module_file).resolve().parents[1]

    inventory = acceptance._installed_distribution_inventory(repository_root)

    packages = inventory["packages"]
    assert inventory["package_count"] == len(packages)
    assert inventory["record_bytes_bound"] is True
    assert inventory["record_file_count"] > inventory["package_count"]
    assert inventory["record_total_size_bytes"] > 0
    assert len(inventory["canonical_sha256"]) == 64
    assert packages == sorted(packages, key=lambda item: item["name"])
    assert len({item["name"] for item in packages}) == len(packages)
    assert any(item["name"] == "groove-serpent" for item in packages)
    assert all(item["record_entry_present"] is True for item in packages)
    assert all(len(item["record_files_sha256"]) == 64 for item in packages)


def test_record_file_hash_and_size_are_verified(tmp_path: Path) -> None:
    payload = b"installed dependency bytes\n"
    installed = tmp_path / "dependency.py"
    installed.write_bytes(payload)
    declared = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()

    receipt = acceptance._hash_installed_record_file(
        installed,
        record_hash_mode="sha256",
        record_hash_value=declared,
        record_size=len(payload),
    )

    assert receipt["sha256"] == hashlib.sha256(payload).hexdigest()
    assert receipt["record_hash_verified"] is True
    assert receipt["record_size_verified"] is True


@pytest.mark.parametrize(
    ("declared_hash", "declared_size", "message"),
    [
        ("wrong", len(b"installed dependency bytes\n"), "RECORD hash"),
        (None, 1, "RECORD declaration"),
    ],
)
def test_record_file_mismatch_fails_closed(
    tmp_path: Path,
    declared_hash: str | None,
    declared_size: int,
    message: str,
) -> None:
    installed = tmp_path / "dependency.py"
    installed.write_bytes(b"installed dependency bytes\n")

    with pytest.raises(acceptance.AcceptanceError, match=message):
        acceptance._hash_installed_record_file(
            installed,
            record_hash_mode="sha256" if declared_hash is not None else None,
            record_hash_value=declared_hash,
            record_size=declared_size,
        )


def test_record_path_rejects_missing_and_outside_environment(tmp_path: Path) -> None:
    environment = tmp_path / ".venv"
    environment.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")

    with pytest.raises(acceptance.AcceptanceError, match="outside .venv"):
        acceptance._safe_installed_record_path(
            environment,
            outside,
            distribution_name="adversarial",
            record_path="../../outside.py",
        )

    with pytest.raises(acceptance.AcceptanceError, match="missing"):
        acceptance._safe_installed_record_path(
            environment,
            environment / "missing.py",
            distribution_name="adversarial",
            record_path="missing.py",
        )


def test_uv_sync_check_requires_locked_no_change_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="",
            stderr=(
                "Would use project environment at: .venv\n"
                "Resolved 19 packages in 1ms\n"
                "Checked 19 packages in 1ms\n"
                "Would make no changes\n"
            ),
        )

    monkeypatch.setattr(acceptance.subprocess, "run", run)
    environment = tmp_path / ".venv"
    environment.mkdir()
    interpreter = environment / "Scripts" / "python.exe"
    result = acceptance._uv_sync_check(
        {"absolute_path": "C:\\tools\\uv.exe"},
        tmp_path,
        interpreter,
        environment,
    )

    assert observed["command"] == [
        "C:\\tools\\uv.exe",
        "--no-config",
        "sync",
        "--check",
        "--locked",
        "--python",
        os.fspath(interpreter),
        "--offline",
        "--no-progress",
        "--color",
        "never",
    ]
    assert observed["cwd"] == tmp_path
    assert result["environment_synchronized"] is True
    assert result["would_make_no_changes"] is True
    assert result["checked_package_count"] == 19


def test_uv_sync_check_rejects_unrelated_success_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="",
            stderr="A different uv operation succeeded.\n",
        )

    monkeypatch.setattr(acceptance.subprocess, "run", run)
    environment = tmp_path / ".venv"
    environment.mkdir()

    with pytest.raises(acceptance.AcceptanceError, match="not synchronized"):
        acceptance._uv_sync_check(
            {"absolute_path": "C:\\tools\\uv.exe"},
            tmp_path,
            environment / "Scripts" / "python.exe",
            environment,
        )


def test_uv_sync_check_rejects_wrong_reported_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = tmp_path / ".venv"
    canonical.mkdir()
    wrong = tmp_path / "wrong-environment"
    wrong.mkdir()

    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="",
            stderr=(
                f"Would use project environment at: {wrong}\n"
                "Resolved 19 packages in 1ms\n"
                "Checked 19 packages in 1ms\n"
                "Would make no changes\n"
            ),
        )

    monkeypatch.setattr(acceptance.subprocess, "run", run)

    with pytest.raises(acceptance.AcceptanceError, match="other than the canonical"):
        acceptance._uv_sync_check(
            {"absolute_path": "C:\\tools\\uv.exe"},
            tmp_path,
            canonical / "Scripts" / "python.exe",
            canonical,
        )


def test_uv_distribution_count_crosscheck_is_exact() -> None:
    result = acceptance._crosscheck_uv_distribution_counts(
        {"package_count": 19},
        {"resolved_package_count": 19, "checked_package_count": 19},
    )

    assert result["installed_distribution_count_matches"] is True


@pytest.mark.parametrize(
    ("installed", "resolved", "checked"),
    [(19, 18, 19), (19, 19, 18), (0, 0, 0)],
)
def test_uv_distribution_count_crosscheck_rejects_mismatch(
    installed: int,
    resolved: int,
    checked: int,
) -> None:
    with pytest.raises(acceptance.AcceptanceError, match="do not match"):
        acceptance._crosscheck_uv_distribution_counts(
            {"package_count": installed},
            {
                "resolved_package_count": resolved,
                "checked_package_count": checked,
            },
        )


def test_exact_write_lease_conflict_evidence_is_accepted() -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=acceptance._LEASE_CONFLICT_EXIT_CODE,
        stdout=(f"{acceptance._LEASE_CONFLICT_MARKER}\n{acceptance._LEASE_CONFLICT_MESSAGE}\n"),
        stderr="",
    )

    acceptance._validate_lease_conflict_result(completed)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (0, "GROOVE_SERPENT_EXPECTED_WRITE_LEASE_CONFLICT_V1\nmessage\n", ""),
        (73, "Another ProjectValidationError\n", ""),
        (
            73,
            "GROOVE_SERPENT_EXPECTED_WRITE_LEASE_CONFLICT_V1\nwrong message\n",
            "",
        ),
        (
            73,
            "GROOVE_SERPENT_EXPECTED_WRITE_LEASE_CONFLICT_V1\n"
            "Another Groove Serpent process is writing this project; "
            "retry after that save finishes.\n",
            "unexpected stderr",
        ),
    ],
)
def test_unrelated_write_lease_errors_cannot_pass(
    returncode: int,
    stdout: str,
    stderr: str,
) -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )

    with pytest.raises(acceptance.AcceptanceError, match="exact expected"):
        acceptance._validate_lease_conflict_result(completed)


def test_promotion_authority_is_rechecked_without_mutating_start_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"schema": "authority/1", "promotion_enforced": True}
    monkeypatch.setattr(
        acceptance,
        "_capture_promotion_authority",
        lambda: dict(expected),
    )

    result = acceptance._finalize_promotion_authority(expected)

    assert result == {
        "schema": "authority/1",
        "promotion_enforced": True,
        "rechecked_at_end": True,
    }
    assert "rechecked_at_end" not in expected


def test_promotion_authority_recheck_rejects_any_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {"schema": "authority/1", "generator": {"sha256": "a" * 64}}
    changed = {"schema": "authority/1", "generator": {"sha256": "b" * 64}}
    monkeypatch.setattr(
        acceptance,
        "_capture_promotion_authority",
        lambda: changed,
    )

    with pytest.raises(acceptance.AcceptanceError, match="changed during acceptance"):
        acceptance._finalize_promotion_authority(expected)


def test_main_rejects_injected_arguments_before_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_called = False

    def capture() -> dict[str, object]:
        nonlocal capture_called
        capture_called = True
        return {}

    monkeypatch.setattr(acceptance, "_capture_promotion_authority", capture)

    with pytest.raises(acceptance.AcceptanceError, match="does not accept injected"):
        acceptance.main([])

    assert capture_called is False


def test_promotion_workload_accepts_only_exact_release_slice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = Path("N:\\private-acceptance")
    monkeypatch.setattr(
        acceptance,
        "validate_standard_acceptance_root",
        lambda path: path,
    )

    assert (
        acceptance._validate_promotion_workload(
            target,
            duration_seconds=acceptance._PROMOTION_DURATION_SECONDS,
            minimum_source_bytes=acceptance._PROMOTION_MINIMUM_SOURCE_BYTES,
            enforce_standard_root=True,
            keep_workdir=False,
        )
        == target
    )


@pytest.mark.parametrize(
    ("duration", "minimum", "enforce", "keep", "message"),
    [
        (16.0, 2 * 1024 * 1024, False, False, "nonstandard root"),
        (16.0, 2 * 1024 * 1024, True, True, "must clean"),
        (15.99, 2 * 1024 * 1024, True, False, "exactly 16.0"),
        (16.0, 2 * 1024 * 1024 - 1, True, False, "exact minimum"),
    ],
)
def test_promotion_workload_rejects_weaker_settings(
    duration: float,
    minimum: int,
    enforce: bool,
    keep: bool,
    message: str,
) -> None:
    with pytest.raises(acceptance.AcceptanceError, match=message):
        acceptance._validate_promotion_workload(
            Path("N:\\private-acceptance"),
            duration_seconds=duration,
            minimum_source_bytes=minimum,
            enforce_standard_root=enforce,
            keep_workdir=keep,
        )


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--allow-nonstandard-root"], "nonstandard root"),
        (["--keep-workdir"], "must clean"),
        (["--duration-seconds", "1"], "exactly 16.0"),
        (["--minimum-source-bytes", "1"], "exact minimum"),
    ],
)
def test_promotion_main_rejects_weaker_cli_before_authority_capture(
    arguments: list[str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_called = False
    module_file = acceptance.__file__
    assert module_file is not None

    def capture() -> dict[str, object]:
        nonlocal capture_called
        capture_called = True
        return {}

    monkeypatch.setenv(
        acceptance.ACCEPTANCE_ROOT_ENV,
        "N:\\private-acceptance",
    )
    monkeypatch.setattr(
        acceptance.sys,
        "argv",
        [os.fspath(Path(module_file).resolve()), *arguments],
    )
    monkeypatch.setattr(acceptance, "_capture_promotion_authority", capture)

    with pytest.raises(acceptance.AcceptanceError, match=message):
        acceptance.main()

    assert capture_called is False


def test_library_acceptance_requires_explicit_diagnostic_mode(tmp_path: Path) -> None:
    target = tmp_path / "must-not-be-created"

    with pytest.raises(acceptance.AcceptanceError, match="explicitly opt into"):
        acceptance.run_acceptance(
            target,
            duration_seconds=0.25,
            minimum_source_bytes=0,
            enforce_standard_root=False,
        )

    assert not target.exists()


def test_library_run_samples_free_space_before_fixture_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def volume_details(_path: Path) -> dict[str, object]:
        events.append("free-space")
        return {"free_bytes_at_start": 123_456}

    def write_fixture(
        root: Path,
        *,
        duration_seconds: float,
        minimum_source_bytes: int,
        ffmpeg_path: str | None,
    ) -> tuple[Path, tuple[Path, ...], tuple[dict[str, object], ...]]:
        assert duration_seconds == 0.25
        assert minimum_source_bytes == 0
        assert ffmpeg_path == "bound-ffmpeg"
        events.append("fixture")
        root.mkdir()
        return root / "plan.json", (root / "source.flac",), ()

    monkeypatch.setattr(acceptance, "_volume_details", volume_details)
    monkeypatch.setattr(acceptance, "_ffmpeg_path", lambda: "bound-ffmpeg")
    monkeypatch.setattr(acceptance, "_write_synthetic_album", write_fixture)
    monkeypatch.setattr(
        acceptance,
        "_accept_identity_snapshot",
        lambda _root, _source: {"passed": True},
    )
    monkeypatch.setattr(
        acceptance,
        "_accept_atomic_no_replace",
        lambda _root: {"passed": True},
    )
    monkeypatch.setattr(
        acceptance,
        "_accept_write_lease",
        lambda _root: {"passed": True},
    )
    monkeypatch.setattr(
        acceptance,
        "_accept_interrupted_recovery",
        lambda _root, _plan: {"passed": True},
    )
    monkeypatch.setattr(
        acceptance,
        "_accept_publication",
        lambda _root, _plan: {"passed": True},
    )

    result = acceptance.run_acceptance(
        tmp_path / "acceptance",
        duration_seconds=0.25,
        minimum_source_bytes=0,
        enforce_standard_root=False,
        keep_workdir=True,
        non_promotion_diagnostic=True,
    )

    assert events == ["free-space", "fixture"]
    assert result["volume"]["free_bytes_at_start"] == 123_456
    assert result["authority"]["promotion_enforced"] is False


def test_standard_root_guard_refuses_broader_owner_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "private-acceptance"
    monkeypatch.setenv(acceptance.ACCEPTANCE_ROOT_ENV, os.fspath(configured))
    broader_tree = tmp_path / "broader-owner-tree"

    with pytest.raises(acceptance.AcceptanceError, match="must be exactly"):
        acceptance.validate_standard_acceptance_root(broader_tree)


def test_standard_root_guard_rejects_exact_root_off_n_drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "private-acceptance"
    monkeypatch.setenv(acceptance.ACCEPTANCE_ROOT_ENV, os.fspath(configured))

    with pytest.raises(acceptance.AcceptanceError, match="must be on N:"):
        acceptance.validate_standard_acceptance_root(configured)


def test_cleanup_refuses_non_owned_directory(tmp_path: Path) -> None:
    root = tmp_path / "acceptance"
    root.mkdir()
    unowned = root / "unowned"
    unowned.mkdir()
    identity = _directory_identity(unowned, label="Test unowned directory")

    with pytest.raises(acceptance.AcceptanceError, match="owned acceptance-run name"):
        acceptance._cleanup_owned_run(unowned, root, identity)

    assert unowned.is_dir()


def test_markdown_preserves_limits_and_scope() -> None:
    result = {
        "schema": acceptance.ACCEPTANCE_SCHEMA,
        "generated_at": "2026-07-13T00:00:00Z",
        "result": "passed",
        "scope": {
            "target_root": "N:\\synthetic",
            "side_count": 2,
            "tracks_per_side": 3,
            "duration_seconds_per_side": 16.0,
            "cleanup_verified": True,
        },
        "volume": {
            "filesystem": "NTFS",
            "drive_type": "fixed",
            "allocation_unit_bytes": 4096,
        },
        "checks": {
            "identity_snapshot": {
                "stream_chunk_count_floor": 3,
                "snapshot_receipt": {"sha256": "a" * 64},
            },
            "multi_side_publication": {
                "estimated_required_bytes": 123,
                "published_artifact_count": 12,
                "tree_receipt": {"canonical_tree_sha256": "b" * 64},
            },
        },
        "limitations": ["Synthetic acceptance is bounded."],
    }

    rendered = acceptance.render_markdown(result)

    assert "Result: **PASSED**" in rendered
    assert "Synthetic acceptance is bounded." in rendered
    assert "not a Groove Serpent 1.0 claim" in rendered


@pytest.mark.skipif(
    os.environ.get("GROOVE_SERPENT_RUN_N_DRIVE_ACCEPTANCE") != "1",
    reason="Set GROOVE_SERPENT_RUN_N_DRIVE_ACCEPTANCE=1 for the destructive N: slice.",
)
def test_opt_in_real_n_drive_acceptance() -> None:
    result = acceptance.run_acceptance(
        acceptance.configured_acceptance_root(),
        duration_seconds=10.0,
        minimum_source_bytes=1024 * 1024,
        non_promotion_diagnostic=True,
    )

    assert result["result"] == "passed"
    assert result["scope"]["cleanup_verified"] is True
    assert result["checks"]["multi_side_publication"]["source_object_count"] == 2
