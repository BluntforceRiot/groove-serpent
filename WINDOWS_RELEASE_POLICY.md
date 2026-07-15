# Windows portable trust, upgrade, and removal policy

Groove Serpent's Windows distribution is a portable application, not a system installer. The final
release receipt must say whether the exact ZIP and launcher are Authenticode-signed. An unsigned
candidate must never be described as signed, trusted by SmartScreen, or publisher-verified.

## Authenticity before first execution

For an unsigned release:

1. Download only from the repository's HTTPS release page.
2. Obtain the ZIP SHA-256 and portable-manifest SHA-256 from the separately published release
   receipt. Do not copy the expected hash from inside the ZIP being checked.
3. Hash the downloaded ZIP with `Get-FileHash -Algorithm SHA256` and compare it exactly.
4. Extract to a new directory, then run:

   ```powershell
   .\verify-portable.cmd --expected-manifest-sha256 EXPECTED_64_HEX_DIGEST
   ```

5. Continue only when the single JSON receipt reports `ok: true`, authenticity
   `anchored-to-expected-manifest-sha256`, and the expected application version.
6. Run `.\groove-serpent.cmd doctor --json` before opening a capture.

The verifier establishes integrity relative to the separately trusted manifest digest. It does not
replace a trusted code-signing certificate. Never disable antivirus, bypass an organization policy,
or treat a SmartScreen warning as proof that a file is either malicious or safe.

## Antivirus and SmartScreen evidence

Every frozen release candidate must record:

- exact ZIP and extracted-tree hashes;
- Windows and Microsoft Defender engine/signature versions;
- Defender scan result for the exact ZIP and extracted directory;
- Authenticode status of launchers and bundled executables.

A valid candidate-specific Authenticode signature is required before claiming publisher identity.
An observed SmartScreen result is required before claiming SmartScreen reputation, and a
launch/removal run on an independent clean supported Windows machine is required before making the
corresponding claim of clean-machine certification. When those external checks are unavailable, the
release receipt must mark them unrun. That does not turn them into an implied pass or prevent
publication of an explicitly unsigned, hash-verifiable portable build whose local acceptance gates
below have passed.

Defender or SmartScreen evidence from an earlier portable build is not proof for changed bytes.
Hosted malware scanning is supplementary and must not receive private captures or project data.

## Side-by-side upgrade and rollback

Upgrades are side-by-side:

1. Keep the previous verified application directory.
2. Verify and extract the new release to a different directory.
3. Back up irreplaceable project and album JSON before an explicit schema migration.
4. Run the new release's doctor and read-only project inspection first.
5. Use only explicit migration commands. Migration backups and receipts stay beside the project.
6. Roll back by closing the new application and launching the retained old application directory.

An old application may refuse a project that was explicitly migrated to a newer schema. Rollback
therefore means restoring the migration's verified backup as a new deliberate operation; it never
means silently rewriting the current project or deleting a migration journal.

## Removal

Close Groove Serpent and delete only its extracted application directory. Project JSON, album JSON,
source captures, exports, artwork, and project-local `.groove-serpent` evidence/cache directories
are user data and are not removed automatically. The cache inspection and cleanup commands retain
live, malformed, unsafe, or uncertain entries rather than guessing that they are disposable.

## Release boundary

An unsigned Windows 1.0 portable may be published when the exact candidate has candidate-specific
reproducible-build and deterministic-ZIP proof, an externally anchored manifest, version and doctor
checks, representative application/runtime proof, fresh-extraction and removal proof on a supported
Windows host, exact ZIP and tree Defender scans, an Authenticode inventory, no-replace behavior, and
an exact release receipt. Its release notes and receipt must identify the build as unsigned and must
state that SmartScreen reputation and independent clean-machine behavior were not certified when
those checks were not run.

A signed, publisher-verified, SmartScreen-trusted, or clean-machine-certified delivery claim has a
stronger boundary: it additionally requires valid candidate-specific signatures and the applicable
observations on an independent clean supported Windows machine. Evidence for either boundary is
bound to the exact bytes and cannot be inherited from an earlier candidate.
