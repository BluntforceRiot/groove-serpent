from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from groove_serpent import publication
from groove_serpent.errors import ExportError


@pytest.fixture(params=("stage", "capture"))
def copy_kind(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def copy_file(kind: str, source: Path, destination: Path) -> None:
    expected = publication.capture_file_receipt(source, label="Synthetic source")
    if kind == "stage":
        publication.stage_verified_copy(source, destination, expected, label="Synthetic source")
    else:
        publication.capture_verified_copy(
            source, destination, label="Synthetic source", expected_sha256=expected.sha256
        )


def test_existing_destination_is_never_deleted(tmp_path: Path, copy_kind: str) -> None:
    source, destination = tmp_path / "source", tmp_path / "existing"
    source.write_bytes(b"immutable source")
    destination.write_bytes(b"unrelated existing content")
    with pytest.raises(ExportError):
        copy_file(copy_kind, source, destination)
    assert source.read_bytes() == b"immutable source"
    assert destination.read_bytes() == b"unrelated existing content"


def test_same_source_and_destination_preserves_original(tmp_path: Path, copy_kind: str) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"immutable source")
    with pytest.raises(ExportError):
        copy_file(copy_kind, source, source)
    assert source.read_bytes() == b"immutable source"


def test_rejected_source_receipt_cannot_delete_existing_destination(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "existing"
    source.write_bytes(b"initial")
    expected = publication.capture_file_receipt(source, label="Synthetic source")
    source.write_bytes(b"changed source")
    destination.write_bytes(b"unrelated existing content")
    with pytest.raises(ExportError, match="changed before"):
        publication.stage_verified_copy(source, destination, expected, label="Synthetic source")
    assert destination.read_bytes() == b"unrelated existing content"


def test_failed_copy_leaves_partial_for_guarded_caller_cleanup(
    tmp_path: Path, copy_kind: str
) -> None:
    source, destination = tmp_path / "source", tmp_path / "partial"
    source.write_bytes(b"immutable source")
    with patch.object(publication.os, "fsync", side_effect=OSError("synthetic fsync failure")):
        with pytest.raises(ExportError, match="synthetic fsync failure"):
            copy_file(copy_kind, source, destination)
    assert destination.read_bytes() == source.read_bytes() == b"immutable source"


def test_replaced_destination_is_rejected_and_never_deleted(
    tmp_path: Path, copy_kind: str
) -> None:
    source, destination = tmp_path / "source", tmp_path / "snapshot"
    parked = tmp_path / "parked-snapshot"
    source.write_bytes(b"immutable source")
    original_capture = publication.capture_file_receipt
    original_path_capture = publication._capture_path_receipt

    def replace_destination(path: Path) -> None:
        if path == destination:
            destination.rename(parked)
            # Identical content still cannot replace the object created by the helper.
            destination.write_bytes(b"immutable source")

    def capture(path: Path, *, label: str) -> publication.FileReceipt:
        replace_destination(path)
        return original_capture(path, label=label)

    def capture_path(path: Path, sha256: str, *, label: str) -> publication.PathReceipt:
        replace_destination(path)
        return original_path_capture(path, sha256, label=label)

    with patch.object(publication, "capture_file_receipt", side_effect=capture), patch.object(
        publication, "_capture_path_receipt", side_effect=capture_path
    ):
        with pytest.raises(ExportError):
            copy_file(copy_kind, source, destination)
    assert source.read_bytes() == b"immutable source"
    assert destination.read_bytes() == b"immutable source"
    assert parked.read_bytes() == b"immutable source"


def test_successful_copy_is_still_independent_and_verified(tmp_path: Path, copy_kind: str) -> None:
    source, destination = tmp_path / "source", tmp_path / "snapshot"
    source.write_bytes(b"immutable source")
    copy_file(copy_kind, source, destination)
    assert destination.read_bytes() == b"immutable source"
    assert not source.samefile(destination)
