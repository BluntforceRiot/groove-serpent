# Groove Serpent 1.0 Windows portable acceptance

> **Status: superseded intermediate evidence — not promotion evidence.** The Linux gate found a
> cross-platform write-lock error translation defect after this portable was built. The source fix
> changes the application wheel, so every hash and result below identifies the prior intermediate
> payload and the portable gate must be replayed after the final blind-review fix set freezes.

Date: 2026-07-13

This report covers the exact Groove Serpent `1.0.0` unsigned Windows x64 portable candidate built
and exercised on native Windows 11 `10.0.26200`. It proves reproducible assembly, anchored
integrity, packaged runtime behavior, deterministic transport, Defender results, fresh extraction,
and removal on this machine. It does not claim Authenticode publisher identity, SmartScreen
reputation, legal certification, or a genuinely clean independent Windows machine.

## Exact inputs

| Input | Bytes | SHA-256 |
| --- | ---: | --- |
| Groove Serpent 1.0 wheel | 597,221 | `735cebe6e7c4d319d6dc4bf804a5ce3845abf9e0af2168a8dd747aa15437efdd` |
| Python 3.13.5 Windows embeddable ZIP | 10,903,542 | `7d2650fd9d1b9d002d4a315d5f354247fd6a44f30517c7ef577b08f57a0fb6d9` |
| NumPy 2.4.6 Windows x64 wheel | 12,318,598 | `c4fc99836233ea196540b17ab0983aff60ed07941751930f5f4d05bc3b3b7359` |
| Minimal Windows media runtime | 2,649,396 | `1956ef640886fbc8f4d5fcb2ee671c623ab9d4b0bf848022798d2a7b2e95afb0` |
| Media corresponding source | 14,736,324 | `3a4bdfc8c13af72ea59c076040ef14e1e24eb6b737b792b10acb7d71d3007822` |
| Groove Serpent license | 11,358 | `c95bae1d1ce0235ecccd3560b772ec1efb97f348a79f0fbe0a634f0c2ccefe2c` |
| Third-party notices | 2,910 | `126aa297197651d09d5f277303b75b205da5943da7dccaeed688a46b3b204030` |
| Packaged verifier | 39,553 | `7fec3ea5b4046e963634593f3337890bf3343ebb348d8d21eaa7c2f12c92aebc` |
| Skill instructions | 11,582 | `adc62876d34ad676678411c33dbbd3ff64dc38df7f298697cd3f8d308dbed3b1` |
| Skill agent metadata | 248 | `54a22a855ded76bdd17148c5015a84a182b3295ad07dd3638b5601374141e865` |
| Skill authority contract | 5,147 | `83b3d6d1f6eb80e2c791a895c02a4f27660db2b33d0134c2099a1ac65dc1078c` |

The builder SHA-256 was
`3142686a85c12f2daa74f04d7f1bfc17f2c80d9c849e42b4556e72572a1519df`; the
transport packager SHA-256 was
`b9ec66c3195f27292e842023dc700495bfee2f1bf2ddaa227e884b1a60a7ebf7`.

## Independent directory builds

Two new, empty local-NTFS output roots were assembled independently from the exact inputs. Both
produced identical 1,074-file inventories and bytes:

| Measurement | Result |
| --- | ---: |
| Payload members bound by manifest | 1,073 |
| Bound payload bytes | 86,483,326 |
| Files including manifest | 1,074 |
| Bytes including manifest | 86,683,276 |
| Portable manifest SHA-256 | `3c12f21515c2a6c290e8a717386f367117671d20c334cea0f52474fd77420b03` |
| Manifest schema | `groove-serpent.windows-portable-manifest/2` |

Each build independently replayed the externally hash-bound verifier before and after directory
publication. The packaged verifier returned `ok: true`, authenticity
`anchored-to-expected-manifest-sha256`, version `1.0.0`, and the exact member/byte totals above.

The manifest records:

- Python 3.13.5 and NumPy 2.4.6;
- FFmpeg/ffprobe 8.1.2 from the bundle-local `tools` directory;
- exercised libsoxr and FFmpeg Chromaprint;
- exact application/direct-FFmpeg fingerprint parity;
- all six packaged web resources readable;
- media capability receipt SHA-256
  `c177ed28acc0120a7bc55ce0ce5d1d6669cd84f2f8a5ca645455c7ce641fbdb3`;
- exact carried corresponding source; and
- unsigned, side-by-side publication with replacement refused.

## Runtime and application proof

- `groove-serpent --version` returned exactly `groove-serpent 1.0.0`.
- `doctor --json` returned `ready: true` with bundle-local FFmpeg, ffprobe, libsoxr, and
  atomic-no-replace checks ready. The optional fingerprint backend was
  `ffmpeg-chromaprint`.
- A standard-library-generated, 40-second, 44.1 kHz stereo 16-bit WAV was analyzed through the
  embedded Python and bundled media tools. The 7,056,044-byte source remained unchanged and the
  result was a schema-4, revision-1, one-track project.
- The packaged review server bound to `127.0.0.1`, returned HTTP 200 from `/api/project`, and
  reported revision 1, one track, project SHA-256
  `f8dd23ff485115d4e97e752105173bb48ed3eef774945bb22b850fc383362e04`, and an exact 32-character
  source-verification receipt for source SHA-256
  `85ee80bc79926041cb6efeb80963d04213c3f0ba97e343fccdeee7321481522e`.
- The review server was stopped; no process or listener remained, and its temporary synthetic
  workspace was removed.
- Focused portable builder, verifier, packager, and media-recipe coverage passed
  `37 passed, 1 platform-dependent symlink skip, 13 subtests passed`.

## Deterministic transport

Two independent package operations produced byte-identical stored ZIP archives:

| Artifact | Members | Bytes | SHA-256 |
| --- | ---: | ---: | --- |
| `Groove-Serpent-1.0.0-windows-x64.zip` | 1,074 | 86,918,722 | `8c3387596447a3f35fa093acf63d3d6fa977e5a0f5f0b06e71753e53c86c9e5f` |

Each transport was reopened and checked against the exact portable manifest and carried
corresponding source. A repeat into an occupied output returned the expected `exists` failure and
left the existing ZIP hash unchanged.

## Defender and signature evidence

Microsoft Defender engine `1.1.26060.3008`, product `4.18.26060.3008`, signature
`1.455.122.0` updated 2026-07-13 07:46:38 local time, with real-time protection enabled:

- scanned the exact ZIP with no matching detections, exit 0, in 34.942 seconds;
- scanned the exact extracted tree with no matching detections, exit 0, in 29.296 seconds; and
- left both the ZIP SHA-256 and portable manifest SHA-256 unchanged.

The embedded `runtime/python.exe` retained a valid signature from the Python Software Foundation.
The bundled FFmpeg/ffprobe binaries are not Authenticode-signed. The CMD launchers are scripts, not
signed PE binaries (`Get-AuthenticodeSignature` returned `UnknownError` with no certificate). The
ZIP carried only its `$DATA` stream and no `Zone.Identifier` because it was built locally.

## Fresh extraction, relocation, and removal

The exact ZIP was extracted to a new random directory. A sanitized process environment removed
developer Python and media tools from `PATH` and redirected profile/cache locations into the
temporary root. The extracted verifier returned the expected manifest, version returned
`1.0.0`, and doctor found every required executable inside the relocated portable directory.
The extracted application directory and isolated profile were then removed automatically.

This is useful same-machine relocation/removal proof. It is not a clean Windows VM or another
physical machine.

## Honest external boundaries

- The release is unsigned and must not be described as SmartScreen-trusted or publisher-verified.
- SmartScreen reputation was not tested: the locally built ZIP had no web-origin mark.
- Windows Sandbox and Hyper-V were unavailable in the non-elevated host session, so a genuinely
  clean supported Windows machine remains external validation.
- Defender results apply only to the exact hashes above and do not predict every antivirus product.
- The carried runtime/source and notices are auditable engineering evidence, not legal advice or
  legal-compliance certification.
- No owner capture was included in or read by the portable build. The synthetic smoke does not
  replace owner audition or player/library acceptance.
