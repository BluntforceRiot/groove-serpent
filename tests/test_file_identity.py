from __future__ import annotations

import os
from pathlib import Path

import groove_serpent.album_publication_executor as publication_executor
import groove_serpent.cache_storage as cache_storage
import groove_serpent.transaction_lock as transaction_lock
from groove_serpent.file_identity import stable_creation_time_ns


def test_runtime_identity_uses_the_supported_platform_creation_time(
    tmp_path: Path,
) -> None:
    metadata = tmp_path.stat()
    expected = getattr(metadata, "st_birthtime_ns", None)
    if expected is None and os.name == "nt":
        expected = metadata.st_ctime_ns

    assert stable_creation_time_ns(metadata) == expected
    assert transaction_lock._LockIdentity.capture(metadata).birth_ns == expected
    assert cache_storage._DirectoryIdentity.capture(metadata).birth_ns == expected
    assert (
        publication_executor._directory_identity(
            tmp_path,
            label="Test directory",
        ).birth_ns
        == expected
    )
