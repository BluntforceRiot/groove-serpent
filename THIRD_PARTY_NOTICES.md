# Third-party notices

Groove Serpent depends on or interoperates with the projects and services below. The wheel, source
distribution, and source archive do not bundle external media executables. The separately labeled
Windows portable does bundle the exact minimal media runtime and Python dependencies identified by
its manifest, and the release carries the corresponding media-source archive. Optional network
services are never bundled. Third-party licenses, terms, trademarks, and content policies remain
their own.

## Runtime dependency

### NumPy

[NumPy](https://numpy.org/) is a Python runtime dependency used for numerical audio analysis. A
normal Python installation obtains it as a package dependency. The Windows portable carries the
exact NumPy wheel named by its manifest and retains NumPy's license and bundled-component notices.
NumPy is distributed under the BSD 3-Clause license; its own distribution is authoritative for the
complete notices.

## External executables

### FFmpeg and ffprobe

[FFmpeg](https://ffmpeg.org/) and `ffprobe` provide media decoding, inspection, and encoding. A
normal wheel or source installation discovers compatible executables on `PATH`. The Windows
portable instead carries a narrow, manifest-bound FFmpeg/ffprobe 8.1.2 shared-library build made
for Groove Serpent's exercised formats and filters; it is not a general FFmpeg distribution.

FFmpeg licensing depends on how a particular build was configured. The upstream project explains
its LGPL/GPL licensing and compliance considerations at <https://ffmpeg.org/legal.html>. The
portable runtime manifest records the exact configuration and component hashes, carries the
applicable licenses and notices, and is paired with the complete corresponding media-source
archive. These engineering records are not legal advice or a legal-compliance certification.

Fixed speed correction uses FFmpeg's optional [libsoxr](https://sourceforge.net/projects/soxr/)
resampling engine. External FFmpeg installations must enable it. The Windows portable's media
runtime bundles the manifest-bound libsoxr 0.1.3 shared library and its LGPL license/source
materials.

### Chromaprint and fpcalc

[Chromaprint](https://acoustid.org/chromaprint) provides acoustic fingerprinting. A normal
installation may use the optional external `fpcalc` executable. The Windows portable does not
bundle `fpcalc`; its narrow FFmpeg runtime carries the manifest-bound Chromaprint 1.6.0 shared
library and exposes the FFmpeg Chromaprint muxer instead. Fingerprinting and AcoustID lookup remain
optional; core local analysis and export do not require them.

### Python runtime

The Windows portable carries the official Python Software Foundation Windows embeddable runtime
identified by its manifest. Its license is retained in the portable. Normal wheel and source
installations use the owner's separately installed compatible Python runtime.

## Optional network services

### MusicBrainz

[MusicBrainz](https://musicbrainz.org/) is an optional metadata service. Groove Serpent contacts it
only after an explicit metadata lookup and follows the service's application-identification and
rate-limit expectations.

### Cover Art Archive

[Cover Art Archive](https://coverartarchive.org/) is an optional artwork service. Artwork lookup is
explicit. Individual images may be copyrighted or carry separate rights information; users are
responsible for determining whether they may store, modify, or redistribute retrieved artwork.

### AcoustID

[AcoustID](https://acoustid.org/) is an optional acoustic-fingerprint lookup service. Groove
Serpent sends a locally calculated fingerprint and duration, not the original recording. Use of
the service remains subject to AcoustID's terms and API policies.

## Read-only application discovery

### Audacity

[Audacity](https://www.audacityteam.org/) is not a Groove Serpent runtime dependency and is not
bundled. Groove Serpent performs read-only discovery of a local Audacity installation for
interoperability information; it does not modify Audacity configuration or control Audacity's
audio processing.

Project and product names are used only to identify the relevant dependencies, tools, and services.
This notice does not imply endorsement by their respective maintainers.
