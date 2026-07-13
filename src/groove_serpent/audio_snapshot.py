"""Verified, operation-local snapshots for consequential audio reads.

Audio tools generally reopen input paths themselves.  A hash check performed
before launching FFmpeg therefore cannot bind the decoder to those checked
bytes: another process can temporarily replace the path and restore it before
the next check.  This module turns the existing verified-copy publication
primitive into one reusable source boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from .cache_storage import (
    SnapshotLease,
    acquire_provisional_snapshot_lease,
    cleanup_stale_snapshots,
    ensure_free_space,
    resolve_cache_root,
)
from .errors import ExportError, GrooveSerpentError, ProjectValidationError
from .publication import (
    FileReceipt,
    PathReceipt,
    assert_file_receipt,
    assert_path_receipt,
    capture_verified_copy,
)


@dataclass(frozen=True, slots=True)
class VerifiedAudioSnapshot:
    """One independently copied audio object bound to a verified live source."""

    live_path: Path
    path: Path
    live_receipt: FileReceipt
    snapshot_receipt: FileReceipt
    live_path_receipt: PathReceipt
    snapshot_path_receipt: PathReceipt
    _snapshot_lease: SnapshotLease = field(
        repr=False,
        compare=False,
    )
    _label: str = field(default="Source audio", repr=False, compare=False)
    _assert_snapshot_on_use: bool = field(default=True, repr=False, compare=False)
    _assert_live_on_use: bool = field(default=True, repr=False, compare=False)

    @property
    def sha256(self) -> str:
        return self.snapshot_receipt.sha256

    @property
    def size_bytes(self) -> int:
        return self.snapshot_receipt.size_bytes

    def assert_snapshot_unchanged(self, *, force: bool = False) -> None:
        """Prove the operation input still matches the captured snapshot."""

        if not force and not self._assert_snapshot_on_use:
            return
        try:
            self._snapshot_lease.assert_owned()
            assert_file_receipt(
                self.path,
                self.snapshot_receipt,
                label=f"Staged {self._label.lower()} snapshot",
            )
        except (ExportError, GrooveSerpentError) as exc:
            raise ProjectValidationError(
                f"The staged {self._label.lower()} snapshot changed during processing."
            ) from exc

    def assert_live_unchanged(self, *, force: bool = False) -> None:
        """Prove the live path still matches the source captured at entry."""

        if not force and not self._assert_live_on_use:
            return
        try:
            assert_file_receipt(self.live_path, self.live_receipt, label=self._label)
        except ExportError as exc:
            raise ProjectValidationError(
                f"{self._label} changed during the verified audio operation."
            ) from exc

    def assert_snapshot_identity(self) -> None:
        """Check snapshot ownership and file identity without a content read."""

        try:
            self._snapshot_lease.assert_owned()
            assert_path_receipt(
                self.path,
                self.snapshot_path_receipt,
                label=f"Staged {self._label.lower()} snapshot",
            )
        except (ExportError, GrooveSerpentError) as exc:
            raise ProjectValidationError(
                f"The staged {self._label.lower()} snapshot lease changed."
            ) from exc

    def assert_live_identity(self) -> None:
        """Check live path identity without rereading its complete contents."""

        try:
            assert_path_receipt(
                self.live_path,
                self.live_path_receipt,
                label=self._label,
            )
        except ExportError as exc:
            raise ProjectValidationError(
                f"{self._label} changed after its session snapshot was captured."
            ) from exc

    def assert_evidence_lease(self) -> None:
        """Cheap proof boundary for non-consequential visual evidence reads."""

        self.assert_snapshot_identity()
        self.assert_live_identity()

    def close(self) -> None:
        """Remove the private snapshot directory.  Cleanup is idempotent."""

        self._snapshot_lease.cleanup()

    def __enter__(self) -> "VerifiedAudioSnapshot":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        try:
            if exc_type is None:
                self.assert_snapshot_unchanged()
                self.assert_live_unchanged()
        finally:
            self.close()


def verified_audio_snapshot(
    live_path: Path | str,
    *,
    expected_sha256: str | None = None,
    expected_size_bytes: int | None = None,
    workspace: Path | str | None = None,
    label: str = "Source audio",
) -> VerifiedAudioSnapshot:
    """Create a verified non-hardlinked copy for one audio operation.

    When no expected hash is supplied, the stable live-file receipt captured at
    entry becomes authoritative.  That is the analysis case: the project binds
    itself to the bytes copied into the snapshot.  Existing projects supply
    their stored hash and size and fail before decoding if either differs.
    """

    source = Path(live_path).expanduser().resolve()
    try:
        source_size = source.stat().st_size
    except OSError as exc:
        raise ProjectValidationError(
            f"{label} could not be inspected before snapshot capture."
        ) from exc
    if expected_size_bytes is not None and source_size != expected_size_bytes:
        raise ProjectValidationError(
            f"{label} no longer matches its expected byte length."
        )
    cache_root = resolve_cache_root(
        project_path=source,
        configured=workspace,
    )
    try:
        cleanup_stale_snapshots(cache_root)
        ensure_free_space(
            cache_root,
            source_size,
            label="Verified audio snapshot",
        )
        lease = acquire_provisional_snapshot_lease(
            cache_root,
            source_size_bytes=source_size,
        )
    except GrooveSerpentError as exc:
        raise ProjectValidationError(
            f"{label} snapshot storage could not be prepared: {exc}"
        ) from exc
    try:
        suffix = source.suffix.casefold() or ".audio"
        snapshot_path = lease.directory / f"source{suffix}"
        try:
            capture = capture_verified_copy(
                source,
                snapshot_path,
                label=label,
                expected_sha256=expected_sha256,
                expected_size_bytes=(
                    expected_size_bytes
                    if expected_size_bytes is not None
                    else source_size
                ),
            )
        except ExportError as exc:
            raise ProjectValidationError(
                f"{label} changed while its verified audio snapshot was created."
            ) from exc
        lease.bind_source_identity(
            capture.source_receipt.sha256,
            capture.source_receipt.size_bytes,
        )
        return VerifiedAudioSnapshot(
            live_path=source,
            path=snapshot_path,
            live_receipt=capture.source_receipt,
            snapshot_receipt=capture.snapshot_receipt,
            live_path_receipt=capture.source_path_receipt,
            snapshot_path_receipt=capture.snapshot_path_receipt,
            _snapshot_lease=lease,
            _label=label,
        )
    except BaseException:
        lease.cleanup()
        raise


__all__ = ["VerifiedAudioSnapshot", "verified_audio_snapshot"]
