# Groove Serpent 1.1 Build Contract

Status: ACTIVE — local stable-release preparation; no remote publication is authorized.

Date opened: 2026-08-11

### Stable 1.1.0 preparation (2026-09-11)

The owner authorized preparing 1.1 for upload and updating its README and release
documentation after the local development candidate was approved. Finalize
`1.1.0` source, wheel, and sdist with fresh exact-byte gates and independent blind
review. Do not reuse development approval as stable-byte proof. A new Windows
portable is not included in this handoff; preserve its historical evidence and
state that limitation. No push, tag, release, or deployment is authorized.

This continuation started at `2026-09-11T04:13:56Z`; the progress-review boundary
is `2026-09-11T14:13:56Z`. Stop earlier when upload-readiness is established by
the final external build-cycle and verified Continuum receipts. The historical
timeboxes below describe previous work, not authority to run beyond this one.

### Current review authority (2026-09-07)

The owner has replaced the browser Sol Pro acceptance step with Astra's direct
review and repair loop. Astra issues the final local APPROVE or REJECT decision
against frozen source and artifact hashes, supported by an independent blind
review and fresh deterministic and runtime evidence. This changes the reviewer,
not the acceptance criteria below. Progress is reported within ten elapsed hours
of this continuation; approval ends the work. Publication remains out of scope.

## Review timebox

- Work started: `2026-08-11T05:00:18Z`.
- Hard review boundary: `2026-08-11T15:00:18Z` (10 elapsed hours).
- Stop at the earlier of a finished local candidate or the review boundary,
  report exact progress and evidence, and wait for owner approval before any
  further implementation.
- The timebox limits unattended work; it does not relax any correctness gate.

### Owner-authorized continuation

- The initial review boundary produced an exact progress handoff and successive
  binding local-review verdicts rather than an implied completion claim.
- After the latest Sol Pro rejection, the owner explicitly authorized a repair
  and re-review loop until Sol Pro accepts the frozen candidate.
- Resumed implementation boundary: `2026-08-11T20:00:00Z`.
- Next progress-review boundary: `2026-08-12T06:00:00Z` (10 elapsed hours), or
  earlier if the frozen candidate receives a binding `ACCEPT`.
- Reaching that boundary requires a progress report; it does not authorize a
  push, tag, release, or publication.

## Requested outcome

Build a reviewed local Groove Serpent 1.1 candidate from the published 1.0.0
commit. Incorporate the reproducible correctness and release-engineering work
recorded for the abandoned 1.0.1 candidate, then improve the collector workflow
only where a real owned recording demonstrates a concrete accuracy or usability
problem.

## Authority

- Repository: `https://github.com/BluntforceRiot/groove-serpent`
- Baseline commit and tag: `a3923fdf337a1a2284ec4defd03355032706883c`
  (`v1.0.0`)
- Local branch: `codex/1.1.0-dev`
- Canonical local checkout: the private owner-selected 1.1 worktree.
- Real-recording inputs: files under the private owner-selected capture root;
  they are immutable test inputs and must never be modified in place.
- Historical 1.0.1 records are design and regression evidence only. No claimed
  pass or missing source byte transfers to 1.1.

## Acceptance criteria

1. Reproduce and repair the ordinary-PR deterministic-archive failure without
   weakening exact release-commit verification.
2. Reconstruct only 1.0.1 corrections that have a reproducible failure mode,
   an explicit invariant, and a regression test on the new 1.1 bytes.
3. Exercise one real album workflow through analysis, endpoint/runout review,
   identification/artwork evidence, speed review, restoration audition, reopen,
   publication planning, and non-destructive output verification.
4. Preserve exact source-sample authority, original-capture immutability,
   propose/apply separation, and no-overwrite publication behavior.
5. Pass applicable unit/integration tests, strict quality gates, browser tests,
   deterministic packaging checks, path/privacy/secret checks, and clean-process
   runtime checks on the exact final candidate.
6. Receive an independent blind review with no unresolved actionable findings.
7. Produce `BUILD_CYCLE_RECEIPT.md` and a current Epic Continuum project-state
   receipt for the exact candidate.

### Source-stream authority regression (Astra review)

Saved source metadata is a claim, not a decoder instruction. Before rendering
or presenting an Original audition, independently probe the verified source
bytes and reject mismatched precision, rate, channels, codec, sample format,
or frame count. Do not silently repair the descriptor by changing the audio.
Known integer FLAC sources must preserve their supported precision; the core
renderer must also enforce this when called without the UI or project exporter.
Archival PCM comparisons use signed 32-bit decode precision for both streams,
never a lower precision chosen by a potentially downconverted output. Prove
this with native low-order 24-bit information, genuine 16-bit controls, and
stereo preview fixtures. Listening approval remains the owner's separate act.

## Explicit non-goals

- No push, tag, GitHub release, repository-control change, or public deployment.
- No live recording, Audacity automation, plug-in hosting, signing, installer,
  or automatic updater.
- No time-varying wow/flutter correction or generative audio reconstruction.
- No promise of imperceptible restoration or automatic acceptance of audio
  changes; the owner remains the listening authority.
- No blind reconstruction of the complete lost 1.0.1 patch.

## Expected changed surfaces

- Version, changelog, release documentation, and GitHub workflows.
- Strict input parsing, release evidence, deterministic packaging, publication
  recovery/no-replace behavior, and their tests where failures are reproduced.
- Analysis/review/workbench behavior and tests only where the real-album run
  establishes a concrete 1.1 requirement.

## Preservation and rollback

- Keep `v1.0.0` and public `main` unchanged.
- Work only on the local `codex/1.1.0-dev` branch.
- Write all generated real-album evidence beneath a dedicated ignored 1.1 test
  output directory, never beside or over an input capture.
- Rollback is deletion of the local branch/checkout after preserving any desired
  receipts; no remote rollback should be necessary because publication is out of
  scope.

## Terminal condition

Stop after the exact local candidate is classified as `PROMOTABLE`, `HOLD`, or
`BLOCKED_EVIDENCE`, or report progress when the active continuation boundary
expires. A promotable result is not authorization to publish it.
