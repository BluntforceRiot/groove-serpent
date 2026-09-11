# Groove Serpent 1.1 Build Contract

Status: ACTIVE — owner-authorized GitHub publication, conditional on current gates.

Date opened: 2026-08-11

### Current publication and repair authority (2026-09-11)

After local upload-readiness approval, the owner explicitly instructed
"go ahead and upload it to github", then "find and fix the errors" when hosted
CI failed. This authorizes the bounded public-source repair, main update, and
`v1.1.0` release with reviewed source, wheel, and sdist assets. It does not waive
exact-byte review, successful current gates, preservation, or final receipts.
Candidate-branch pushes may obtain hosted proof after independent source review;
stable tag/release requires the final exact public commit's successful hosted CI.
Do not reuse development approval as proof of changed stable bytes. A new
Windows portable is not included; preserve its historical evidence and state
that limitation.

Report progress within ten elapsed hours of the latest owner-authorized
continuation, or sooner when a decision is needed. A failed gate does not restart
that allowance. Stop after the authorized repair/publication and final receipts;
do not continue unrelated feature work or unattended build loops.

### Current review authority (2026-09-07)

The owner replaced the browser Sol Pro acceptance step with Astra's direct
review and repair loop. Astra issues the final local APPROVE or REJECT decision
against frozen source and artifact hashes, supported by an independent blind
review and fresh deterministic and runtime evidence. This changes the reviewer,
not the acceptance criteria below. Local approval alone is not publication
authority; the separate owner upload instruction above supplies that authority.

## Historical preparation boundaries

The initial local review ran from `2026-08-11T05:00:18Z` to its progress boundary
at `2026-08-11T15:00:18Z`. The owner subsequently authorized repair/re-review
from `2026-08-11T20:00:00Z`, with a boundary at `2026-08-12T06:00:00Z`.
Stable local preparation began `2026-09-11T04:13:56Z`, with its progress boundary
at `2026-09-11T14:13:56Z`. Those phases did not authorize publication. They are
historical, superseded by the explicit upload and CI-repair instructions above;
none is an unlimited-work allowance or a substitute for current proof.

## Requested outcome

Publish the reviewed Groove Serpent 1.1.0 update with the reconstructed 1.0.1
corrections and collector-workflow improvements already validated locally.
Repair reproduced hosted CI failures, refresh release documentation and exact
artifacts, and verify the final public result. Do not expand this publication
repair into new features or unrelated recording analysis.

## Authority

- Repository: `https://github.com/BluntforceRiot/groove-serpent`
- Baseline commit and tag: `a3923fdf337a1a2284ec4defd03355032706883c`
  (`v1.0.0`)
- Current repair branch: `codex/publication-ci-1.1.0`, in the owner-selected
  publication worktree; public target: `main` and `v1.1.0`.
- The private `codex/1.1.0-dev` checkout is historical development authority,
  not a branch to push. Publish only sanitized public ancestry.
- Real-recording inputs: files under the private owner-selected capture root;
  they are immutable test inputs and must never be modified in place.
- Historical 1.0.1 records are design and regression evidence only. No claimed
  pass or missing source byte transfers to 1.1.

## Acceptance criteria

1. Reproduce and repair the ordinary-PR deterministic-archive failure without
   weakening exact release-commit verification.
2. Reconstruct only 1.0.1 corrections that have a reproducible failure mode,
   an explicit invariant, and a regression test on the new 1.1 bytes.
3. Preserve the completed real-album workflow evidence for analysis, endpoint/runout review,
   identification/artwork evidence, speed review, restoration audition, reopen,
   publication planning, and non-destructive output verification. Rerun affected
   behavior if application code changes; never describe historical evidence as
   a fresh run for changed source.
4. Preserve exact source-sample authority, original-capture immutability,
   propose/apply separation, and no-overwrite publication behavior.
5. Pass applicable unit/integration tests, strict quality gates, browser tests,
   deterministic packaging checks, path/privacy/secret checks, and clean-process
   runtime checks on the exact final candidate.
6. Receive an independent blind review with no unresolved actionable findings.
7. Produce `BUILD_CYCLE_RECEIPT.md` and a current Epic Continuum project-state
   receipt for the exact candidate.
8. Obtain all 17 hosted CI jobs and required steps successful on the exact final
   public commit. Verify uploaded/downloaded asset hashes and preserve existing
   releases. Record actual remote state in the external completion receipt.

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

- No force push, private-history upload, previous-release mutation, repository
  administration change, or deployment beyond the authorized GitHub release.
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

- Preserve `v1.0.0`, previous release assets and all existing public history.
- Repair on `codex/publication-ci-1.1.0`; update `main` only by fast-forward after
  applicable source review and candidate gates. Never push private ancestry.
- Write all generated real-album evidence beneath a dedicated ignored 1.1 test
  output directory, never beside or over an input capture.
- Preserve failed-run evidence and superseded artifacts without overwriting
  them. A necessary public rollback is an explicitly reviewed follow-up revert,
  not history rewriting or deletion of existing releases.

## Terminal condition

Stop after the authorized release is verified and the final build-cycle and
Continuum receipts exist. If a remaining failure or external dependency prevents
delivery, report `HOLD` or `BLOCKED_EVIDENCE` with the exact gap, or report progress
at the continuation boundary. Never turn an unrun check into a pass, and do not
claim published completion from local approval alone.
