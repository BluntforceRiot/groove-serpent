# Supported capture profile

Groove Serpent 1.0 uses a deliberately narrow input profile for completed physical-media
captures. A file outside this profile is refused before waveform analysis. Refusal never modifies
the source.

The current profile identifier is `lossless-vinyl-capture-v1`:

- containers and codecs: FLAC/FLAC, WAV/16- or 24-bit little-endian integer PCM, and AIFF/16- or
  24-bit big-endian integer PCM;
- sample rates: 44.1, 48, 88.2, 96, 176.4, or 192 kHz;
- channels: mono or stereo;
- bit depths: 16-bit or 24-bit integer;
- duration: greater than zero and no more than six hours per capture;
- file size: greater than zero and no more than 64 GiB;
- decoded workload estimate: no more than 32 GiB of interleaved integer PCM;
- analysis envelope: no more than two million RMS windows for the selected analysis settings; and
- geometry: FFprobe must establish an exact positive sample count consistent with the reported
  duration and sample rate within two samples.

Lossy files, float PCM, 32-bit integer PCM, more than two channels, unknown bit depth, ambiguous
sample geometry, and unsupported container/codec pairings fail closed. These are support-policy
boundaries, not claims that FFmpeg is technically unable to decode other media.

Analysis remains streaming: Groove Serpent does not load the full-rate capture into memory for its
RMS envelope. The decoded-workload figure describes the amount of source PCM processed, not an
allocation of that size. Later restoration methods may have narrower limits; a method must refuse
the capture rather than silently exceed its documented resource envelope.

The gate is implemented by `groove_serpent.capture_envelope` and is invoked on the verified source
snapshot before analysis decoding begins. Metadata validation is not a decoder proof. Source
snapshot verification, FFmpeg decode success, immutable-source checks, long-capture profiling, and
release-candidate platform evidence remain separate acceptance requirements.
