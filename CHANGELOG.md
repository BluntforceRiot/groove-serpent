# Changelog

All notable public changes to Groove Serpent are documented here.

## 1.1.0

These entries describe the changes from 1.0.0 to 1.1.0. Exact release validation
and asset availability are recorded on GitHub Releases; approval does not transfer
to changed bytes. See [1.1 release notes](RELEASE_NOTES_1.1.md).

### Fixed

- Fix the hosted package audit's import path when GitHub executes its Python
  script from a temporary directory. Keep private-content rejection enforced.
- Give headless browser tests their own audio service and realistic native
  audition lead-in; keep failed/interrupted playback non-authorizing. Require
  successful replacement audio before accepting a WebKit startup cancellation.
- Check actual filesystem alias behavior in Unicode race tests while preserving
  the foreign file's identity and contents on macOS, Linux, and Windows.
- Refuse preexisting staged track outputs before rendering. Error-only FFmpeg
  diagnostics now fail the operation even when the encoder exits zero, preventing
  a stale but valid AAC file from being credited as a newly rendered track.
- Independently validate saved source descriptors against the actual verified
  audio before export, Original audition, click work, and continuous previews.
  Mismatched precision, channels, rate, codec, sample format, or frame count are
  refused rather than silently converted to match the saved descriptor.
- Preserve supported 24-bit integer precision in archival FLAC rendering.
  Complete source/output PCM comparisons now decode both streams at signed
  32-bit precision, so a lower-precision output cannot hide discarded source
  bits. Core track rendering also validates its source geometry independently.
- Preserve existing or replaced destinations when immutable-copy staging fails;
  failed partial copies remain under their caller's guarded staging cleanup.
- Bind project, album, cache, proposal, evidence, restoration, and publication
  JSON cleanup to the original open writer's file identity and exact bytes,
  preserving replacements even when they contain identical content.
- Preserve substituted preview/export staging directories and artwork files;
  uncertain unregistered restoration artifacts remain non-authorizing scratch
  material instead of being deleted without an ownership receipt.
- Drain bounded rejected HTTP request bodies before closing so ordinary
  unauthorized requests reliably receive their explicit error response.
- Keep CLI output paths lexical until the output policy checks their ancestry,
  so symbolic links and Windows junctions cannot bypass destination rejection.
- Restoration audition requires uninterrupted, normal-speed playback from at or
  before the changed window through its end for every role. Partial entry,
  seeking, pauses, stalls, rate changes, and discontinuous media clocks do not
  satisfy the audition gate.
- New project paths use portable separators. Existing Windows-relative source
  paths can reopen on POSIX without taking precedence over literal POSIX names.
- Public source and package scans share normalized text and Python/JSON literal
  inspection. Owner-specific denylist values are supplied by an external private
  policy, with only its digest retained in local evidence. Source archives exclude
  Git history bundles; earlier public history is not rewritten by this repair.

- Ordinary pull requests now build deterministic candidate source archives
  without misusing the immutable prior-release marker; final-release archive
  verification remains a separate fail-closed mode.
- Python 3.11 on Windows uses its creation-time field for safe no-replace
  publication identity, so archive safety tests execute instead of being skipped.
- State-changing review requests and snapshot lease receipts reject duplicate
  JSON fields and non-finite numeric constants before interpretation.
- Endpoint evidence now decides the music start and end independently, so an
  ambiguous intro cannot hide a trustworthy no-runout ending (or vice versa).
  Needle morphology may corroborate a strongly low-frequency lead-in/runout
  signature, but every proposed edge remains audition-only until explicitly
  accepted by the owner.
- Album-side endpoint review is bound to every sibling project sharing the
  capture, and an open child review is retired if a neighboring side changes
  the physical midpoint from which its exact scope was derived.
- Approved click recipes and renders now require core-validated preview proof
  and an owner-channel authority proof. The recipe binds the exact preview
  manifests, the catalog treats them as dependencies, and recipe creation plus
  rendering re-open and hash the before, proposed, and removed FLAC bytes
  through stable file identities.
- The restoration recipe schema is now version 3. A browser approval seals the
  full scan candidate record rather than the reduced display projection, so the
  core renderer can verify exactly what was reviewed.
- The retired `click-recipe` and `click-render` CLI commands now fail closed;
  recipes and restoration renders are created only through the owner review
  workbench. Decision journals also use a hash of the full project identity,
  preventing display-safe filename collisions from sharing review state.
- Both local review servers close malformed persistent connections when a
  rejected POST body remains unread or a GET incorrectly carries a body,
  preventing the next request from being parsed out of leftover bytes.
- Album-side endpoint acceptance now follows the same child-lock-before-write-
  lease order as ordinary side saves. A real HTTP concurrency regression proves
  that the first serialized mutation succeeds and the stale second mutation is
  rejected without reaching the write-lease timeout.
- Native/Bearer review clients can construct restoration evidence but can no
  longer record decisions, create recipes, or render derivatives. Those routes
  require the same-origin owner browser session, and approvals additionally use
  exact one-use capabilities issued after all three transports cross the changed
  sample window. The recipe schema records that authority
  as an owner-channel action without claiming to prove human perception.
- Rejected and protected click decisions now persist in a strict self-hashed,
  non-authorizing partial journal bound to the exact project, source, scan,
  candidate, and preview. Reopening the workbench restores reviewed progress
  while pending approvals still require a fresh browser audition and a complete
  recipe before any render is possible.

### Compatibility and release scope

- Project schema 4 and album schema 3 remain in use. Restoration recipes now
  require schema 3 and current review authority; do not relabel older recipes
  or treat earlier approvals as fresh audition.
- The reviewed development candidate had native Windows Python 3.11/3.13,
  browser, real-album export/reopen/replay, and package evidence. Those results
  remain historical to its exact bytes, not stable 1.1 release passes.
- Native Linux/WSL/macOS, Python 3.12, live online/raw-fpcalc identification,
  physical-device Safari, and new owner listening approval were not established
  by that development review. Windows WebKit's unavailable FLAC decoder is an
  explicit refusal case, not successful Safari playback.
- A 1.1 portable asset, signing, SmartScreen reputation, and independent
  clean-machine certification are not implied by the source release. Only
  candidate-specific delivery receipts can establish those claims.

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
