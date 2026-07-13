# Third-party notices

Groove Serpent depends on or interoperates with the projects and services below. Unless a release asset says otherwise, the external executables and optional services listed here are not bundled with Groove Serpent. Their licenses, terms, trademarks, and content policies remain their own.

## Runtime dependency

### NumPy

[NumPy](https://numpy.org/) is a Python runtime dependency used for numerical audio analysis. It is installed as a package dependency and is distributed under its own BSD 3-Clause license. See the NumPy distribution for its complete license and bundled-component notices.

## External executables

### FFmpeg and ffprobe

[FFmpeg](https://ffmpeg.org/) and `ffprobe` are required external executables for media decoding, inspection, and encoding. They are discovered on `PATH` and are not presently bundled by Groove Serpent.

FFmpeg licensing depends on how a particular FFmpeg build was configured. The upstream project explains its LGPL/GPL licensing and compliance considerations at <https://ffmpeg.org/legal.html>. Anyone who later redistributes an FFmpeg binary with Groove Serpent must review that exact build and satisfy its license obligations.

Fixed speed correction uses FFmpeg's optional [libsoxr](https://sourceforge.net/projects/soxr/)
resampling engine. libsoxr is not bundled with Groove Serpent; the selected FFmpeg build must
enable it. libsoxr is distributed under its own LGPL license terms.

### Chromaprint and fpcalc

[Chromaprint](https://acoustid.org/chromaprint) provides the optional external `fpcalc` executable used to calculate acoustic fingerprints for AcoustID lookup. Groove Serpent does not presently bundle it. Fingerprinting and AcoustID lookup are optional; core local analysis and export do not require them.

## Optional network services

### MusicBrainz

[MusicBrainz](https://musicbrainz.org/) is an optional metadata service. Groove Serpent contacts it only after an explicit metadata lookup and follows the service's application-identification and rate-limit expectations.

### Cover Art Archive

[Cover Art Archive](https://coverartarchive.org/) is an optional artwork service. Artwork lookup is explicit. Individual images may be copyrighted or carry separate rights information; users are responsible for determining whether they may store, modify, or redistribute retrieved artwork.

### AcoustID

[AcoustID](https://acoustid.org/) is an optional acoustic-fingerprint lookup service. Groove Serpent sends a locally calculated fingerprint and duration, not the original recording. Use of the service remains subject to AcoustID's terms and API policies.

## Read-only application discovery

### Audacity

[Audacity](https://www.audacityteam.org/) is not a Groove Serpent runtime dependency and is not bundled. Groove Serpent performs read-only discovery of a local Audacity installation for interoperability information; it does not modify Audacity configuration or control Audacity's audio processing.

Project and product names are used only to identify the relevant dependencies, tools, and services. This notice does not imply endorsement by their respective maintainers.
