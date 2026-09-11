# Groove Serpent 1.1.0 release notes

These notes describe Groove Serpent 1.1.0. Published asset availability and exact validation
records are listed on [GitHub Releases](https://github.com/BluntforceRiot/groove-serpent/releases).
The release receipt and artifact hashes determine the tested and distributable boundary;
approval of earlier development bytes does not transfer to a changed build.

## What changed since 1.0

1.1 strengthens the existing local album workflow rather than adding an automatic remastering mode.
Completed captures remain immutable, all output formats use exact source-sample boundaries, and
the owner remains the listening authority.

### Source fidelity and honest Original audition

Source hashes alone do not make saved stream metadata authoritative. Export, review, and
restoration now independently check the verified audio's precision, channels, sample rate, codec,
sample format, and frame count against the project descriptor. A disagreement is refused; audio
is not silently converted to make the saved description appear correct.

Archival FLAC rendering preserves supported 24-bit integer detail. Verification compares the
source and output through signed 32-bit PCM decoding, independently of the output's declared
precision, so discarded low-order bits cannot disappear from both sides of the comparison. These
archival guarantees do not describe AAC/M4A, which remains a lossy portable derivative, or promise
sample identity after an explicitly selected speed correction or repair.

### Restoration decisions stay with the owner

- Click approval requires the owner browser to play Original, Proposed, and Removed Signal
  uninterrupted at normal speed from at or before the changed window through its end. Partial
  entry, seeking, pausing, stalling, rate changes, and discontinuous playback clocks do not satisfy
  the gate. The recorded action does not prove human perception or an inaudible repair.
- Approved recipes bind full candidate records, exact preview manifests, before/proposed/removed
  audio, and owner-channel authority. The recipe schema is `groove-serpent.restoration-recipe/3`.
- Rejected and protected decisions persist in an exact-state, non-authorizing journal. Reopening
  restores that progress; pending approvals need fresh audition and cannot authorize a render.
- `click-recipe` and `click-render` remain recognizable CLI commands but now fail closed. Create
  decisions, recipes, and restoration renders in the owner review workbench. Native/Bearer clients
  may prepare evidence but cannot perform those owner-only actions.

Full-side `restored.flac` output still requires complete, untruncated coverage of the exact reviewed
music range. Each approved repair is limited to its bounded candidate channels/windows. Hum,
rumble, hiss, and continuous crackle remain separate proposal/audition workflows, not automatic
broadband cleanup or authorization to publish.

### Better endpoint review and safer failures

- A staged track destination must be new. Error-only FFmpeg diagnostics also
  refuse the render even if the encoder exits zero, so existing audio cannot be
  mistaken for a successful new AAC render after a no-overwrite refusal.
- Music-start and music-end proposals are decided independently. A clear ending can remain useful
  when the intro is ambiguous. Low-frequency lead-in/runout evidence still needs corroborating
  needle morphology, and all proposed edges remain owner-reviewable.
- Reviews of sides sharing one capture bind the sibling projects that determine their scope.
  Neighboring-side changes retire stale child review sessions, and endpoint acceptance uses the
  same serialized lock order as ordinary side saves.
- Cleanup binds JSON files and staging directories to the original writer's identity and exact
  bytes. Replaced files/directories and uncertain unregistered artifacts are preserved rather
  than guessed to be disposable.
- Strict JSON rejects duplicate fields and non-finite constants at mutation boundaries. Both
  local servers handle rejected request bodies without reinterpreting leftover bytes as another
  request.
- Windows snapshot leases and no-replace identity handling are hardened. Output paths are checked
  before resolution can hide a symlink or junction, and new relative project paths use portable
  separators while retaining safe handling of legacy Windows-relative references.

### Release integrity

Ordinary changes can build deterministic candidate source archives without borrowing the prior
release's immutable marker. Final-release verification remains separate and fail-closed. Public
source/package scans inspect normalized text and Python/JSON literals; private owner-specific
values come from an external private policy. Source archives exclude Git history bundles.
Historical releases and Git history are not rewritten by these changes.

Hosted CI also checks the package scanner from GitHub's temporary-script environment, provides
an owned headless audio service for real browser playback, and verifies foreign-file preservation
on both normalization-sensitive and normalization-insensitive filesystems. Native audition tests
use an explicit lead-in before the changed window; interrupted-playback rejection tests retain
their short-window cases. No application audition requirement is relaxed by these test repairs.

## Install or upgrade

Use the [README installation and album workflow](README.md). The source package accepts Python
3.11, 3.12, and 3.13; FFmpeg and ffprobe are required, and fixed-speed rendering needs libsoxr.
Run `groove-serpent doctor --json` for the tools and optional fingerprinting capabilities available
in your environment.

Keep the prior application environment, source captures, reviewed exports, and backups of project
and album JSON. Install 1.1 into a new environment and inspect existing projects before further
work. Project schema 4 and album schema 3 remain current; use only explicit migration commands for
older schemas. Do not hand-edit an old restoration recipe or authority record to make it current:
perform the required review in the 1.1 workbench. If a source descriptor is refused, preserve the
existing project and reanalyze the original capture into a new project for review rather than
changing the audio to fit the descriptor.

Changed sides require deliberate review and repinning. New publication plans and export batches
must use nonexistent destinations. Rollback means returning to the retained application and its
compatible project backups; it does not mean overwriting captures or deleting reviewed work.

## Validation and delivery limits

The previously reviewed `1.1.0.dev1` candidate had native Windows Python 3.11/3.13, browser,
real-album export/reopen/replay, source/package, and independent review evidence. That is historical
context, not a fresh stable-release pass. Changed 1.1.0 source and artifact bytes require their own
tests, package checks, independent review, and build-cycle/Continuum receipts. No final test count
or stable approval is asserted in these notes.

The current hosted CI matrix exercises Windows, Ubuntu, and macOS with Python 3.11/3.12/3.13,
four Linux browser projects, package audit/installation, and cross-platform source ZIP identity.
Only completed successful jobs for the exact selected commit count as runtime proof. See the
release's validation record for observed results, including skips and limitations. WSL, live
online/raw-fpcalc identification, and physical-device Safari require separate evidence. Windows
WebKit's missing FLAC decoder must fail closed; its refusal test is not evidence of successful
Safari playback. Transport checks and screenshots do not establish a new human listening
decision. The real-album evidence used no restoration; it does not approve previously rejected
or undecided repairs.

A Windows portable builder is included, but this handoff delivers source and Python packages,
not a newly certified 1.1 portable. Any later portable must be separately listed with exact
release hashes and candidate-specific acceptance evidence. The
[1.0 portable acceptance report](WINDOWS_PORTABLE_ACCEPTANCE_1.0.md) is historical, not 1.1 proof.
No signed installer, automatic updater, SmartScreen reputation, or independent clean-machine
certification is claimed. Follow the [Windows delivery policy](WINDOWS_RELEASE_POLICY.md) for any
unsigned portable asset; never treat an internal manifest as its own authenticity anchor.

Provider lookups remain optional and explicit. MusicBrainz/Cover Art Archive supply optional
metadata/artwork; AcoustID receives only a locally computed fingerprint and duration, never source
audio. Local workflows require no account or network access. No live recording, Audacity control,
plug-in hosting, time-varying wow/flutter correction, generative reconstruction, or promise of zero
audible restoration impact is added in 1.1.

See [CHANGELOG.md](CHANGELOG.md) for the detailed history and [LICENSE](LICENSE) for Apache-2.0.
Private captures, artwork, projects, and evidence are not made public by the code license.
