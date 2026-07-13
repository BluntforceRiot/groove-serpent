---
name: groove-serpent
description: Operate the local Groove Serpent physical-music digitization workflow. Use when Codex or Hermes needs to inspect or analyze a long vinyl recording, review exact waveform/spectrogram boundaries, identify tracks, retrieve MusicBrainz metadata or artwork, manage history/checkpoints, review fixed-speed correction, pin and publish multi-side albums, or scan/preview/approve/render conservative click repairs while preserving exact project, source, artwork, restoration, and publication identities.
---

# Groove Serpent

Operate the `0.5.0a1` project-local CLI and review cockpit. Resolve the repository root as two directories above this skill folder.

## Enforce read-before-write identity

Treat source captures as immutable and every mutation or audio-bearing operation as receipt-bound.
Groove Serpent must probe, decode, fingerprint, scan, preview, render, and verify immutable
operation/session snapshots rather than repeatedly reopened live paths. Snapshot construction is
one stable-handle copy-and-hash pass. Evidence refresh consumes the existing review-session
snapshot without a whole-source reread. After full session-start authentication, ordinary browser
audio ranges use stable snapshot handles and cheap lease/path identity checks; consequential
operations still revalidate fully. Treat a snapshot, lease, or live receipt disagreement as a hard
stop. Refuse evidence geometry above the 256 MiB decoded-PCM input ceiling.

Before a save, metadata/topology change, recognition request, evidence request, restoration action, repin, or export:

1. Inventory matching `.tracklist.json`, `.groove.json`, `.album.json`, artwork, restoration receipts, and publication manifests.
2. Run `info --json` and record the project revision, project-file SHA-256, editable-state SHA-256, source SHA-256, and source verification receipt.
3. Record the artwork SHA-256 when artwork participates.
4. Pass expected revision/project/source receipts to browser or agent operations that accept them. For CLI operations, re-read immediately before the command.
5. Stop on missing, stale, or mismatched identity. Reload and re-review; never silently retry against newer state.
6. Re-read after a write and report the resulting identities.

Do not hand-edit canonical project, album, restoration, or publication JSON. Use validated CLI/browser/model paths. Prefer an existing hash-matched project over re-analysis. Never replace a reviewed project, derivative, or published directory without explicit user approval and a command that safely supports replacement.

## Prepare the runtime

From the repository root:

```powershell
$env:PYTHONPATH = "src"
uv run --frozen python -m groove_serpent doctor
uv run --frozen python -m groove_serpent info "Artist - Album.groove.json" --json
```

Use `.venv\Scripts\python.exe` only when that environment is complete. Report missing FFmpeg/ffprobe. Treat Chromaprint and AcoustID as optional.

Audacity control and MCP integration remain deferred. Do not enable `mod-script-pipe`, change Audacity configuration, launch/control Audacity, or expose a new MCP server in this workflow. Use the existing CLI/browser validation path.

Snapshot storage defaults to `.groove-serpent/cache/snapshots` beside a project (or source before
a project exists). Set `GROOVE_SERPENT_CACHE_DIR` when another volume is required. Inspect or
safely reclaim leftovers:

```powershell
uv run --frozen python -m groove_serpent cache status --project "Artist - Album.groove.json"
uv run --frozen python -m groove_serpent cache clean --project "Artist - Album.groove.json"
```

`cache status` is read-only. `cache clean` may remove only schema-valid leases whose owner is
provably gone; never manually delete live, malformed, linked/unsafe, or uncertain entries.

## Preserve strict model and development contracts

Never coerce JSON numbers on behalf of a caller. Numeric model fields accept only representable,
in-domain finite JSON integers/floats; booleans, quoted numbers, null, NaN, infinity, overflowing
integers, physically meaningless analyzer magnitudes, and source-inconsistent sample/time geometry
must fail validation.
Integer-only fields also reject fractional values. Surface the validation error and repair the
input rather than hand-editing a canonical project.

When changing Groove Serpent itself, use the pinned Python development dependencies and one
quality entry point:

```powershell
uv sync --frozen --group dev
uv run --frozen --group dev python scripts/check_quality.py
uv run --frozen --group dev python -m pytest -q
```

The quality script checks strict mypy on the native and Linux targets, Flake8, Ruff, and Node
syntax. Preserve the shared 100-character policy and E203 exception for NumPy slice formatting.
Do not report a changed build as frozen until its own complete tests, browser acceptance,
distributions, clean install, and archive/member/leak receipts exist. Frozen release evidence is
version-specific; never relabel an earlier build's receipts as proof of changed source bytes.

## Build or reuse a track list

Prefer official artist/label evidence for title order and a physical-release source for side layout. Preserve provider duration precision and MusicBrainz IDs. Do not accept provider order that conflicts with official, acoustic, or physical-side evidence. Do not infer an exact pressing or color variant from audio alone.

Use strict UTF-8 JSON:

```json
{
  "artist": "Artist",
  "album": "Album",
  "tracks": [
    {"title": "First song", "side": "A", "duration": "4:00"},
    {"title": "Second song", "side": "A", "duration": 195.5}
  ]
}
```

Reject malformed arrays, entries, and clocks such as `4:99`; do not coerce provider mistakes.

## Analyze and review exact evidence

Create a new project path:

```powershell
uv run --frozen python -m groove_serpent analyze "Artist - Album - Side A.flac" `
  --tracklist "Artist - Album - Side A.tracklist.json" `
  --project "Artist - Album - Side A.groove.json"

uv run --frozen python -m groove_serpent review "Artist - Album - Side A.groove.json"
```

Use `--overwrite` only after explicit approval; it discards reviewed markers, metadata, and history. Run `info --json` after analysis.

Use the whole-side map for orientation and the synchronized waveform/spectrogram microscope for exact decisions. Inspect energy, spectral continuity, transient morphology, and low-frequency runout together. Drag/nudge/snap markers, add/split, delete/merge, and audition before/across/after/loop instead of editing JSON.

Rapid selection changes are newest-request-wins. A superseded evidence response must not overwrite
the current view; cancellation should terminate/reap its FFmpeg process. Reusing an exact cached
window is valid, but do not mutate cached payloads or bypass exact source/sample/analysis keys.

Save meaningful checkpoints. Restoring a checkpoint must create a new reversible history entry. Correct track count is not proof of correct cuts. Retain the final wanted fade while excluding runout, needle pickup, and handling audio. Stop when evidence remains ambiguous.

## Apply metadata and topology carefully

Provider calls occur only after explicit UI actions; remain offline when requested. Inspect release side order, durations, medium count, label/catalog, and artwork source before applying. Review and audition a complete topology proposal before approving split/merge changes. Confirm the prior state appears in persistent history.

Treat AcoustID results as hints, not automatic identity or pressing approval. It must re-verify the exact source and must not store the API key in project JSON.

## Review fixed speed

Use one evidence-backed record-specific factor:

```text
capture RPM / intended RPM * fine factor
```

The verified effective-factor range is `0.25` through `2.0`, inclusive. This includes transfers such as 78.26 RPM captured at 33 1/3 RPM (approximately `0.426`). Fail closed outside the range. Do not reuse another record's factor.

Keep Archival and Corrected audition distinct. Corrected playback changes pitch and tempo together; it is not loudness normalization or time-varying wow/flutter correction.

```powershell
uv run --frozen python -m groove_serpent export "Artist - Album - Side A.groove.json" `
  --output-dir "exports\Artist - Album - Side A - corrected" `
  --formats flac --source-speed-factor FACTOR
```

Verify requested/effective factor, integer intermediate rate, libsoxr method, mapped exact sample counts, and unchanged source identity.

## Publish an approved side

```powershell
uv run --frozen python -m groove_serpent export "Artist - Album - Side A.groove.json" `
  --output-dir "exports\Artist - Album - Side A" --formats flac,m4a
```

Require a nonexistent destination. The exporter must use one verified private source snapshot and, when present, one verified artwork snapshot for every track. It must preflight conservative destination storage before copy/encoding, then revalidate the project file, in-memory state, live source, artwork, and staged snapshots before atomic publication.

Filesystem-facing names are Unicode NFC and collide under NFC plus casefold. Never evade a
normalization-equivalent target/filename/side/checkpoint refusal by hand-renaming JSON. Treat
ambiguous or symlink/junction/reparse output ancestors as a hard stop. Generated components must
fit their final UTF-8 byte and UTF-16 code-unit budgets after prefixes/extensions; retain the
deterministic truncation hash and perform collision checks on the final component.

Verify `groove-serpent-manifest.json`:

- `schema` is `groove-serpent.publication-manifest/1` and the Groove Serpent version is recorded.
- Project file SHA-256, revision, editable-state SHA-256, source content/file identity, and artwork identity match the read receipt.
- Output profile, FFmpeg/ffprobe versions, encoder implementation/settings, processing plan, and processing-plan SHA-256 are present.
- Every FLAC/M4A was fully probed and completely decoded.
- FLAC codec/rate/channels/precision/exact sample count are verified. Archival FLAC decoded-PCM SHA-256 equals its selected source-range PCM SHA-256.
- Every M4A `expected_sample_count` equals `presentation_sample_count`.

Any disagreement or one-sample mismatch must delete staging and publish nothing. Never pad or waive a verification failure.

## Pin sides and publish an album

Keep the album project, side projects, sources, and artwork inside one contained directory tree. The short side form inherits the reviewed side-project speed state and pins it. The five-field form creates an explicit visible override.

```powershell
uv run --frozen python -m groove_serpent album create "Artist - Album.album.json" `
  --side "A|Side A.groove.json" --side "B|Side B.groove.json" `
  --artist "Artist" --album "Album" --artwork "artwork\cover.jpg"

uv run --frozen python -m groove_serpent album inspect "Artist - Album.album.json" --json
```

Each pin binds the side project revision/file SHA-256/editable-state SHA-256, source SHA-256, selected speed-state SHA-256, and project speed-state SHA-256. `inherit` must match the project speed state exactly. An `override` must remain visibly different when it disagrees.

If inspect reports drift or an unpinned side, export must stop. Review the changed side first, then explicitly repin only the reviewed labels:

```powershell
uv run --frozen python -m groove_serpent album repin "Artist - Album.album.json" --side A
uv run --frozen python -m groove_serpent album inspect "Artist - Album.album.json" --json
```

Use `--all` only after reviewing every side. Repinning is approval, not refresh housekeeping.

```powershell
uv run --frozen python -m groove_serpent album export "Artist - Album.album.json" `
  --output-dir "album-exports\Artist - Album" --formats flac,m4a
```

Verify pinned identities, inherited/override speed receipts, continuous numbering, side FLACs, per-side manifests, album manifest, artwork, exact count sums, approximate 75-fps CUE disclosure, and exact integer-sample `album.chapters.json`. Refuse portable CUE generation above 99 tracks; split into separately approved volumes.

Album export must also preflight conservative space before copy/render work. Treat its estimate as
an early refusal guard, not a guarantee that the filesystem cannot fill or fail later.

## Restore isolated clicks conservatively

Accept only hash-matched integer 16-bit or 24-bit FLAC. Scanning changes nothing. Needle-drop, pickup, and handling events remain protected even when impulse scores are high.

Start with bounded previews:

```powershell
uv run --frozen python -m groove_serpent click-scan "Side A.groove.json" `
  --report "Side A.600-605.click-scan.json" --start 600 --end 605

uv run --frozen python -m groove_serpent click-preview "Side A.groove.json" `
  "Side A.600-605.click-scan.json" --candidate clk-left --candidate clk-right `
  --bundle "restoration-previews\event-001"
```

Selected IDs must form one short event. Confirm preview schema/bindings, windows no longer than 128 frames, exact format/frame proofs, and identity outside selected windows/channels. Compare `before.flac`, `proposed.flac`, and declared-gain `removed.flac`. Reject when Removed Signal contains wanted music. Never infer approval from detector score.

Create a strict decision for every retained candidate: `approved`, `rejected`, or `protected` with `needle-drop`, `needle-pickup`, `handling-event`, or `other-structural-event`. Require explicit user approval for every approved repair.

Recipe creation snapshots only project/scan JSON and verifies live source identity. It must not
copy, probe, or decode the full audio solely to assemble decisions.

### Enforce the restoration coverage ledger

Inspect the scan/recipe coverage ledger before rendering:

- music start/end and total frames,
- scanned frames and percent,
- whether the scan covers the full music range,
- detected, retained, and unretained candidate counts,
- truncation state and unreviewed regions,
- `restoration_status`: `complete`, `partial`, or `exploratory`.

Partial or exploratory scans may produce previews and reviewed recipes, but must not produce a full `restored.flac`. Full render requires the complete project music range, no candidate truncation, one valid decision for every retained candidate, and at least one explicitly approved repair. Never bypass the rule by renaming selected repairs as a complete restoration. Increase `--max-candidates` only after inspecting the ledger; its hard maximum is 10,000.

A complete, untruncated scan with zero retained candidates should conclude that no restored
derivative is necessary and hide the audition panel plus recipe/render actions. Partial/truncated
zero states keep those actions visible and disabled. Report the exact coverage caveat; do not
generalize this result into a claim that the physical record is globally noiseless.

```powershell
uv run --frozen python -m groove_serpent click-scan "Side A.groove.json" `
  --report "Side A.full.click-scan.json" --max-candidates 10000

uv run --frozen python -m groove_serpent click-recipe "Side A.groove.json" `
  "Side A.full.click-scan.json" --decisions "Side A.decisions.json" `
  --recipe "Side A.restoration-recipe.json"

uv run --frozen python -m groove_serpent click-render "Side A.groove.json" `
  "Side A.full.click-scan.json" "Side A.restoration-recipe.json" `
  --bundle "restored-sides\Side A - reviewed"
```

Verify exact project/source/scan/recipe bindings, coverage, project music-range count, source format, lossless round trip, approved patch hashes, protected decisions, and identity outside approved windows/channels. Never modify or rename the master.

Do not run broadband hiss/hum/rumble/crackle reduction, automatic noise profiling, EQ, normalization, or generative reconstruction. The implemented renderer is only for explicitly approved bounded micro-events.

## Preserve fixtures and defer integrations

Do not regenerate or replace reviewed masters, projects, archival/corrected tracks, or receipts. Treat historical preview schemas as immutable evidence; create new paths for newer previews and recipes.

Use this skill through the existing CLI/browser. Keep MCP/Hermes tool wrapping and Audacity scripting as future work; they must eventually reuse these identity and validation contracts rather than duplicate them.
