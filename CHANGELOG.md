# Changelog

All notable public changes to Groove Serpent are documented here.

## 0.5.0a1 — 2026-07-12

First public alpha of the local-first vinyl digitization and conservative restoration workbench.

### Included

- Long FLAC side analysis with reviewable track-boundary proposals.
- Browser waveform and spectrogram evidence, marker editing, transition audition, metadata review, checkpoints, undo, and redo.
- Optional MusicBrainz, Cover Art Archive, and AcoustID lookup without uploading source audio.
- Constant per-side speed correction with separately rendered output.
- Review-gated isolated click restoration with before, proposed, and removed-signal audition.
- Verified archival FLAC and portable AAC/M4A export.
- CLI album-side pairing, exact chapter receipts, approximate CUE output, and transactional publication.
- Immutable source binding, deterministic project state, verified operation snapshots, storage preflight, and crash-safe cache leases.
- Portable filename limits for UTF-8 and UTF-16 filesystems with deterministic collision-resistant truncation.
- Efficient browser range playback from an already verified review snapshot.
- Cross-platform normalized source archives and public CI for Windows, Ubuntu, and macOS.
- Immutable commit pins for every GitHub Action plus pinned uv and Twine release tooling.
- Fresh-checkout provenance checks that require release archives to match canonical Git bytes.
- Capability-accurate Alpha 1 artwork paired with a real workbench screenshot from synthetic audio.
- Hosted CI explicitly exercises the libsoxr precision-33 resampling path, uses Chocolatey's
  pinned full FFmpeg build on Windows and a commit-pinned option-enabled Homebrew formula on
  macOS. macOS temporary files stay under the canonical runner directory without weakening
  symlink/reparse-point publication defenses.

### Known limitations

- Track boundaries and restoration candidates require human review.
- Browser album pairing and a unified restored/corrected album publication graph are not yet available.
- Restoration currently targets isolated impulses, not broadband hiss, hum, rumble, or continuous crackle.
- Speed correction is constant per side; wow, flutter, and drift are not estimated.
- Browser codec support and player handling of tags, artwork, CUE files, and M4A output vary.
- Installers, signing, automatic updates, and broad native macOS acceptance remain future work.

See [README.md](README.md) for the preservation contract and current workflow.
