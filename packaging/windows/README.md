# Windows portable-delivery foundation

This is a verified builder contract for a genuinely self-contained Windows x64
directory. It does not wrap or call a system Python installation. The bundle has
an isolated Windows embeddable Python runtime, exact application and dependency
wheels, and the exact auditable Groove Serpent minimal Windows media runtime.

The builder does not download anything. A release operator must acquire every
input separately, record its SHA-256, and pass both path and hash. Inputs are
copied through an open regular-file handle and rejected unless their observed
bytes match. Wheel `RECORD` hashes and metadata are independently checked before
extraction.

## Required inputs

- the built `groove-serpent` wheel and exact version;
- an official Windows x64 embeddable Python ZIP and exact Python version;
- every runtime dependency as a wheel (currently exact NumPy);
- `groove-serpent-windows-media-8.1.2-x86_64.zip` with its exact SHA-256;
- `groove-serpent-windows-media-8.1.2-corresponding-source.zip` with its exact
  SHA-256; the builder validates the pair and carries the source archive inside
  the portable directory;
- Groove Serpent and FFmpeg license texts;
- a version-matched third-party notice;
- the stdlib-only `scripts/verify_windows_portable.py` verifier;
- all three files in `skills/groove-serpent`.

The media pair must have exact, internally consistent manifests and inventories.
Its source archive must contain every source input named by the runtime and the
complete deterministic recipe. The builder reruns the full synthetic capability
suite and requires byte-for-byte equality with the embedded proof. `doctor
--json` must find bundle-local FFmpeg, ffprobe, libsoxr, and Chromaprint. A second
smoke requires Groove Serpent's app fingerprint to equal direct bundled FFmpeg.

## Example invocation

First calculate every digest with `Get-FileHash -Algorithm SHA256`. Then run the
builder with explicit values; placeholders below are intentional:

```powershell
uv run python scripts/build_windows_portable.py `
  --wheel C:\exact-inputs\groove_serpent-VERSION-py3-none-any.whl `
  --wheel-sha256 APP_WHEEL_SHA256 --version VERSION `
  --dependency-wheel numpy NUMPY_VERSION C:\exact-inputs\numpy.whl NUMPY_SHA256 `
  --python-embed C:\exact-inputs\python-embed-amd64.zip `
  --python-embed-sha256 PYTHON_ZIP_SHA256 --python-version PYTHON_VERSION `
  --windows-media-runtime C:\exact-inputs\groove-serpent-windows-media-8.1.2-x86_64.zip `
  --windows-media-runtime-sha256 WINDOWS_MEDIA_RUNTIME_SHA256 `
  --windows-media-corresponding-source `
    C:\exact-inputs\groove-serpent-windows-media-8.1.2-corresponding-source.zip `
  --windows-media-corresponding-source-sha256 WINDOWS_MEDIA_SOURCE_SHA256 `
  --groove-license LICENSE --groove-license-sha256 GROOVE_LICENSE_SHA256 `
  --third-party-notices packaging\windows\THIRD_PARTY_NOTICES.txt `
  --third-party-notices-sha256 THIRD_PARTY_SHA256 `
  --portable-verifier scripts\verify_windows_portable.py `
  --portable-verifier-sha256 PORTABLE_VERIFIER_SHA256 `
  --skill-file SKILL.md skills\groove-serpent\SKILL.md SKILL_SHA256 `
  --skill-file agents/openai.yaml skills\groove-serpent\agents\openai.yaml AGENT_SHA256 `
  --skill-file references/authority-contract.json `
    skills\groove-serpent\references\authority-contract.json AUTHORITY_SHA256 `
  --output-root C:\private-builds\groove-serpent-portable
```

The output name is
`Groove-Serpent-VERSION-windows-x64`. Existing output and staging names are
refused. A fully staged, smoked, and hash-manifested directory is published with
a native Windows new-name rename. The builder fails closed on non-Windows hosts.

Before consequential use, run the packaged read-only verifier. A trusted release
receipt should provide the expected manifest hash:

```powershell
.\verify-portable.cmd --expected-manifest-sha256 EXPECTED_MANIFEST_SHA256
```

It hashes every manifest member and rejects missing, extra, linked, unsafe, or
portable-colliding paths without importing the application or invoking FFmpeg.
Without the separately supplied expected manifest hash, a passing result proves
internal consistency only; it does not establish who produced the directory.

Create the versioned transport ZIP only from an anchored, verified directory:

```powershell
uv run python scripts/package_windows_portable.py `
  --directory C:\private-builds\groove-serpent-portable\Groove-Serpent-VERSION-windows-x64 `
  --output-directory C:\private-builds\deliveries `
  --expected-manifest-sha256 EXPECTED_MANIFEST_SHA256
```

The packager derives `Groove-Serpent-VERSION-windows-x64.zip`, refuses an
existing file, writes fixed ZIP timestamps/modes/order with no comments or
absolute source paths, and stores members without recompression. It verifies
the source both before and after streaming, then reopens and rehashes every
staged and published ZIP member. Repeating the operation into two empty output
directories from the same verified directory must produce identical ZIP bytes.
The packager refuses any directory whose manifest does not bind the exact source
companion at `CORRESPONDING-SOURCE/groove-serpent-windows-media-8.1.2-corresponding-source.zip`.

## Update, rollback, and removal

Updates are side by side: close the running application and start the newer
version directory. Rollback means closing it and starting an older intact
directory. Do not merge, overlay, or partially replace directories.

There is no installer registration, service, registry configuration, automatic
updater, account, or telemetry. To remove a version, close Groove Serpent and
delete only that versioned portable directory. Audio, projects, exports, and
caches outside it are not removed. Inspect before deleting anything.

## Proven and unproven boundaries

The builder proves exact input bytes, safe bounded extraction, packaged browser
assets and skill files, exact app/Python/NumPy versions, bundle-local FFmpeg and
ffprobe identity, exercised libsoxr, an exercised FFmpeg Chromaprint fingerprint,
deterministic payload bytes, an exact member manifest, and new-directory publication
on the machine that ran it. Local fingerprint generation is self-contained; an
owner-supplied AcoustID key and opt-in network lookup are still required to identify
a recording.

It does not by itself prove an independent clean-machine run, Windows SmartScreen
reputation, Authenticode signing, legal compliance, general antivirus compatibility,
or correct behavior on every Windows edition/filesystem. Separate candidate-specific
validation is required before making any corresponding claim; those claims are not
implied by publishing an unsigned, hash-verifiable portable build. The manifest
explicitly records that the output is unsigned.
External `fpcalc` remains a compatibility fallback for source-checkout installations
whose system FFmpeg explicitly lacks its Chromaprint muxer; it is not a portable
bundle dependency. Runtime provenance, carried corresponding source, and
capability proof are still not substitutes for legal review of public distribution.
