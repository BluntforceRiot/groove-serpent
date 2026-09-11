"""Temporary cleanup must preserve files not bound by the writer's receipt."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterator
from unittest import mock

import pytest

from groove_serpent import endpoint_proposals, review_evidence_evaluation, speed_estimation
from groove_serpent.atomic_create import OwnedFileReceipt
from groove_serpent.errors import GrooveSerpentError


@pytest.fixture(params=(endpoint_proposals, speed_estimation, review_evidence_evaluation))
def writer(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[ModuleType, Callable[[Path], object]]]:
    module = request.param
    if module is endpoint_proposals:
        with mock.patch.object(
            module, "validate_endpoint_proposal_document", side_effect=lambda value: value,
        ):
            yield module, lambda path: module.write_endpoint_proposal_document(
                {"synthetic": True}, path,
            )
    elif module is speed_estimation:
        yield module, lambda path: module._write_new_bytes(
            b'{"synthetic": true}\n', path, label="Synthetic proposal",
        )
    else:
        yield module, lambda path: module._write_new_canonical(
            path, b'{"synthetic": true}\n',
        )


def test_writer_success_remains_no_overwrite(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]],
) -> None:
    _module, invoke = writer
    destination = tmp_path / "proposal.json"
    invoke(destination)
    published = destination.read_bytes()
    assert json.loads(published) == {"synthetic": True}
    assert list(tmp_path.glob(".proposal.json.*.tmp")) == []
    with pytest.raises(GrooveSerpentError, match="exist"):
        invoke(destination)
    assert destination.read_bytes() == published


def test_receipt_is_bound_to_the_still_open_writer_descriptor(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]],
) -> None:
    module, invoke = writer
    original = module.capture_owned_file_receipt
    observed = []

    def inspect_descriptor(
        path: Path, payload: bytes, *, owned_descriptor: int | None = None,
    ) -> OwnedFileReceipt:
        assert owned_descriptor is not None
        writer_metadata = os.fstat(owned_descriptor)
        assert writer_metadata.st_ino == path.stat().st_ino
        assert writer_metadata.st_size == len(payload)
        observed.append(owned_descriptor)
        return original(path, payload, owned_descriptor=owned_descriptor)

    with mock.patch.object(module, "capture_owned_file_receipt", side_effect=inspect_descriptor):
        invoke(tmp_path / "proposal.json")
    assert len(observed) == 1


@pytest.mark.skipif(os.name == "nt", reason="Windows denies rename of ordinary open writer handle")
def test_same_bytes_replacement_before_capture_cannot_be_adopted(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]],
) -> None:
    module, invoke = writer
    original = module.capture_owned_file_receipt
    foreign: list[tuple[Path, bytes]] = []

    def replace_before_capture(
        path: Path, payload: bytes, *, owned_descriptor: int | None = None,
    ) -> OwnedFileReceipt:
        assert owned_descriptor is not None
        path.rename(tmp_path / "displaced-owned-stage")
        path.write_bytes(payload)
        foreign.append((path, payload))
        return original(path, payload, owned_descriptor=owned_descriptor)

    with (
        mock.patch.object(module, "capture_owned_file_receipt", side_effect=replace_before_capture),
        mock.patch.object(module, "rename_no_replace") as publish,
    ):
        with pytest.raises(OSError, match="changed before its cleanup identity"):
            invoke(tmp_path / "proposal.json")
    publish.assert_not_called()
    assert not (tmp_path / "proposal.json").exists()
    assert len(foreign) == 1
    assert foreign[0][0].read_bytes() == foreign[0][1]


@pytest.mark.parametrize("late_failure", (False, True))
def test_foreign_temporary_after_success_is_preserved(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]], late_failure: bool,
) -> None:
    module, invoke = writer
    original = module.rename_no_replace
    destination = tmp_path / "proposal.json"
    foreign = b"unrelated newly created file"
    vacated: list[Path] = []

    def publish_then_reuse(source: Path, target: Path) -> None:
        original(source, target)
        source.write_bytes(foreign)
        vacated.append(source)
        if late_failure:
            raise OSError("synthetic post-publication failure")

    with mock.patch.object(module, "rename_no_replace", side_effect=publish_then_reuse):
        if late_failure:
            with pytest.raises(OSError, match="synthetic post-publication failure"):
                invoke(destination)
        else:
            invoke(destination)
    assert json.loads(destination.read_bytes()) == {"synthetic": True}
    assert len(vacated) == 1
    assert vacated[0].read_bytes() == foreign


@pytest.mark.parametrize("replacement", ("different", "identical", "in_place"))
def test_foreign_replacement_on_publication_failure_is_preserved(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]], replacement: str,
) -> None:
    module, invoke = writer
    destination = tmp_path / "proposal.json"
    replaced: list[tuple[Path, bytes]] = []

    def replace_then_fail(source: Path, target: Path) -> None:
        original_bytes = source.read_bytes()
        foreign = original_bytes if replacement == "identical" else b"unrelated replacement"
        if replacement != "in_place":
            source.rename(tmp_path / "displaced-owned-stage")
        source.write_bytes(foreign)
        replaced.append((source, foreign))
        raise OSError("synthetic publication failure")

    with mock.patch.object(module, "rename_no_replace", side_effect=replace_then_fail):
        with pytest.raises(OSError, match="synthetic publication failure"):
            invoke(destination)
    assert not destination.exists()
    assert len(replaced) == 1
    assert replaced[0][0].read_bytes() == replaced[0][1]


def test_owned_temporary_is_cleaned_after_publication_failure(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]],
) -> None:
    module, invoke = writer
    destination = tmp_path / "proposal.json"
    with mock.patch.object(
        module, "rename_no_replace", side_effect=OSError("synthetic publication failure"),
    ):
        with pytest.raises(OSError, match="synthetic publication failure"):
            invoke(destination)
    assert not destination.exists()
    assert list(tmp_path.glob(".proposal.json.*.tmp")) == []


def test_racing_destination_is_preserved_and_owned_stage_is_cleaned(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]],
) -> None:
    module, invoke = writer
    destination = tmp_path / "proposal.json"
    foreign = b"racing destination"
    original = module.rename_no_replace

    def occupy_then_publish(source: Path, target: Path) -> None:
        target.write_bytes(foreign)
        original(source, target)

    with mock.patch.object(module, "rename_no_replace", side_effect=occupy_then_publish):
        with pytest.raises((GrooveSerpentError, FileExistsError)):
            invoke(destination)
    assert destination.read_bytes() == foreign
    assert list(tmp_path.glob(".proposal.json.*.tmp")) == []


def test_temporary_without_ownership_receipt_is_not_unlinked(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]],
) -> None:
    module, invoke = writer
    destination = tmp_path / "proposal.json"
    with (
        mock.patch.object(
            module, "capture_owned_file_receipt", side_effect=OSError("synthetic receipt failure"),
        ),
        mock.patch.object(module, "rename_no_replace") as publish,
    ):
        with pytest.raises(OSError, match="synthetic receipt failure"):
            invoke(destination)
    publish.assert_not_called()
    assert not destination.exists()
    stages = list(tmp_path.glob(".proposal.json.*.tmp"))
    assert len(stages) == 1
    assert json.loads(stages[0].read_bytes()) == {"synthetic": True}


@pytest.mark.parametrize("foreign_write", (False, True))
def test_fsync_failure_cleans_only_the_receipt_bound_stage(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]], foreign_write: bool,
) -> None:
    module, invoke = writer
    destination = tmp_path / "proposal.json"
    captured: list[Path] = []
    original = module.capture_owned_file_receipt

    def capture(
        path: Path, payload: bytes, *, owned_descriptor: int | None = None,
    ) -> OwnedFileReceipt:
        receipt = original(path, payload, owned_descriptor=owned_descriptor)
        captured.append(path)
        return receipt

    def fail_fsync(_descriptor: int) -> None:
        assert len(captured) == 1
        if foreign_write:
            captured[0].write_bytes(b"foreign write during fsync failure")
        raise OSError("synthetic fsync failure")

    with (
        mock.patch.object(module.os, "fsync", side_effect=fail_fsync),
        mock.patch.object(module, "capture_owned_file_receipt", side_effect=capture),
        mock.patch.object(module, "rename_no_replace") as publish,
    ):
        with pytest.raises(OSError, match="synthetic fsync failure"):
            invoke(destination)
    publish.assert_not_called()
    assert len(captured) == 1
    assert not destination.exists()
    if foreign_write:
        assert captured[0].read_bytes() == b"foreign write during fsync failure"
    else:
        assert not captured[0].exists()


@pytest.mark.parametrize("phase", ("write", "flush"))
def test_write_or_flush_failure_does_not_adopt_unattested_stage(
    tmp_path: Path, writer: tuple[ModuleType, Callable[[Path], object]], phase: str,
) -> None:
    module, invoke = writer
    destination = tmp_path / "proposal.json"
    original = module.os.fdopen

    def failing_writer(descriptor: int, mode: str) -> mock.MagicMock:
        handle = original(descriptor, mode)
        wrapper = mock.MagicMock(wraps=handle)
        wrapper.__enter__.return_value = wrapper
        wrapper.__exit__.side_effect = handle.__exit__

        def fail_write(payload: bytes) -> None:
            handle.write(payload[:4])
            raise OSError("synthetic pre-receipt write failure")

        def fail_flush() -> None:
            handle.flush()
            raise OSError("synthetic pre-receipt flush failure")

        if phase == "write":
            wrapper.write.side_effect = fail_write
        else:
            wrapper.flush.side_effect = fail_flush
        return wrapper

    with (
        mock.patch.object(module.os, "fdopen", side_effect=failing_writer),
        mock.patch.object(module, "capture_owned_file_receipt") as capture,
        mock.patch.object(module, "rename_no_replace") as publish,
    ):
        with pytest.raises(OSError, match="synthetic pre-receipt"):
            invoke(destination)
    capture.assert_not_called()
    publish.assert_not_called()
    assert not destination.exists()
    stages = list(tmp_path.glob(".proposal.json.*.tmp"))
    assert len(stages) == 1
    assert stages[0].read_bytes()
