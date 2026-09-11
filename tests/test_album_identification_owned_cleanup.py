from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from unittest.mock import patch

import pytest

from groove_serpent import album_identification_catalog as catalog
from groove_serpent.errors import ProjectValidationError
import test_album_identification_catalog as catalog_tests


@pytest.mark.parametrize("replacement", ("none", "different", "identical", "modified"))
def test_failed_catalog_rename_removes_only_owned_temp(tmp_path: Path, replacement: str) -> None:
    album, proposal = catalog_tests.AlbumIdentificationCatalogTests()._case(tmp_path)
    observed: list[tuple[Path, bytes]] = []

    def fail(source: Path, destination: Path) -> None:
        payload = source.read_bytes()
        observed.append((source, payload))
        if replacement in {"different", "identical"}:
            source.rename(source.with_suffix(".retained"))
            source.write_bytes(payload if replacement == "identical" else b"foreign replacement")
        elif replacement == "modified":
            source.write_bytes(b"modified in place")
        raise OSError("primary catalog publication failure")

    with patch.object(catalog, "rename_no_replace", side_effect=fail):
        with pytest.raises(ProjectValidationError, match="no-overwrite proposal") as error:
            catalog.save_album_identification_proposal(album, proposal)
    assert str(error.value.__cause__) == "primary catalog publication failure"
    assert len(observed) == 1
    temporary, payload = observed[0]
    if replacement == "none":
        assert not temporary.exists()
    elif replacement == "identical":
        assert temporary.read_bytes() == payload
    elif replacement == "different":
        assert temporary.read_bytes() == b"foreign replacement"
    else:
        assert temporary.read_bytes() == b"modified in place"


@pytest.mark.parametrize("identical", (False, True))
def test_catalog_cannot_adopt_foreign_temp_after_writer_close(
    tmp_path: Path, identical: bool,
) -> None:
    album, proposal = catalog_tests.AlbumIdentificationCatalogTests()._case(tmp_path)
    original_mkstemp = catalog.tempfile.mkstemp
    original_fdopen = catalog.os.fdopen
    observed: list[Path] = []
    foreign_payload: list[bytes] = []

    def create(*args: Any, **kwargs: Any) -> tuple[int, str]:
        descriptor, name = original_mkstemp(*args, **kwargs)
        observed.append(Path(name))
        return descriptor, name

    @contextmanager
    def replace_after_close(descriptor: int, *args: Any, **kwargs: Any) -> Iterator[BinaryIO]:
        with original_fdopen(descriptor, *args, **kwargs) as handle:
            yield handle
        temporary = observed[-1]
        payload = temporary.read_bytes() if identical else b"foreign after writer close"
        temporary.rename(temporary.with_suffix(".retained"))
        temporary.write_bytes(payload)
        foreign_payload.append(payload)

    with patch.object(catalog.tempfile, "mkstemp", side_effect=create), patch.object(
        catalog.os, "fdopen", side_effect=replace_after_close
    ), patch.object(catalog, "rename_no_replace", side_effect=OSError("primary refusal")):
        with pytest.raises(ProjectValidationError, match="no-overwrite proposal"):
            catalog.save_album_identification_proposal(album, proposal)
    assert len(observed) == 1
    assert observed[0].read_bytes() == foreign_payload[0]
    assert observed[0].with_suffix(".retained").is_file()


def test_catalog_receipt_rejects_identical_foreign_before_capture(tmp_path: Path) -> None:
    album, proposal = catalog_tests.AlbumIdentificationCatalogTests()._case(tmp_path)
    payload = catalog._proposal_bytes(proposal)
    original = catalog.tempfile.mkstemp
    observed: list[tuple[Path, Path]] = []

    def create_swapped(*args: Any, **kwargs: Any) -> tuple[int, str]:
        descriptor, name = original(*args, **kwargs)
        temporary = Path(name)
        retained = temporary.with_suffix(".retained")
        os.close(descriptor)
        temporary.rename(retained)
        temporary.write_bytes(payload)
        descriptor = os.open(retained, os.O_RDWR | getattr(os, "O_BINARY", 0))
        observed.append((temporary, retained))
        return descriptor, name

    with patch.object(catalog.tempfile, "mkstemp", side_effect=create_swapped), patch.object(
        catalog, "rename_no_replace"
    ) as publish:
        with pytest.raises(OSError, match="cleanup identity"):
            catalog.save_album_identification_proposal(album, proposal)
    publish.assert_not_called()
    assert len(observed) == 1
    assert observed[0][0].read_bytes() == observed[0][1].read_bytes() == payload


@pytest.mark.parametrize("late_error", (False, True))
def test_catalog_cleanup_preserves_repopulated_temp_after_rename(
    tmp_path: Path, late_error: bool,
) -> None:
    album, proposal = catalog_tests.AlbumIdentificationCatalogTests()._case(tmp_path)
    original = catalog.rename_no_replace
    observed: list[tuple[Path, Path]] = []

    def publish(source: Path, destination: Path) -> None:
        original(source, destination)
        source.write_bytes(b"foreign after successful rename")
        observed.append((source, destination))
        if late_error:
            raise OSError("primary error after rename")

    with patch.object(catalog, "rename_no_replace", side_effect=publish):
        if late_error:
            with pytest.raises(ProjectValidationError, match="no-overwrite proposal"):
                catalog.save_album_identification_proposal(album, proposal)
        else:
            catalog.save_album_identification_proposal(album, proposal)
    assert len(observed) == 1
    assert observed[0][0].read_bytes() == b"foreign after successful rename"
    assert catalog.load_album_identification_proposal_file(observed[0][1]).proposal == proposal


def test_catalog_cleanup_failure_preserves_primary_error(tmp_path: Path) -> None:
    album, proposal = catalog_tests.AlbumIdentificationCatalogTests()._case(tmp_path)
    with patch.object(
        catalog, "rename_no_replace", side_effect=OSError("primary rename failure")
    ), patch.object(
        catalog, "remove_owned_file_if_present", side_effect=OSError("secondary cleanup failure")
    ):
        with pytest.raises(ProjectValidationError) as error:
            catalog.save_album_identification_proposal(album, proposal)
    assert str(error.value.__cause__) == "primary rename failure"
    assert len(list(tmp_path.glob(".*.tmp"))) == 1


def test_catalog_fsync_failure_cleans_owned_bytes(tmp_path: Path) -> None:
    album, proposal = catalog_tests.AlbumIdentificationCatalogTests()._case(tmp_path)
    with patch.object(catalog.os, "fsync", side_effect=OSError("primary fsync failure")):
        with pytest.raises(OSError, match="primary fsync failure"):
            catalog.save_album_identification_proposal(album, proposal)
    assert list(tmp_path.glob(".*.tmp")) == []


def test_catalog_short_write_cleans_owned_partial_bytes(tmp_path: Path) -> None:
    album, proposal = catalog_tests.AlbumIdentificationCatalogTests()._case(tmp_path)
    original_fdopen = catalog.os.fdopen

    @contextmanager
    def short_writer(descriptor: int, *args: Any, **kwargs: Any) -> Iterator[BinaryIO]:
        with original_fdopen(descriptor, *args, **kwargs) as handle:
            original_write = handle.write
            with patch.object(handle, "write", side_effect=lambda raw: original_write(raw[:8])):
                yield handle

    with patch.object(catalog.os, "fdopen", side_effect=short_writer):
        with pytest.raises(OSError, match="short write"):
            catalog.save_album_identification_proposal(album, proposal)
    assert list(tmp_path.glob(".*.tmp")) == []
