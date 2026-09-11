from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from groove_serpent import album as album_module
from groove_serpent.album import AlbumProject, AlbumSide, save_album_project


@pytest.mark.parametrize("overwrite", (False, True))
@pytest.mark.parametrize("replace_temporary", (False, True))
def test_failed_album_save_cleans_only_its_owned_temporary(
    tmp_path: Path, overwrite: bool, replace_temporary: bool
) -> None:
    album = AlbumProject(metadata={}, sides=[AlbumSide("A", 1, "side.groove.json")])
    destination = tmp_path / "album.json"
    if overwrite:
        save_album_project(album, destination)
    previous = destination.read_bytes() if overwrite else None
    temporary_paths: list[Path] = []
    original = album_module.os.replace if overwrite else album_module.rename_no_replace

    def fail(source: Path, target: Path) -> None:
        if target != destination:
            original(source, target)
            return
        temporary_paths.append(source)
        if replace_temporary:
            source.rename(source.with_suffix(".parked"))
            source.write_bytes(b"foreign temporary-path replacement")
        raise OSError("synthetic album commit failure")

    owner = album_module.os if overwrite else album_module
    operation = "replace" if overwrite else "rename_no_replace"
    with patch.object(owner, operation, side_effect=fail):
        with pytest.raises(OSError, match="synthetic album commit failure"):
            save_album_project(album, destination, overwrite=overwrite)
    assert len(temporary_paths) == 1
    if replace_temporary:
        assert temporary_paths[0].read_bytes() == b"foreign temporary-path replacement"
    else:
        assert not temporary_paths[0].exists()
    assert (destination.read_bytes() if destination.exists() else None) == previous
