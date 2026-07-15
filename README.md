# Groove Serpent

![Groove Serpent](assets/groove-serpent-hero.png)

Groove Serpent is a local-first, open-source workbench for turning completed vinyl captures into
reviewed, reproducible digital albums.

It proposes exact track boundaries, identifies or accepts release metadata and artwork, pairs record
sides, offers conservative restoration audition, and publishes verified FLAC and AAC/M4A copies.
The original capture is never modified. Automatic work remains reviewable, reversible, and bound to
the exact bytes that were inspected. **Your ears are the final authority.**

## Why it is different

- **Local first:** no account, telemetry, subscription, or source-audio upload.
- **Exact review:** audio, waveform, spectrogram, markers, selection, and playhead share integer
  source-sample coordinates.
- **Album aware:** pair sides, expose drift and unresolved work, open exact side evidence, and publish
  through one Album Workbench.
- **Conservative restoration:** compare Original, Proposed, and Removed Signal before accepting any
  bounded change.
- **Reproducible:** strict project files, hashes, history, migrations, immutable publication plans,
  complete verification, replay, and recovery receipts.
- **Agent safe:** an optional Codex/Hermes skill uses strict propose/apply separation and cannot grant
  itself listening or publication approval.

![Groove Serpent Album Workbench](assets/groove-serpent-workbench.png)

## 1.0 highlights

- Exact side analysis and marker editing, including split/merge, zoom, audition, undo/redo, history,
  and checkpoints.
- Multi-side `groove-serpent.album/3` projects and an exception-first browser Album Workbench.
- Optional MusicBrainz, Cover Art Archive, and AcoustID evidence; manual metadata and artwork remain
  first-class.
- Review-only multimodal endpoint and constant-speed proposals with confidence and abstention.
- Exact derivative fixed-speed rendering through integer `asetrate` and libsoxr; pitch and tempo move
  together and archival output stays separate.
- Bounded click/pop/clipped-run repair plus persistent hum, rumble, hiss, and crackle audition
  workflows.
- Unified archival-source, reviewed-restored, corrected-lossless, and portable publication profiles.
- Full output decode verification, exact chapters, approximate CUE, close/reopen discovery,
  deterministic replay comparison, and receipted orphan recovery.
- Explicit project/album migrations and an optional disabled-by-default private evidence corpus.

## Install

Groove Serpent requires Python 3.11, 3.12, or 3.13, plus FFmpeg and ffprobe. NumPy installs with
the package.

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install .
.venv\Scripts\groove-serpent doctor
```

Linux or macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/groove-serpent doctor
```

For a source checkout with `uv`:

```console
uv sync --frozen --group dev
uv run --frozen groove-serpent doctor --json
```

Supported lossless capture formats and workload limits are documented in
[`SUPPORTED_CAPTURES.md`](SUPPORTED_CAPTURES.md). A deterministic unsigned Windows portable is also
produced when the corresponding release receipt proves its exact bytes.

## A complete album loop

Analyze each side:

```powershell
groove-serpent analyze "Artist - Album - Side A.flac" --tracks 5 --side A
groove-serpent analyze "Artist - Album - Side B.flac" --tracks 5 --side B
```

Pair and review:

```powershell
groove-serpent album create "Artist - Album.album.json" `
  --side "A|Artist - Album - Side A.groove.json" `
  --side "B|Artist - Album - Side B.groove.json" `
  --artist "Artist" --album "Album"

groove-serpent album review "Artist - Album.album.json"
```

Plan and publish only after the workbench is ready:

```powershell
groove-serpent album publication plan "Artist - Album.album.json" `
  "Artist - Album.publication-plan.json" `
  --profiles archival-source,corrected-lossless,portable

groove-serpent album publication preflight "Artist - Album.publication-plan.json" --json
groove-serpent album publication execute "Artist - Album.publication-plan.json" `
  "exports\Artist - Album"
groove-serpent album publication verify "exports\Artist - Album" --json
```

Existing output directories are refused. Every audio-bearing operation uses a verified immutable
snapshot and revalidates live inputs before publication.

## Restoration boundaries

Groove Serpent does not promise “no quality loss.” It proves where and how a derivative changed,
provides matched comparison and removed-signal audition, and leaves perceptual approval to the owner.
Needle drop, pickup, handling, and other structural events can be protected. Continuous hum, rumble,
hiss, and crackle processing remains proposal/audition-first and cannot silently change a project.

It does not implement live recording, Audacity control, plug-in hosting, generative reconstruction,
or time-varying wow/flutter correction.

## Privacy and optional providers

Analysis, review, restoration, and export work offline. Provider calls occur only when requested.
AcoustID receives a locally computed fingerprint and duration, never source audio. Put an optional
application key in `GROOVE_SERPENT_ACOUSTID_KEY`; never commit credentials.

## Verification and support

The repository carries checked-in unit/integration tests, strict typing and lint gates,
multi-engine Playwright coverage, deterministic source packaging, migration/recovery tests, and
acceptance reports. Historical evidence is not reused as proof of changed release bytes.

- Architecture: [`ARCHITECTURE.md`](ARCHITECTURE.md)
- Browser and accessibility evidence: [`BROWSER_ACCEPTANCE.md`](BROWSER_ACCEPTANCE.md)
- Capture policy: [`SUPPORTED_CAPTURES.md`](SUPPORTED_CAPTURES.md)
- Windows delivery policy: [`WINDOWS_RELEASE_POLICY.md`](WINDOWS_RELEASE_POLICY.md)
- Exact Windows portable evidence:
  [`WINDOWS_PORTABLE_ACCEPTANCE_1.0.md`](WINDOWS_PORTABLE_ACCEPTANCE_1.0.md)
- Security reporting: [`SECURITY.md`](SECURITY.md)
- Contributing: [`CONTRIBUTING.md`](CONTRIBUTING.md)

Important limitations remain: automatic boundaries need human review; fixed-speed estimation can
abstain and cannot repair drifting speed; CUE timing is approximate; player/library behavior is only
claimed for an explicitly tested matrix; and unsigned Windows downloads may trigger reputation
warnings.

Apache License 2.0. See [`LICENSE`](LICENSE).
