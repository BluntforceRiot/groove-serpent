# Changelog

All notable public changes to Groove Serpent are documented here.

## 1.0.0 — 2026-07-15

First collector-grade release of the complete local album workflow.

### Added

- Exception-first Album Workbench with side pairing, drift/readiness review, exact side navigation,
  deliberate repin, metadata/artwork review, and publication operations.
- Strict project schema 4 and album schema 3 with explicit migration registries, immutable backups,
  receipts, historical fixtures, no-overwrite behavior, and forward-field refusal.
- Reopenable album-identification proposals and review-only multimodal endpoint proposals.
- Review-only constant-speed estimation with independent audio-and-visual boundary evidence,
  confidence, diagnostics, outlier handling, coarse-RPM hypotheses, and abstention.
- Persistent proposal, attestation, Original/Proposed/Removed audition, rejection, catalog, and exact
  reopen workflows for hum, rumble, hiss, and continuous crackle.
- Unified immutable publication plans for archival-source, reviewed-restored, corrected-lossless,
  and portable profiles.
- Publication preflight, atomic no-replace execution, complete decode verification, restart
  discovery, deterministic replay comparison, and receipted orphan recovery.
- Optional disabled-by-default private review-evidence corpus with inspect, export, evaluation,
  disable, and exact-delete controls.
- Strict version-matched Codex/Hermes skill over the same identity and human-authority boundaries.
- Multi-engine Playwright coverage for Chromium, Firefox, WebKit, and mobile Chromium, including
  keyboard, reflow, text zoom, reduced-motion, and forced-colors checks where supported.

### Changed

- Exact evidence refreshes serialize a bounded current FFmpeg request and queue the newest exact
  bounds, preventing overlapping WebKit request rewinds while ignoring stale results.
- Browser test fixtures now shut down their complete Windows process trees and fail if a child
  survives, preventing long-lived FFmpeg/Python process accumulation.
- Artwork publication rejects symlink, junction, reparse, redirected, non-directory, and changed
  destination components before and immediately before atomic publication.

### Known limitations

- Boundary, endpoint, speed, identification, and restoration proposals require human review.
- Constant speed does not correct time-varying wow, flutter, or drift.
- Restoration scope proof is not a promise of zero audible impact inside an approved repair.
- CUE timing is approximate and player/library behavior is only claimed for an explicit tested
  matrix.
- The Windows portable is unsigned unless an exact release receipt says otherwise; SmartScreen
  reputation is external state.
- Native macOS interactive audio, spoken screen-reader task completion, and an independent
  nondeveloper real-album run remain useful external validation rather than implied passes.

## 0.5.0a1 — 2026-07-12

First public alpha of the local-first vinyl digitization and conservative restoration workbench.

### Included

- Long FLAC side analysis with reviewable track-boundary proposals.
- Browser waveform and spectrogram evidence, marker editing, transition audition, metadata review,
  checkpoints, undo, and redo.
- Optional MusicBrainz, Cover Art Archive, and AcoustID lookup without uploading source audio.
- Constant per-side speed correction with separately rendered output.
- Review-gated isolated click restoration with before, proposed, and removed-signal audition.
- Verified archival FLAC and portable AAC/M4A export.
- CLI album-side pairing, exact chapter receipts, approximate CUE output, and transactional publication.
- Immutable source binding, deterministic project state, verified operation snapshots, storage
  preflight, and crash-safe cache leases.
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
