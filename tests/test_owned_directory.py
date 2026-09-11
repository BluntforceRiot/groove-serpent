"""Ownership and quarantine boundaries for bounded transaction-directory cleanup."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from groove_serpent import owned_directory as owned


def _capture(root: Path) -> owned.OwnedDirectoryReceipt:
    root.mkdir()
    return owned.capture_owned_directory_receipt(root)


def test_owned_tree_and_registered_subtree_are_removed(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    child = root / "work"
    child.mkdir()
    owned.register_owned_directory(receipt, child)
    (child / "payload.bin").write_bytes(b"owned work")
    (root / "output.bin").write_bytes(b"owned output")
    owned.remove_owned_directory_if_present(child, receipt)
    assert root.is_dir() and not child.exists()
    owned.assert_owned_directory_receipt(root, receipt)
    owned.remove_owned_directory_if_present(root, receipt)
    assert list(tmp_path.iterdir()) == []
    owned.remove_owned_directory_if_present(root, receipt)


@pytest.mark.parametrize("kind", ("file", "directory"))
@pytest.mark.parametrize("scope", ("root", "child"))
def test_replacement_is_preserved_without_quarantine(
    tmp_path: Path, kind: str, scope: str,
) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    target = root
    if scope == "child":
        target = root / "work"
        target.mkdir()
        owned.register_owned_directory(receipt, target)
    target.rename(tmp_path / "original-owned")
    if kind == "file":
        target.write_bytes(b"foreign")
    else:
        target.mkdir()
        (target / "foreign.bin").write_bytes(b"foreign")
    with mock.patch.object(owned, "rename_no_replace") as rename:
        with pytest.raises(OSError, match="preserved"):
            owned.remove_owned_directory_if_present(root, receipt)
    rename.assert_not_called()
    assert (target if kind == "file" else target / "foreign.bin").read_bytes() == b"foreign"


def test_registration_cannot_refresh_a_replaced_identity(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    child = root / "work"
    child.mkdir()
    owned.register_owned_directory(receipt, child)
    child.rename(tmp_path / "original-owned")
    child.mkdir()
    with pytest.raises(OSError, match="replaced registered directory"):
        owned.register_owned_directory(receipt, child)
    with pytest.raises(OSError, match="substituted"):
        owned.remove_owned_directory_if_present(root, receipt)
    assert child.is_dir()


def test_nonempty_root_cannot_be_adopted(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    root.mkdir()
    (root / "unknown.bin").write_bytes(b"preserve")
    with pytest.raises(OSError, match="not empty"):
        owned.capture_owned_directory_receipt(root)
    assert (root / "unknown.bin").read_bytes() == b"preserve"


def test_unregistered_directory_is_not_adopted_during_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    child = root / "unregistered"
    child.mkdir()
    (child / "foreign.bin").write_bytes(b"preserve")
    with pytest.raises(OSError, match="unregistered"):
        owned.remove_owned_directory_if_present(root, receipt)
    assert (child / "foreign.bin").read_bytes() == b"preserve"


def test_quarantine_revalidates_identity_and_restores_foreign_tree(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    original = owned.rename_no_replace

    def replace_before_quarantine(source: Path, target: Path) -> None:
        if source == root:
            source.rename(tmp_path / "original-owned")
            source.mkdir()
            (source / "foreign.bin").write_bytes(b"preserve")
        original(source, target)

    with mock.patch.object(owned, "rename_no_replace", side_effect=replace_before_quarantine):
        with pytest.raises(OSError, match="substituted"):
            owned.remove_owned_directory_if_present(root, receipt)
    assert (root / "foreign.bin").read_bytes() == b"preserve"
    assert (tmp_path / "original-owned").is_dir()
    assert list(tmp_path.glob(".owned-cleanup-*")) == []


def test_vacated_name_reuse_survives_owned_quarantine_deletion(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    (root / "owned.bin").write_bytes(b"owned")
    original = owned.rename_no_replace

    def reuse_after_quarantine(source: Path, target: Path) -> None:
        original(source, target)
        source.mkdir()
        (source / "foreign.bin").write_bytes(b"preserve")

    with mock.patch.object(owned, "rename_no_replace", side_effect=reuse_after_quarantine):
        with pytest.raises(OSError, match="new occupant"):
            owned.remove_owned_directory_if_present(root, receipt)
    assert (root / "foreign.bin").read_bytes() == b"preserve"
    assert list(tmp_path.glob(".owned-cleanup-*")) == []


def test_cleanup_failure_preserves_primary_error_and_remaining_tree(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    (root / "owned.bin").write_bytes(b"owned")
    with mock.patch.object(owned.shutil, "rmtree", side_effect=OSError("synthetic delete failure")):
        with pytest.raises(OSError, match="synthetic delete failure") as failure:
            owned.remove_owned_directory_if_present(root, receipt)
    assert (root / "owned.bin").read_bytes() == b"owned"
    assert any("preserved material" in note for note in failure.value.__notes__)


def test_unsupported_quarantine_does_not_fall_back_to_recursive_delete(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    (root / "owned.bin").write_bytes(b"owned")
    with (
        mock.patch.object(owned, "rename_no_replace", side_effect=OSError("unsupported rename")),
        mock.patch.object(owned.shutil, "rmtree") as delete,
    ):
        with pytest.raises(OSError, match="unsupported rename"):
            owned.remove_owned_directory_if_present(root, receipt)
    delete.assert_not_called()
    assert (root / "owned.bin").read_bytes() == b"owned"


def test_rename_then_raise_preserves_owned_tree_and_primary_error(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    (root / "owned.bin").write_bytes(b"owned")
    original = owned.rename_no_replace

    def move_then_fail(source: Path, destination: Path) -> None:
        original(source, destination)
        if source == root:
            raise OSError("synthetic late quarantine failure")

    with mock.patch.object(owned, "rename_no_replace", side_effect=move_then_fail):
        with pytest.raises(OSError, match="synthetic late quarantine failure"):
            owned.remove_owned_directory_if_present(root, receipt)
    assert (root / "owned.bin").read_bytes() == b"owned"
    assert list(tmp_path.glob(".owned-cleanup-*")) == []


def test_substituted_parent_is_preserved(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    root = parent / "stage"
    receipt = _capture(root)
    parent.rename(tmp_path / "original-parent")
    parent.mkdir()
    root.mkdir()
    (root / "foreign.bin").write_bytes(b"preserve")
    with pytest.raises(OSError, match="parent"):
        owned.remove_owned_directory_if_present(root, receipt)
    assert (root / "foreign.bin").read_bytes() == b"preserve"


def test_linked_descendant_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "stage"
    receipt = _capture(root)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"preserve")
    link = root / "link.bin"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Native symlink creation unavailable")
    with pytest.raises(OSError, match="linked or reparse"):
        owned.remove_owned_directory_if_present(root, receipt)
    assert os.path.lexists(link)
    assert outside.read_bytes() == b"preserve"
