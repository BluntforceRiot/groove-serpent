from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from groove_serpent import metadata
from groove_serpent.atomic_create import OwnedFileReceipt, remove_owned_file_if_present
from test_metadata import FakeResponse, RELEASE_ID


IMAGE = b"\xff\xd8\xffowned fixture image"
RESOLVED = {
    "release_id": RELEASE_ID,
    "urls": {"1200": "https://coverartarchive.org/release/front.jpg"},
}


@contextmanager
def _download_client(
    root: Path, payload: bytes = IMAGE,
) -> Iterator[metadata.CoverArtArchiveClient]:
    client = metadata.CoverArtArchiveClient(root)
    with patch.object(
        client, "_open", return_value=FakeResponse(payload, headers={"Content-Type": "image/jpeg"})
    ):
        yield client


@pytest.mark.parametrize("replacement", ("none", "different", "identical", "modified"))
def test_failed_artwork_rename_removes_only_owned_temp(tmp_path: Path, replacement: str) -> None:
    observed: list[Path] = []

    def fail(source: Path, destination: Path) -> None:
        observed.append(source)
        if replacement in {"different", "identical"}:
            source.rename(source.with_suffix(".retained"))
            source.write_bytes(IMAGE if replacement == "identical" else b"foreign replacement")
        elif replacement == "modified":
            source.write_bytes(b"modified in place")
        raise OSError("primary publication failure")

    with _download_client(tmp_path) as client, patch.object(
        metadata, "rename_no_replace", side_effect=fail
    ):
        with pytest.raises(metadata.MetadataLookupError, match="no-overwrite cover file") as error:
            client._download_resolved_front_art(RESOLVED, size="1200")
    assert str(error.value.__cause__) == "primary publication failure"
    assert len(observed) == 1
    temporary = observed[0]
    if replacement == "none":
        assert not temporary.exists()
    elif replacement == "identical":
        assert temporary.read_bytes() == IMAGE
    elif replacement == "different":
        assert temporary.read_bytes() == b"foreign replacement"
    else:
        assert temporary.read_bytes() == b"modified in place"


@pytest.mark.parametrize("late_error", (False, True))
def test_artwork_cleanup_preserves_repopulated_temp_after_rename(
    tmp_path: Path, late_error: bool,
) -> None:
    original = metadata.rename_no_replace
    observed: list[tuple[Path, Path]] = []

    def publish(source: Path, destination: Path) -> None:
        original(source, destination)
        source.write_bytes(b"foreign after successful rename")
        observed.append((source, destination))
        if late_error:
            raise OSError("primary error after rename")

    with _download_client(tmp_path) as client, patch.object(
        metadata, "rename_no_replace", side_effect=publish
    ):
        if late_error:
            with pytest.raises(metadata.MetadataLookupError, match="no-overwrite cover file"):
                client._download_resolved_front_art(RESOLVED, size="1200")
        else:
            client._download_resolved_front_art(RESOLVED, size="1200")
    assert len(observed) == 1
    assert observed[0][0].read_bytes() == b"foreign after successful rename"
    assert observed[0][1].read_bytes() == IMAGE


def test_artwork_receipt_rejects_identical_foreign_before_capture(tmp_path: Path) -> None:
    original = metadata.tempfile.mkstemp
    observed: list[tuple[Path, Path]] = []

    def create_swapped(*args: Any, **kwargs: Any) -> tuple[int, str]:
        descriptor, name = original(*args, **kwargs)
        temporary = Path(name)
        retained = temporary.with_suffix(".retained")
        # Reopen the same owned object after parking it so this also exercises
        # Windows, whose ordinary Python writer denies rename while it is open.
        os.close(descriptor)
        temporary.rename(retained)
        temporary.write_bytes(IMAGE)
        descriptor = os.open(retained, os.O_RDWR | getattr(os, "O_BINARY", 0))
        observed.append((temporary, retained))
        return descriptor, name

    with _download_client(tmp_path) as client, patch.object(
        metadata.tempfile, "mkstemp", side_effect=create_swapped
    ), patch.object(metadata, "rename_no_replace") as publish:
        with pytest.raises(metadata.MetadataLookupError, match="cleanup identity"):
            client._download_resolved_front_art(RESOLVED, size="1200")
    publish.assert_not_called()
    assert len(observed) == 1
    assert observed[0][0].read_bytes() == observed[0][1].read_bytes() == IMAGE


def test_artwork_result_has_only_original_json_keys_and_owned_receipt(tmp_path: Path) -> None:
    with _download_client(tmp_path) as client:
        result = client._download_resolved_front_art(RESOLVED, size="1200")
    assert isinstance(result, metadata.ArtworkDownloadResult)
    assert isinstance(result.cleanup_receipt, OwnedFileReceipt)
    expected = {
        "relative_path": f"artwork/{RELEASE_ID}-front-1200.jpg",
        "source_url": RESOLVED["urls"]["1200"],
        "mime_type": "image/jpeg",
        "sha256": hashlib.sha256(IMAGE).hexdigest(),
        "size_bytes": len(IMAGE),
        "requested_size": "1200",
        "selected_size": "1200",
    }
    assert result == expected
    assert json.loads(json.dumps(result)) == expected
    assert "cleanup_receipt" not in result
    assert remove_owned_file_if_present(tmp_path / result["relative_path"], result.cleanup_receipt)


def test_artwork_returned_receipt_cannot_delete_same_bytes_replacement(tmp_path: Path) -> None:
    with _download_client(tmp_path) as client:
        result = client._download_resolved_front_art(RESOLVED, size="1200")
    saved = tmp_path / result["relative_path"]
    saved.rename(saved.with_suffix(".retained"))
    saved.write_bytes(IMAGE)
    assert not remove_owned_file_if_present(saved, result.cleanup_receipt)
    assert saved.read_bytes() == IMAGE


def test_artwork_partial_response_failure_cleans_owned_bytes(tmp_path: Path) -> None:
    response = FakeResponse(IMAGE, headers={"Content-Type": "image/jpeg"})
    client = metadata.CoverArtArchiveClient(tmp_path)
    with patch.object(client, "_open", return_value=response), patch.object(
        response, "read", side_effect=[IMAGE[:8], OSError("primary response failure")]
    ):
        with pytest.raises(metadata.MetadataLookupError, match="primary response failure"):
            client._download_resolved_front_art(RESOLVED, size="1200")
    assert list((tmp_path / "artwork").iterdir()) == []


def test_artwork_cleanup_failure_preserves_primary_error(tmp_path: Path) -> None:
    with _download_client(tmp_path) as client, patch.object(
        metadata, "rename_no_replace", side_effect=OSError("primary rename failure")
    ), patch.object(
        metadata, "remove_owned_file_if_present", side_effect=OSError("secondary cleanup failure")
    ):
        with pytest.raises(metadata.MetadataLookupError) as error:
            client._download_resolved_front_art(RESOLVED, size="1200")
    assert str(error.value.__cause__) == "primary rename failure"
    assert len(list((tmp_path / "artwork").glob(".cover-*.tmp"))) == 1


def test_artwork_fsync_failure_cleans_owned_bytes(tmp_path: Path) -> None:
    with _download_client(tmp_path) as client, patch.object(
        metadata.os, "fsync", side_effect=OSError("primary fsync failure")
    ):
        with pytest.raises(metadata.MetadataLookupError, match="primary fsync failure"):
            client._download_resolved_front_art(RESOLVED, size="1200")
    assert list((tmp_path / "artwork").iterdir()) == []
