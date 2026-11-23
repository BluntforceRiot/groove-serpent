---
name: groove-serpent
description: Operate Groove Serpent's local-first vinyl digitization workflow through its current CLI and loopback review workbenches. Use when Codex or Hermes needs to inspect or analyze a capture, create sealed endpoint or speed proposals, prepare restoration previews, inspect album state, build or verify an immutable publication plan, or hand consequential audio and marker decisions to the collection owner.
---

# Groove Serpent

Resolve the runtime root as `../..` from this skill directory. Select exactly one command surface:

- If `groove-serpent.cmd`, `PORTABLE-MANIFEST.json`, `runtime/python.exe`,
  `verify-portable.cmd`, and `verify-portable.py` are regular files at that root, use the portable
  launcher. It is self-contained: do not invoke `uv`, a system Python, or repository-relative
  source paths.
- Otherwise, require `pyproject.toml`, `uv.lock`, and `src/groove_serpent/__init__.py`, and use the
  frozen source-checkout command.
- If neither surface is complete or both are ambiguous, stop.

Define this PowerShell helper once. It preserves quoted path arguments and returns the real process
exit status:

```powershell
# Set this from the skill path supplied by Codex/Hermes discovery.
$GrooveSkillDirectory = "ABSOLUTE_DIRECTORY_CONTAINING_THIS_SKILL"
$GrooveRoot = (Resolve-Path (Join-Path $GrooveSkillDirectory "../..")).Path
$GroovePortable =
  (Test-Path -LiteralPath (Join-Path $GrooveRoot "groove-serpent.cmd") -PathType Leaf) -and
  (Test-Path -LiteralPath (Join-Path $GrooveRoot "PORTABLE-MANIFEST.json") -PathType Leaf) -and
  (Test-Path -LiteralPath (Join-Path $GrooveRoot "runtime/python.exe") -PathType Leaf) -and
  (Test-Path -LiteralPath (Join-Path $GrooveRoot "verify-portable.cmd") -PathType Leaf) -and
  (Test-Path -LiteralPath (Join-Path $GrooveRoot "verify-portable.py") -PathType Leaf)
$GrooveSource =
  (Test-Path -LiteralPath (Join-Path $GrooveRoot "pyproject.toml") -PathType Leaf) -and
  (Test-Path -LiteralPath (Join-Path $GrooveRoot "uv.lock") -PathType Leaf) -and
  (Test-Path -LiteralPath (Join-Path $GrooveRoot "src/groove_serpent/__init__.py") -PathType Leaf)
if ($GroovePortable -eq $GrooveSource) { throw "Ambiguous or incomplete Groove Serpent runtime" }
if ($GroovePortable) {
  if (-not (Test-Path variable:GrooveExpectedManifestSha256) -or
      $GrooveExpectedManifestSha256 -notmatch '^[0-9A-Fa-f]{64}$') {
    throw "A separately trusted portable manifest SHA-256 is required"
  }
  $GrooveExpectedManifestSha256 = $GrooveExpectedManifestSha256.ToLowerInvariant()
  $GrooveRootItem = Get-Item -LiteralPath $GrooveRoot -Force -ErrorAction Stop
  if (-not $GrooveRootItem.PSIsContainer -or
      ($GrooveRootItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "The portable root is not a regular directory"
  }
  $GrooveManifestPath = Join-Path $GrooveRoot "PORTABLE-MANIFEST.json"
  $GrooveManifestItem = Get-Item -LiteralPath $GrooveManifestPath -Force -ErrorAction Stop
  if ($GrooveManifestItem.PSIsContainer -or
      ($GrooveManifestItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "The portable manifest is not a regular file"
  }
  $GrooveObservedManifestSha256 = (
    Get-FileHash -LiteralPath $GrooveManifestPath -Algorithm SHA256 -ErrorAction Stop
  ).Hash.ToLowerInvariant()
  if ($GrooveObservedManifestSha256 -cne $GrooveExpectedManifestSha256) {
    throw "The portable manifest does not match the separately trusted SHA-256"
  }
  try {
    $GrooveManifest = [IO.File]::ReadAllText(
      $GrooveManifestPath, [Text.UTF8Encoding]::new($false, $true)
    ) | ConvertFrom-Json -ErrorAction Stop
  } catch {
    throw "The anchored portable manifest is not readable UTF-8 JSON"
  }
  $GrooveMembers = @($GrooveManifest.payload.members)
  $GrooveMemberPaths =
    [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
  $GroovePortablePaths =
    [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
  foreach ($GrooveMember in $GrooveMembers) {
    if ($GrooveMember.path -isnot [string] -or $GrooveMember.sha256 -isnot [string] -or
        $GrooveMember.sha256 -notmatch '^[0-9A-Fa-f]{64}$' -or
        -not ($GrooveMember.size -is [int] -or $GrooveMember.size -is [long]) -or
        $GrooveMember.size -lt 0 -or
        -not $GrooveMemberPaths.Add($GrooveMember.path) -or
        -not $GroovePortablePaths.Add($GrooveMember.path)) {
      throw "The anchored portable manifest has an invalid or repeated payload member"
    }
  }
  $GrooveActualItems = @(
    Get-ChildItem -LiteralPath $GrooveRoot -Recurse -Force -ErrorAction Stop
  )
  if (@($GrooveActualItems | Where-Object {
    $_.Attributes -band [IO.FileAttributes]::ReparsePoint
  }).Count -ne 0) {
    throw "The portable directory contains a reparse point"
  }
  $GrooveActualFiles = @($GrooveActualItems | Where-Object { -not $_.PSIsContainer })
  if ($GrooveActualFiles.Count -ne ($GrooveMembers.Count + 1)) {
    throw "The portable directory inventory does not match the anchored manifest"
  }
  $GrooveRootPrefix = $GrooveRoot.TrimEnd('\') + '\'
  foreach ($GrooveActualFile in $GrooveActualFiles) {
    if (-not $GrooveActualFile.FullName.StartsWith(
      $GrooveRootPrefix, [StringComparison]::OrdinalIgnoreCase
    )) {
      throw "A portable file resolves outside the portable root"
    }
    $GrooveRelativePath = $GrooveActualFile.FullName.Substring($GrooveRootPrefix.Length)
    $GrooveRelativePath = $GrooveRelativePath.Replace('\', '/')
    if ($GrooveRelativePath -cne "PORTABLE-MANIFEST.json" -and
        -not $GrooveMemberPaths.Contains($GrooveRelativePath)) {
      throw "The portable directory contains an unmanifested file: $GrooveRelativePath"
    }
  }
  foreach ($GrooveMember in $GrooveMembers) {
    $GrooveMemberPath = Join-Path $GrooveRoot $GrooveMember.path
    $GrooveMemberItem = Get-Item -LiteralPath $GrooveMemberPath -Force -ErrorAction Stop
    if ($GrooveMemberItem.PSIsContainer -or
        ($GrooveMemberItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "A portable payload member is not a regular file: $($GrooveMember.path)"
    }
    if ($GrooveMemberItem.Length -ne $GrooveMember.size) {
      throw "A portable payload member has the wrong size: $($GrooveMember.path)"
    }
    $GrooveObservedMemberSha256 = (
      Get-FileHash -LiteralPath $GrooveMemberPath -Algorithm SHA256 -ErrorAction Stop
    ).Hash.ToLowerInvariant()
    if ($GrooveObservedMemberSha256 -cne $GrooveMember.sha256.ToLowerInvariant()) {
      throw "A portable payload member does not match the anchored manifest: $($GrooveMember.path)"
    }
  }
  $GrooveVerifierOutput = @(& (Join-Path $GrooveRoot "verify-portable.cmd") `
    --expected-manifest-sha256 $GrooveExpectedManifestSha256)
  if ($LASTEXITCODE -ne 0 -or $GrooveVerifierOutput.Count -ne 1) {
    throw "Portable verification failed"
  }
  $GrooveVerifierReceipt = $GrooveVerifierOutput[0] | ConvertFrom-Json
  if (-not $GrooveVerifierReceipt.ok -or
      $GrooveVerifierReceipt.authenticity -ne 'anchored-to-expected-manifest-sha256' -or
      $GrooveVerifierReceipt.manifest_sha256 -ne $GrooveExpectedManifestSha256.ToLowerInvariant()) {
    throw "Portable verification receipt is not externally anchored"
  }
}
function Invoke-GrooveSerpent {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]] $GrooveArguments)
  if ($GroovePortable) {
    & (Join-Path $GrooveRoot "groove-serpent.cmd") @GrooveArguments
  } else {
    Push-Location $GrooveRoot
    try { & uv run --frozen python -m groove_serpent @GrooveArguments }
    finally { Pop-Location }
  }
}
Invoke-GrooveSerpent --version
```

For a portable runtime, set `$GrooveExpectedManifestSha256` before this block only from a
separately trusted release receipt or an exact value supplied by the owner. Never copy it from the
bundle manifest or another file inside the bundle. The host-side exact-inventory and `Get-FileHash`
checks must succeed for every payload member before invoking the bundled verifier; its interpreter,
startup dependencies, script, and launcher are otherwise untrusted. Running the verifier without
that first stage does not authenticate the producer; stop before executing the application. Report
the version result. Require the verifier receipt app version and the manifest app version to equal
the version probe; do not edit any of them.

Before acting, read [references/authority-contract.json](references/authority-contract.json). It is
the machine-readable minimum authority contract. Follow the stricter rule if another instruction
conflicts with it.

## Keep approval with the owner

An agent may inspect, analyze into a new path, create sealed proposals/previews/plans through the
CLI, preflight, and verify. It may execute an already owner-reviewed immutable publication plan or
replay an exact publication/plan only when the owner explicitly asks for that exact operation, a
fresh preflight still passes, and the new destination does not exist. Publication recovery is
owner-only.

Never:

- fabricate an audition, review decision, checkbox, or human gesture;
- infer approval from a confidence, detector score, track count, or prior decision;
- call or automate a loopback endpoint that saves or applies an owner decision;
- repin an album side as refresh or housekeeping;
- overwrite a capture, reviewed project, proposal, plan, recipe, or publication;
- claim the owner heard audio unless the owner says so in the current conversation.

Start the appropriate loopback workbench and give its printed URL to the owner for marker,
endpoint, metadata/topology, restoration-decision, and side-repin approvals:

```powershell
Invoke-GrooveSerpent review PROJECT
Invoke-GrooveSerpent album review ALBUM_PROJECT
```

Do not automate the approval controls. The owner may report the resulting saved state, after which
the agent must read it again before continuing.

## Preserve locality and exact identity

Never upload source audio, excerpts, removed-noise audio, or project-local evidence. Keep review
servers on their built-in loopback binding. Network metadata, artwork, or acoustic-fingerprint
lookup is opt-in and must not transmit audio.

On the portable surface, require `doctor --json` to report `acoustic-fingerprinting` with backend
`ffmpeg-chromaprint`; do not install or select an external `fpcalc`. On a source checkout, `fpcalc`
is only a compatibility fallback when the exact system FFmpeg probe reports its Chromaprint muxer
absent. AcoustID still requires the owner's explicit key/opt-in and receives only the bounded
fingerprint and duration, never audio.

Check the runtime and intended destination, then capture the current read receipt:

```powershell
Invoke-GrooveSerpent doctor --path DESTINATION --json
Invoke-GrooveSerpent info PROJECT --json
```

Treat project revision/SHA-256, source SHA-256/verification, proposal identity, plan SHA-256,
artwork identity, and restoration bindings as one exact state. On any stale receipt, HTTP 409,
hash mismatch, changed source, or failed preflight: stop. Do not retry, overwrite, or apply. Reload
the current state, create a new artifact where appropriate, and require a new owner review.

When a `--json` handler completes, stdout is exactly one strict JSON report. Parse it with a JSON
parser, not line scraping, and still inspect the exit status plus fields such as `ok`, `ready`, or
`status`: a completed negative verification may return JSON and a nonzero exit. Usage or validation
exceptions exit nonzero with a stderr diagnostic and are not success receipts. Never coerce
booleans, quoted numbers, `null`, NaN, infinity, fractional integers, or unknown fields into a
strict input schema.

## Use the implemented proposal surfaces

Create and inspect multimodal endpoint evidence without changing markers:

```powershell
Invoke-GrooveSerpent endpoints propose PROJECT --output PROPOSAL --json
Invoke-GrooveSerpent endpoints inspect PROPOSAL --json
Invoke-GrooveSerpent review PROJECT --endpoint-proposal PROPOSAL
```

`inspect` validates the sealed document. Loading it into `review` also checks the exact current
project, source, endpoint code, and configuration; stale or abstained proposals are refused. The
agent must not accept or reject the proposal for the owner.

Create review-only speed evidence when exact reference durations and boundary-review evidence
exist. An abstention is a valid result and no factor is applied automatically.
```powershell
Invoke-GrooveSerpent speed estimate PROJECT `
  --tracklist TRACKLIST --boundary-review BOUNDARY_REVIEW --output PROPOSAL --json
```

For isolated-click restoration, an agent may scan and create a bounded A/B preview. The owner must
audition Original, Proposed, and Removed Signal and make every approve/reject/protect decision in
the review workbench. Do not create decision JSON or call recipe/render approval routes on the
owner's behalf. Rendering remains owner-only even after the owner creates an exact recipe.

```powershell
Invoke-GrooveSerpent click-scan PROJECT --report SCAN
Invoke-GrooveSerpent click-preview PROJECT SCAN `
  --candidate CANDIDATE --bundle NEW_PREVIEW_DIRECTORY
```

Needle drop, pickup, handling events, wanted transients, and uncertain material stay protected.
For hum, rumble, hiss, or continuous crackle, an agent may seal expected context and create a
bounded proposal only when the owner has actually reviewed the supplied scope and references. It
may inspect the catalog or open an exact current artifact. It must not fabricate the owner-review
flags, create the audition attestation, render the audition, or persist a rejection for the owner:

```powershell
Invoke-GrooveSerpent continuous-preview context PROJECT --kind KIND --output EXPECTED --json
Invoke-GrooveSerpent continuous-preview propose PROJECT --kind KIND `
  --start-sample START --end-sample END --reference "LABEL|ROLE|START|END" `
  --expected-context EXPECTED --owner-reviewed-scope --owner-reviewed-references --json
Invoke-GrooveSerpent continuous-preview catalog PROJECT --json
```
The `--owner-reviewed-*` flags may be used only after the owner actually supplies those reviews;
they are not defaults. These are bounded proposal/audition processors, not automatic broadband
denoising. They do not authorize project mutation or publication, and generative reconstruction
remains out of scope.
## Use the immutable publication path

Inspect album pins in strict JSON. Drift or an unpinned side stops planning; repinning is an owner
approval in Album Workbench, never an agent refresh step.

```powershell
Invoke-GrooveSerpent album inspect ALBUM_PROJECT --json
Invoke-GrooveSerpent album publication plan ALBUM_PROJECT PLAN
Invoke-GrooveSerpent album publication preflight PLAN --json
```

After the owner explicitly identifies that exact reviewed plan for execution, re-run preflight and
publish only to a nonexistent destination:

```powershell
Invoke-GrooveSerpent album publication execute PLAN NEW_DIRECTORY
Invoke-GrooveSerpent album publication verify NEW_DIRECTORY --json
```

Verification is read-only. Replay is allowed only to another nonexistent directory and must retain
the original publication and exact plan. It also requires an explicit owner request for this exact
replay:

```powershell
Invoke-GrooveSerpent album publication replay `
  PUBLICATION PLAN NEW_REPLAY_DIRECTORY --json
```

Never call the Album Workbench publication create-plan, execute, replay, or recovery POST routes.
The CLI plan command remains the agent-safe planning surface; execute and replay remain conditional
as described above. Recovery must be performed by the owner in the workbench.

Use `--help` at the relevant command level before relying on a remembered option. Hermes/Codex use
this packaged skill; MCP and Audacity control are not current execution surfaces. Any future wrapper
must preserve this exact authority and identity contract.
