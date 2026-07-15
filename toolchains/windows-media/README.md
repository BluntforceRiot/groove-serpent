# Groove Serpent minimal Windows media toolchain

This directory builds the Windows x64 `ffmpeg.exe`/`ffprobe.exe` runtime that
Groove Serpent actually needs. It is intentionally not a general-purpose
FFmpeg distribution.

The design is a shared-library build. FFmpeg, Chromaprint, and libsoxr remain
separate DLLs so a recipient can replace each LGPL library with a compatible
modified build. zlib is statically linked only to let FFmpeg inspect PNG
dimensions before copying PNG cover-art streams. The MinGW GCC runtimes are
statically linked under the GCC Runtime Library Exception 3.1. These are
engineering and provenance observations, not legal advice or a certification
of license compliance.

## Exact sources

| Component | Version and authentication | Role |
| --- | --- | --- |
| FFmpeg | 8.1.2; SHA-256 `464BEB5E7BF0C311E68B45AE2F04E9CC2AF88851ABB4082231742A74D97B524C`; detached signature pinned to `FCF986EA15E6E293A5644F10B4322F04D67658D8` | Probe, decode, filters, FLAC/AAC encode, artwork stream copy, Chromaprint muxer |
| Chromaprint | 1.6.0; SHA-256 `9D33482E56A1389A37A0D6742C376139FA43E3B8A63D29003222B93DB2CB40DA` | Acoustic fingerprint library |
| KissFFT | Vendored inside Chromaprint 1.6.0; `FFT_LIB=kissfft` | BSD-3-Clause FFT backend; FFTW is not used |
| libsoxr | 0.1.3; SHA-256 `B111C15FDC8C029989330FF559184198C161100A59312F5DC19DDEB9B5A15889` | High-precision fixed speed correction |
| zlib | 1.3.2; SHA-256 `D7A0654783A4DA529D1BB793B7AD9C3318020AF77667BCAE35F95D0E42A792F3`; detached signature pinned to `5ED46A6721D365587791E2AA783FCD8E58BCAFBA` | PNG header/decode support needed for PNG artwork stream copy |

The build deliberately omits `--enable-gpl`, `--enable-version3`,
`--enable-nonfree`, all network protocols, and optional codec libraries. The
FFmpeg configure result must report `LGPL version 2.1 or later` or the build
fails. `--disable-x86asm` also avoids depending on a separate assembler and
keeps the narrow reference build easier to reproduce.

`--enable-small` is intentionally not used. It removes the Chromaprint
muxer's identifying long name, which makes Groove Serpent's fail-closed
capability probe reject an otherwise functional muxer.

Chromaprint's own `LICENSE.md` says the package is LGPL 2.1 as a whole because
it contains FFmpeg-derived resampling code. libsoxr is LGPL 2.1 or later.
KissFFT is BSD-3-Clause. zlib uses the zlib License. The runtime carries each
exact license/notice and the complete paired source archive carries all four
upstream source archives, the two detached signatures, pinned public keys, and
the complete build recipe.

The enabled MJPEG decoder uses FFmpeg files whose license notice requires
Independent JPEG Group credit for executable-only distribution: this product
includes software based in part on the work of the Independent JPEG Group.

## Capability scope

The runtime enables only the paths exercised by Groove Serpent:

- probe and decode supported FLAC, PCM WAV, and PCM AIFF captures;
- exact `s16le`, `s32le`, and `f32le` PCM pipes;
- the exact `atrim`, `asettb`, `asetpts`, `asetrate`, and libsoxr
  `aresample` chains;
- lossless FLAC encoding and native AAC-in-M4A (`ipod`) encoding;
- JPEG and PNG cover-art stream copy into FLAC and M4A, plus exact
  `image2pipe` extraction for verification;
- the FFmpeg Chromaprint muxer backed by Chromaprint 1.6.0 and KissFFT;
- the `lavfi`/`anullsrc` path used by `groove-serpent doctor`.

The synthetic smoke creates 16/24-bit WAV and AIFF sources, lossless FLACs,
AAC/M4A and FLAC files with both artwork types, an exact float-PCM window, a
libsoxr speed-corrected render, and a deterministic Chromaprint fingerprint.
It completely decodes the generated audio and byte-compares extracted art.

## Reference build

The reference environment is the `neuroforge` WSL Ubuntu 24.04 instance. Run:

```bash
cd '/mnt/n/HomelabForge/Groove Serpent/toolchains/windows-media'
/usr/bin/bash --noprofile --norc -p ./bootstrap-ubuntu-24.04.sh
DIST_DIR=/tmp/gs-media-dist-a /usr/bin/python3.12 -I -B ./build.py
```

`DIST_DIR` is required and must name a new, absent absolute directory whose
parent already exists. Do not pre-create it. Use a native Linux filesystem such
as `/tmp`; WSL DrvFS paths under `/mnt/n` do not provide the required atomic
directory-publication primitive and are rejected before downloads or compilation.
The normalized output path must also be disjoint from the deterministic work root;
the work root itself and all of its descendants are rejected before staging.

The reproducible compiler path is intentionally fixed at
`/tmp/groove-serpent-windows-media-v1`. Each build holds a non-blocking kernel
lock on that exact directory for its entire lifetime. A second build fails before
cleaning or downloading, and the lock is released automatically when the owning
process exits. While holding the lock, the recipe removes only its enumerated work
children and refuses unexpected residue; the empty mode-`0700` root itself remains
so a new directory inode cannot bypass an older lock.

`bootstrap-ubuntu-24.04.sh` installs only exact versions from the checked-in
inventory. Before its first privileged operation, it requires `/usr/bin/sudo`
and `/usr/bin/apt-get` to come from the exact pinned `sudo` and `apt` packages.
Its supported privileged-Bash invocation ignores inherited startup files,
functions, and shell options, then fixes locale, timezone, umask, frontend, and
APT/proxy/dpkg-affecting environment before inspecting or invoking APT.
Every recipe script resets `PATH` to `/usr/bin:/bin`; the host verifier binds
each directly invoked command to one expected absolute path, owning package,
and pinned package version. The inventory covers the compiler and build tools
as well as the shell, download, signature, archive, certificate, and core
command providers that the recipe invokes directly.
`build.py` is the supported build entry point. It requires isolated mode, validates
its bounded inputs, validates the exact root-owned `/usr/bin/python3.12` and
`/usr/bin/bash` trust roots, then stable-opens and hashes the complete script,
package-inventory, and signing-key authority. It uses `execve` to start the bound
internal `build.sh` under privileged-mode Bash and a minimal allowlisted
environment. This
places sanitation before Bash can import `SHELLOPTS`, `BASH_ENV`, shell functions,
or startup files. Invoking the internal recipe explicitly with Bash is unsupported.

The recipe re-executes itself under a minimal allowlisted environment that carries
only `DIST_DIR`, optional `JOBS`, and the WSL distro label needed to address the
native build directory from the Windows smoke. It then fixes the process locale and timezone,
sets umask `0022`, clears Info-ZIP options, and removes inherited compiler,
linker, make, CMake, pkg-config, Python, and exported-function influence before
any artifact file is created. Every recipe Python invocation uses isolated mode.
The clean-child marker is accepted only when an isolated interpreter confirms
that the complete exported environment matches that allowlist, so setting the
marker in a hostile parent does not bypass re-execution.
Before it executes a live helper or host-verification recipe, the build requires
the snapshot creator to reproduce the launcher-bound aggregate content digest
while stable-copying the exact scripts, package inventory, and signing keys into
a private identity-and-SHA-bound snapshot under the locked work root. The host
provider check and every subsequent helper, key, package, verification, and
source-archive read come from that snapshot, which is revalidated before and
after important consumers and immediately before publication.
Staged file and directory modes are normalized explicitly before hashing and
archiving. The WSL smoke converts native Linux paths to the documented
`\\wsl.localhost\DISTRO\...` form directly and does not depend on WSL's
unpackaged `wslpath` shim.
The build refuses a different version, a different generic MinGW thread
variant, an existing output directory, any input hash/signature mismatch, any
unsafe source member, a GPL/nonfree/version3/network FFmpeg configuration, an
unexpected runtime binary, or an unexpected PE import.

The completed ZIPs and their pair checksum are staged together in an
unpredictable, mode-0700 sibling directory. Before the expensive build begins,
that real stage is moved once with the same kernel-enforced no-replace primitive
used for publication, proving support on the destination filesystem. The final
commit moves the complete directory to `DIST_DIR` with one Linux
`renameat2(RENAME_NOREPLACE)` operation. A racing destination is never replaced,
and consumers can never observe a mixed or partially published artifact set.
An interruption after that one commit is treated as successful only when the
final directory still matches the identity-bound staged snapshot and its exact
`SHA256SUMS` binding. A failed pre-commit stage is retained for inspection rather
than risk deleting a concurrently replaced path. If the outer shell observes a
failed publisher after the stage has disappeared, it may report a self-consistent
final directory for inspection but preserves the failing status because the
pre-commit identity is no longer available. The published directory mode is fixed
at `0755`, and the two archives plus checksum receipt are fixed at `0644`.
The checksum receipt is written from the digests saved by the final full archive
verification, checked against the staged bytes, and supplied again as an anchor
to the atomic publisher; it is never regenerated from unverified later bytes.
Immediately before publication, the completed ZIP pair is reopened through the
artifact verifier, its exact inventories and internal checksums are validated,
the pinned detached source signatures are reverified, and the capability smoke
is replayed from the archived runtime rather than the build staging tree.

The outputs are:

- `groove-serpent-windows-media-8.1.2-x86_64.zip`;
- `groove-serpent-windows-media-8.1.2-corresponding-source.zip`;
- `SHA256SUMS` binding the pair.

Verify an artifact pair with externally supplied hashes:

```bash
/usr/bin/python3.12 -I -B ./verify_artifact.py \
  --runtime-zip /tmp/gs-media-dist-a/groove-serpent-windows-media-8.1.2-x86_64.zip \
  --source-zip /tmp/gs-media-dist-a/groove-serpent-windows-media-8.1.2-corresponding-source.zip \
  --runtime-sha256 EXPECTED_RUNTIME_SHA256 \
  --source-sha256 EXPECTED_SOURCE_SHA256 \
  --verify-signatures \
  --execute-smoke
```

Without both expected hashes, the verifier proves only internal consistency,
not authenticity. The detached-signature option independently re-verifies the
FFmpeg and zlib inputs. It ignores inherited `PATH` and requires the exact
`/usr/bin/gpg` provider to be an executable regular file owned by root:root and
not writable by group or other users. The execution option reruns the capability
suite and requires byte-for-byte equality with the embedded report. It also
executes the Windows programs extracted from the runtime ZIP, so treat
`--execute-smoke` as code execution. Use it only when both expected hashes came
from an independently trusted source and matched in this same invocation;
upstream detached signatures alone do not authenticate the assembled runtime.

## Explicit limits

- This is not legal advice and has not been approved by counsel.
- The media-component corresponding source is paired with the binaries, but
  the Ubuntu APT packages, their transitive dependency closure, and the APT
  repository snapshot are not mirrored in this bundle. Directly invoked host
  package versions are exact and verified; the build is still not a hermetic,
  long-term rebuild environment.
- Reproducibility must be demonstrated by two clean builds on the pinned host;
  it is not inferred from flags alone.
- The binaries are unsigned. SmartScreen or antivirus reputation, independent
  clean-Windows-machine certification, installer/update behavior, and external-user
  testing require separate validation before making the corresponding claim; none
  is implied by publication of an unsigned, hash-verifiable portable build.
- This narrow runtime intentionally cannot open arbitrary music/video formats
  and cannot make network requests.
- Synthetic capability execution is not a judgment of restoration quality.
  Real captures remain read-only and require separate evidence and audition.
