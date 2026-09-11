from __future__ import annotations

import ctypes
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from groove_serpent import restoration_workflow as workflow
from groove_serpent.errors import GrooveSerpentError
from groove_serpent.portable_names import PortablePathError


OPERATIONS = ("scan", "preview", "render")


def _invoke(operation: str, root: Path, output: Path) -> dict[str, Any]:
    project = root / "project.json"
    if operation == "scan":
        return workflow.scan_project_clicks(project, output)
    if operation == "preview":
        return workflow.create_click_preview(project, root / "scan.json", "clk-synthetic", output)
    return workflow.render_restored_side(project, root / "scan.json", root / "recipe.json", output)


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("kind", ("junction", "symlink"))
@pytest.mark.parametrize("equivalent_ancestor", (False, True))
def test_restoration_rejects_redirecting_output_ancestry_before_preparation(
    tmp_path: Path, operation: str, kind: str, equivalent_ancestor: bool,
) -> None:
    if (kind == "junction") != (os.name == "nt"):
        pytest.skip("Native Windows junction or POSIX symlink regression")
    outside = tmp_path / "outside"
    outside.mkdir()
    redirect = tmp_path / "Redirect-é"
    if kind == "junction":
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(redirect), str(outside)],
            check=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode:
            pytest.skip("Cannot create a native directory junction")
    else:
        redirect.symlink_to(outside, target_is_directory=True)
    marker = outside / "unexpected-preparation.txt"

    def unexpected_preparation(*args: object, **kwargs: Any) -> None:
        (kwargs["workspace"] / marker.name).write_bytes(b"unexpected preparation")
        raise AssertionError("Redirected preparation was reached")

    try:
        requested_parent = tmp_path / "redirect-e\u0301" if equivalent_ancestor else redirect
        with mock.patch.object(
            workflow, "_prepare_restoration_inputs", side_effect=unexpected_preparation,
        ) as prepare:
            with pytest.raises(GrooveSerpentError, match="portable-safe|symlink|reparse"):
                _invoke(operation, tmp_path, requested_parent / "new-output")
        prepare.assert_not_called()
        assert not marker.exists()
        assert list(outside.iterdir()) == []
    finally:
        if kind == "junction":
            os.rmdir(redirect)
        else:
            redirect.unlink()


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("equivalent", (False, True))
def test_restoration_refuses_exact_and_portable_equivalent_existing_outputs(
    tmp_path: Path, operation: str, equivalent: bool,
) -> None:
    existing = tmp_path / "Artifact-é"
    existing.write_bytes(b"preserve existing output")
    output = tmp_path / ("artifact-e\u0301" if equivalent else existing.name)
    with mock.patch.object(workflow, "_prepare_restoration_inputs") as prepare:
        with pytest.raises(GrooveSerpentError, match="already exists"):
            _invoke(operation, tmp_path, output)
    prepare.assert_not_called()
    assert existing.read_bytes() == b"preserve existing output"
    assert {path.name for path in tmp_path.iterdir()} == {existing.name}


@pytest.mark.skipif(os.name == "nt", reason="POSIX dangling output symlink regression")
@pytest.mark.parametrize("operation", OPERATIONS)
def test_restoration_refuses_dangling_output_entries(tmp_path: Path, operation: str) -> None:
    target = tmp_path / "absent-target"
    output = tmp_path / "output-link"
    output.symlink_to(target)
    with mock.patch.object(workflow, "_prepare_restoration_inputs") as prepare:
        with pytest.raises(GrooveSerpentError, match="already exists"):
            _invoke(operation, tmp_path, output)
    prepare.assert_not_called()
    assert output.is_symlink()
    assert not target.exists()


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("new_parent", (False, True))
def test_restoration_preserves_safe_outputs_and_reuses_equivalent_ancestors(
    tmp_path: Path, operation: str, new_parent: bool,
) -> None:
    ancestor = tmp_path / "Collector-é"
    ancestor.mkdir()
    output = tmp_path / "collector-e\u0301"
    if new_parent:
        output /= "new-e\u0301"
    output /= "result-e\u0301"
    canonical_parent = ancestor / "new-é" if new_parent else ancestor
    expected_output = canonical_parent / "result-é"
    inputs = mock.Mock()
    runner = {
        "scan": "_scan_project_clicks",
        "preview": "_create_click_preview",
        "render": "_render_restored_side",
    }[operation]
    with (
        mock.patch.object(workflow, "_prepare_restoration_inputs", return_value=inputs) as prepare,
        mock.patch.object(workflow, runner, return_value={"synthetic": True}) as operation_runner,
    ):
        assert _invoke(operation, tmp_path, output) == {"synthetic": True}
    assert prepare.call_args.kwargs["workspace"] == canonical_parent
    assert prepare.call_args.args[0] == (tmp_path / "project.json").resolve()
    assert expected_output in operation_runner.call_args.args
    inputs.close.assert_called_once_with()
    assert list(ancestor.iterdir()) == []  # Validation itself must not create output directories.


@pytest.mark.parametrize("operation", OPERATIONS)
def test_restoration_rejects_file_ancestors_before_preparation(
    tmp_path: Path, operation: str,
) -> None:
    ancestor = tmp_path / "not-a-directory"
    ancestor.write_bytes(b"preserve ancestor")
    with mock.patch.object(workflow, "_prepare_restoration_inputs") as prepare:
        with pytest.raises(GrooveSerpentError, match="portable-safe|not a directory"):
            _invoke(operation, tmp_path, ancestor / "new-output")
    prepare.assert_not_called()
    assert ancestor.read_bytes() == b"preserve ancestor"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_restoration_rejects_ambiguous_ancestors_before_preparation(
    tmp_path: Path, operation: str,
) -> None:
    first = tmp_path / "Collector-é"
    second = tmp_path / "Collector-e\u0301"
    first.mkdir()
    try:
        second.mkdir()
    except FileExistsError:
        pytest.skip("Filesystem normalizes Unicode directory spellings")
    with mock.patch.object(workflow, "_prepare_restoration_inputs") as prepare:
        with pytest.raises(GrooveSerpentError, match="ambiguous"):
            _invoke(operation, tmp_path, first / "new-output")
    prepare.assert_not_called()
    assert list(first.iterdir()) == list(second.iterdir()) == []


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", (OSError, PortablePathError, RuntimeError))
def test_restoration_path_inspection_failures_are_domain_errors(
    tmp_path: Path, operation: str, failure: type[Exception],
) -> None:
    with (
        mock.patch.object(workflow, "resolve_portable_path", side_effect=failure("synthetic")),
        mock.patch.object(workflow, "_prepare_restoration_inputs") as prepare,
    ):
        with pytest.raises(GrooveSerpentError, match="portable-safe"):
            _invoke(operation, tmp_path, tmp_path / "output")
    prepare.assert_not_called()


def test_borrowed_snapshot_does_not_bypass_output_collision(tmp_path: Path) -> None:
    existing = tmp_path / "existing-output"
    existing.write_bytes(b"preserve output")
    snapshot = mock.Mock()
    with mock.patch.object(workflow, "_prepare_restoration_inputs") as prepare:
        for entrypoint, args in (
            (workflow.scan_project_clicks, ("project.json", existing)),
            (workflow.create_click_preview, ("project.json", "scan.json", "clk-id", existing)),
            (workflow.render_restored_side, ("project.json", "scan.json", "recipe.json", existing)),
        ):
            with pytest.raises(GrooveSerpentError, match="already exists"):
                entrypoint(*args, source_snapshot=snapshot)
    prepare.assert_not_called()
    snapshot.assert_not_called()
    assert existing.read_bytes() == b"preserve output"


@pytest.mark.skipif(os.name != "nt", reason="Native Windows DOS 8.3 alias regression")
@pytest.mark.parametrize("operation", OPERATIONS)
def test_restoration_canonicalizes_valid_short_path_after_safety_checks(
    tmp_path: Path, operation: str,
) -> None:
    get_short_path = ctypes.windll.kernel32.GetShortPathNameW
    get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    get_short_path.restype = ctypes.c_uint32
    size = get_short_path(str(tmp_path), None, 0)
    if not size:
        pytest.skip("Filesystem does not provide DOS 8.3 aliases")
    buffer = ctypes.create_unicode_buffer(size)
    written = get_short_path(str(tmp_path), buffer, size)
    if not written or written >= size:
        pytest.skip("Cannot obtain a native short-path spelling")
    short = Path(buffer.value)
    if short == tmp_path:
        pytest.skip("Fixture has no distinct short-path spelling")
    inputs = mock.Mock()
    runner = {
        "scan": "_scan_project_clicks",
        "preview": "_create_click_preview",
        "render": "_render_restored_side",
    }[operation]
    with (
        mock.patch.object(workflow, "_prepare_restoration_inputs", return_value=inputs) as prepare,
        mock.patch.object(workflow, runner, return_value={}) as operation_runner,
    ):
        _invoke(operation, tmp_path, short / "new-output")
    assert prepare.call_args.kwargs["workspace"] == tmp_path.resolve()
    assert tmp_path.resolve() / "new-output" in operation_runner.call_args.args
    inputs.close.assert_called_once_with()


@pytest.mark.parametrize("overwrite", (False, True))
@pytest.mark.parametrize("late_failure", (False, True))
def test_restoration_atomic_json_preserves_foreign_vacated_temporary_names(
    tmp_path: Path, overwrite: bool, late_failure: bool,
) -> None:
    output = tmp_path / "report.json"
    if overwrite:
        output.write_bytes(b"prior report")
    foreign = b"preserve unrelated replacement"
    vacated = []
    original = os.replace if overwrite else workflow.rename_no_replace

    def publish_then_replace_temporary(source: Path, destination: Path) -> None:
        original(source, destination)
        source.write_bytes(foreign)
        vacated.append(source)
        if late_failure:
            raise OSError("synthetic post-publication interruption")

    owner = workflow.os if overwrite else workflow
    attribute = "replace" if overwrite else "rename_no_replace"
    with mock.patch.object(owner, attribute, side_effect=publish_then_replace_temporary):
        if late_failure:
            with pytest.raises(OSError, match="synthetic post-publication"):
                workflow._atomic_json(output, {"synthetic": True}, overwrite=overwrite)
        else:
            workflow._atomic_json(output, {"synthetic": True}, overwrite=overwrite)
    assert json.loads(output.read_text(encoding="utf-8")) == {"synthetic": True}
    assert len(vacated) == 1
    assert vacated[0].read_bytes() == foreign


@pytest.mark.parametrize("overwrite", (False, True))
def test_restoration_atomic_json_preserves_foreign_temporary_after_failed_publish(
    tmp_path: Path, overwrite: bool,
) -> None:
    output = tmp_path / "report.json"
    if overwrite:
        output.write_bytes(b"prior report")
    foreign = b"preserve unrelated replacement"
    replaced = []

    def replace_temporary_then_fail(source: Path, destination: Path) -> None:
        source.unlink()
        source.write_bytes(foreign)
        replaced.append(source)
        raise OSError("synthetic pre-publication interruption")

    owner = workflow.os if overwrite else workflow
    attribute = "replace" if overwrite else "rename_no_replace"
    with mock.patch.object(owner, attribute, side_effect=replace_temporary_then_fail):
        with pytest.raises(OSError, match="synthetic pre-publication"):
            workflow._atomic_json(output, {"synthetic": True}, overwrite=overwrite)
    assert len(replaced) == 1
    assert replaced[0].read_bytes() == foreign
    if overwrite:
        assert output.read_bytes() == b"prior report"
    else:
        assert not output.exists()


@pytest.mark.parametrize("overwrite", (False, True))
def test_restoration_atomic_json_does_not_unlink_without_an_ownership_receipt(
    tmp_path: Path, overwrite: bool,
) -> None:
    output = tmp_path / "report.json"
    with mock.patch.object(
        workflow, "capture_owned_file_receipt", side_effect=OSError("synthetic receipt failure"),
    ):
        with pytest.raises(OSError, match="synthetic receipt failure"):
            workflow._atomic_json(output, {"synthetic": True}, overwrite=overwrite)
    assert not output.exists()
    stages = list(tmp_path.glob(".report.json.*.tmp"))
    assert len(stages) == 1
    assert json.loads(stages[0].read_text(encoding="utf-8")) == {"synthetic": True}
