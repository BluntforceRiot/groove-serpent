# Groove Serpent architecture

## Authority and trust boundaries

The Python model and workflow layers are authoritative. Browser code, CLI parsing, and the optional
agent skill call those layers; they do not reimplement validation or infer approval.

The system separates six kinds of authority:

1. **Source identity:** which immutable capture bytes were inspected.
2. **Editable project state:** exact markers, metadata, revision, history, and checkpoints.
3. **Evidence:** measurements and proposals that may explain or abstain but cannot approve.
4. **Human decision:** explicit review, rejection, protection, or deliberately selected state.
5. **Processing plan:** immutable inputs, tools, settings, ordering, and desired derivative profiles.
6. **Publication proof:** exact produced bytes, verification, replay relationship, and recovery state.

A hash, score, provider match, agent request, or audition attestation is not human perceptual
approval. Source captures are never edited in place.

## Primary data flow

```text
completed lossless side capture(s)
        │
        ├─ capture-envelope validation
        ├─ stable-handle copy + hash → verified immutable snapshot
        │
        ├─ streamed analysis → schema-4 side project
        │                     ├─ exact markers/history/checkpoints
        │                     ├─ synchronized audio/waveform/spectrogram evidence
        │                     ├─ optional provider/topology proposals
        │                     ├─ endpoint and speed proposals
        │                     └─ restoration proposals/decisions/artifacts
        │
        └─ schema-3 album project
                              ├─ ordered side references and approved pins
                              ├─ Album Workbench readiness/exceptions
                              ├─ identification/artwork review
                              └─ immutable publication plan
                                            │
                                            ├─ preflight and stable snapshots
                                            ├─ archival/restored/corrected/portable nodes
                                            ├─ staged no-replace publication
                                            ├─ full verification + chapters/navigation
                                            └─ restart catalog/replay/recovery receipts
```

Provider calls are optional and explicit. No ordinary analysis, review load, save, restoration, or
publication operation uploads source audio.

## Module map

### Core capture, validation, and state

- `capture_envelope.py`: supported format/rate/channel/precision/duration and workload policy.
- `media.py`: FFmpeg/ffprobe discovery, probing, stable hashing, and streamed decode.
- `audio_snapshot.py`: one-pass stable-handle capture into immutable operation/review snapshots.
- `cache_storage.py`: snapshot leases, cache status/cleanup, and storage preflight.
- `subprocess_policy.py`: noninteractive child-process invocation, diagnostics, cancellation, and
  cleanup policy.
- `validation.py`: non-coercive finite/range/text/container validation.
- `portable_names.py`: NFC normalization, portable component budgets, and collision keys.
- `atomic_create.py`, `transaction_lock.py`: no-replace creation and native transaction ownership.
- `models.py`: strict project schema 4, exact state hashes, analyzer baseline, edit history, and
  checkpoints.
- `project_io.py`: strict JSON load, source verification, optimistic concurrency, and atomic save.
- `project_migration.py`, `migration_fence.py`: explicit sequential migrations, backups, pending
  fences, receipts, recovery, and forward-field refusal.

### Analysis and review evidence

- `analysis.py`: streamed levels, adaptive floor, candidate scoring, side-aware topology, and exact
  boundary selection.
- `evidence.py`: bounded waveform, STFT/spectrogram, transient, morphology, selection, and needle
  evidence in integer source-sample coordinates.
- `topology.py`: nonmutating release-duration split/merge proposals.
- `endpoint_proposals.py`: sealed multimodal music-endpoint/runout proposals with abstention.
- `speed_estimation.py`: sealed constant-speed proposals from independently reviewed boundaries and
  reference durations, including confidence, diagnostics, outliers, side disagreement, and
  abstention.
- `review_server.py`, `web/`: loopback-only side cockpit and synchronized transport.

### Album workflow

- `album.py`: strict album schema 3, side speed selection, approved pins, drift, repin, legacy direct
  export, exact chapters, and CUE.
- `album_workbench.py`: deterministic exception/readiness state for the browser landing page.
- `album_review_server.py`: loopback Album Workbench APIs, exact side child sessions, reviewed
  metadata/artwork, identification, and publication operations.
- `album_identification.py`, `album_identification_catalog.py`: locally fingerprinted, ranked,
  content-addressed album evidence and restart classification.
- `album_migration.py`: explicit album migration plan, backup, receipt, and recovery.

### Restoration

- `restoration.py`: bounded impulse/clipped-run detection and micro-repair primitives.
- `restoration_workflow.py`: click scan, coverage ledger, preview-v3, decision recipe, restored-side
  render, and exact PCM proof.
- `restoration_catalog.py`: restart discovery and current/stale/invalid restoration artifacts.
- `continuous_noise.py`: proposal-only stationary hum/rumble evidence from declared references.
- `hum_preview.py`, `rumble_preview.py`, `hiss_preview.py`, `crackle_preview.py`: separate bounded
  processor-specific proposal, attestation, render, and metric contracts.
- `continuous_preview_workflow.py`: persistent expected context, proposal, attestation,
  Original/Proposed/Removed preview, rejection, catalog, and exact reopen across all four kinds.

Continuous preview decisions do not mutate side projects or authorize publication. The current
publication graph accepts only its explicitly supported reviewed-restoration inputs.

### Publication

- `publication.py`: canonical JSON hashes, portable file receipts, stable verification, and immutable
  operation snapshots.
- `exporter.py`: exact side-track FLAC/M4A output, fixed-speed mapping, complete decode/presentation
  proof, and side publication receipts.
- `album_publication_policy.py`: allowed profiles and plan-level policy.
- `album_publication_plan.py`, `album_publication_builder.py`: strict immutable DAG and live-bound
  plan construction.
- `album_publication_executor.py`: snapshot, render, stage, verify, journal, and no-replace commit.
- `album_publication_navigation.py`: exact chapter/navigation artifacts.
- `album_publication_catalog.py`, `album_publication_operations.py`: restart discovery of plans,
  receipts, and orphans.
- `album_publication_durability.py`: verification, replay comparison, orphan inventory, quarantine,
  and exact recovery.

### Providers, local evidence, and machine operation

- `metadata.py`: MusicBrainz search and bounded Cover Art Archive publication with containment,
  content-type/signature, size, hash, redirect, and reparse defenses.
- `recognition.py`: replaceable provider protocol and optional local Chromaprint/AcoustID evidence.
- `review_evidence.py`, `review_evidence_evaluation.py`: optional private append-only records,
  deterministic path-free export, source-group evaluation, abstention, and exact deletion.
- `doctor.py`: dependency and destination capability report.
- `audacity.py`: read-only installation/mod-script-pipe discovery; no control surface.
- `cli.py`: supported human and machine command boundary.
- `skills/groove-serpent/`: version-matched Codex/Hermes instructions over strict interfaces.

## Persistent schemas

### Side project schema 4

A `.groove.json` project contains:

- exact source path metadata, format, sample rate/channels/precision/count, size, and SHA-256;
- analysis settings and summary;
- contiguous exact integer track ranges and track metadata;
- project metadata;
- immutable analyzer baseline;
- bounded exact edit-history transitions and named checkpoints;
- monotonically increasing revision, application version, and timestamps.

The editable-state hash covers only reproducible editable state. The file SHA-256 covers the complete
serialized project. Both are used where their distinction matters.

Schema 4 intentionally makes migration an explicit command rather than a side effect of ordinary
load/save. Historical schemas 1 through 3 have registered sequential migrations. Unknown future
schemas and unexpected fields are refused.

### Album schema 3

An album project contains ordered side references, metadata, optional verified artwork, and an
approved pin for each side. A pin binds project revision/file hash/editable-state hash, source hash,
and selected/project speed-state hashes. Drift is visible and publication is blocked until explicit
repin.

All references remain inside the album-project tree. Relative references are resolved from the album
file rather than the shell working directory. Symlink, junction, reparse, escape, and ambiguous
ancestor cases fail closed.

### Separate evidence and operation schemas

Endpoint, speed, topology, album identification, restoration, continuous preview, migration,
publication, and evidence-corpus documents use independent versioned schemas. They are not smuggled
into the side project as unvalidated free-form state. Every loader checks semantic derivations and
live bindings, not only a stored digest.

## Identity, snapshots, and concurrency

Project mutations carry the revision and project SHA-256 loaded by the caller. Source-bound browser
and CLI work also carries the exact source receipt. A stale but otherwise clean tab is still stale.

Review startup copies and hashes one stable source handle into a leased snapshot. `/audio` and
evidence decode that snapshot. Ordinary range/evidence requests use cheap lease and native identity
checks after initial authentication; consequential recognition, restoration, mutation, and
publication boundaries revalidate full required identities.

Exact evidence requests are bounded and serialized. A completed stale response is ignored and the
newest exact bounds are queued, which avoids overlapping request-body rewinds while retaining
newest-selection behavior. Provider-busy state can still cancel the local evidence path.

Snapshot leases carry owner process identity and creation evidence. Cleanup removes only a
schema-valid lease whose owner is provably absent or reused. Malformed, linked, live, or uncertain
entries remain for inspection.

Lease schema 2 also binds a hashed local process namespace. Windows hosts, Linux/WSL boots and PID
namespaces, and macOS boot sessions therefore cannot interpret one another's PID numbers as death
proof. Legacy schema-1, foreign-namespace, and unavailable-namespace receipts remain uncertain and
are never automatically reclaimed. Interrupted cache quarantines remain discoverable, and their
ownership receipt is removed only after payload members.

Each review server has an independent 128-bit random `.localhost` hostname and host-only cookie.
Cookies are not port-bound. Raw-IP and sibling-host services do not receive the cookie, but a
same-user process that learns the exact random hostname from terminal or browser-profile state and
causes a navigation to that hostname on another port can receive and replay it. Hostile same-user
processes and browser-profile disclosure are outside the review-server threat boundary; generic
cross-port isolation is not claimed.

## Boundary, endpoint, and speed evidence

Analysis uses streamed windows and exact integer samples. Candidate scoring combines quietness,
depth, duration, contrast, expected-duration fit, and side topology. A candidate is evidence, not a
cut approval.

The side cockpit’s audio, waveform, spectrogram, playhead, marker, and selection all use the same
source-sample coordinates. Needle drop/pickup morphology may be shown and protected but does not
become an automatic repair target.

Endpoint proposals are sealed and proposal-only. Speed estimation requires reference duration
provenance plus an exact project-bound attestation that every boundary was reviewed with audio and
visuals independently of those reference durations. Missing, stale, bimodal, inconsistent, or weak
evidence produces diagnostics and abstention rather than a correction authority.

## Restoration proof model

Isolated-click scan records complete, partial, or exploratory coverage and whether candidate
retention was truncated. Preview writes Original, Proposed, Removed Signal, and a receipt. A recipe
must decide every retained candidate exactly once. `restored.flac` is reserved for complete,
untruncated coverage of the reviewed music range.

The rendered side preserves rate, channels, integer precision, and exact range length. Output is
redecoded; approved patch hashes and identity outside approved channel/windows are recomputed.

Continuous processors use owner-reviewed scope/reference assertions and a separate audition
attestation that explicitly does **not** claim listening occurred. Original and Proposed remain
gain-neutral arrays/files; declared comparison and removed-monitor gain are separate. Processor
caps, reference guards, identities, and array-derived metrics are independently checkable. These are
scope and reproducibility proofs, not a promise of inaudibility.

## Unified album publication

`groove-serpent.album-publication-plan/1` is an immutable DAG. It binds the album and side projects,
sources, speed, restoration inputs, artwork, tools, configs, profiles, output names, and dependencies.
Supported profiles are archival-source, restored-side, corrected-lossless, and portable.

Execution revalidates the plan and live inputs, obtains stable snapshots, renders every selected
node, generates exact chapter/navigation artifacts, fully probes and decodes audio, verifies staged
inventory, then commits to one nonexistent destination. The
`groove-serpent.album-publication-manifest/2` receipt captures the exact result.

Reopen catalogs classify plans and publications as current, stale, or invalid. Verification is
read-only. Replay publishes from an explicit plan into another new directory and compares canonical
lossless/result identities. Owned partial directories carry journals; inventory is read-only and
recovery requires the exact journal hash plus native directory identity before quarantine/removal.

No ordinary path replaces a reviewed output. Directory rename is application-failure protection,
not a power-loss guarantee.

## Local evidence and agent operation

The private review corpus is disabled by default. Enabling affects only future explicitly supported
records. Records are versioned, source/project/config/tool-bound, inspectable, deterministically
exportable without paths, evaluable with source-group separation, and exactly deletable. Evaluation
cannot apply a setting or approve a proposal.

The packaged skill exposes stable commands and human-authority boundaries. An agent may inspect,
propose, preflight, or invoke an explicitly authorized operation using expected identities. It may
not manufacture a listening decision, weaken stale-state checks, rewrite canonical JSON by hand, or
turn evidence confidence into approval. An MCP adapter may later transport the same contracts but is
not required by 1.0.

## Deliberate 1.0 exclusions and residual limits

- live USB capture, recording, DAW, plug-in hosting, and Audacity scripting;
- time-varying wow/flutter/drift correction;
- generative reconstruction presented as authentic archival sound;
- cloud accounts, telemetry, or uploaded learning;
- universal player/CUE/browser-codec compatibility;
- automatic human or agent approval;
- a claim that any restoration has zero audible effect;
- power-loss atomicity beyond the documented filesystem behavior;
- a native auto-updater or mandatory installer.

External evidence such as native macOS interactive audio, spoken screen-reader task completion,
owner-player interoperability, and independent nondeveloper real-album completion is reported as
open when it has not been obtained; the architecture does not convert those unknowns into passes.
