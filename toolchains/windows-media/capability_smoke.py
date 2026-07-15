#!/usr/bin/env python3
"""Exercise the exact Groove Serpent media paths against one Windows runtime."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, Sequence, cast


REPORT_SCHEMA = "groove-serpent.windows-media-capability-smoke/1"
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
WSL_DISTRO_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGPU2hfN"
    "gA0wYRUdtBIAALgBU09TpG8AAAAASUVORK5CYII="
)
JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoH"
    "BwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/2wBDAQME"
    "BAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU"
    "FBQUFBQUFBQUFBQUFBT/wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEA"
    "AAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIh"
    "MUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
    "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZ"
    "mqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx"
    "8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAV"
    "YnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hp"
    "anN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
    "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDQ"
    "ooor88P5PP/Z"
)


class SmokeFailure(RuntimeError):
    """A required media capability did not execute exactly as promised."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tool_path(path: Path) -> str:
    """Return a path a Windows PE process can open from Windows or WSL."""

    resolved = path.resolve()
    if os.name == "nt":
        return str(resolved)
    distro = os.environ.get("WSL_DISTRO_NAME")
    if distro:
        if WSL_DISTRO_RE.fullmatch(distro) is None:
            raise SmokeFailure("WSL_DISTRO_NAME is not a safe UNC share name.")
        posix = resolved.as_posix()
        if not posix.startswith("/") or "\\" in posix or any(ord(char) < 32 for char in posix):
            raise SmokeFailure("WSL media path cannot be represented as an exact UNC path.")
        relative = posix.removeprefix("/").replace("/", "\\")
        return f"\\\\wsl.localhost\\{distro}\\{relative}"
    return str(resolved)


def _run(
    command: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            list(command),
            input=input_bytes,
            stdin=subprocess.DEVNULL if input_bytes is None else None,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmokeFailure(f"Could not run {Path(command[0]).name}: {exc}") from exc
    if len(completed.stdout) > MAX_CAPTURE_BYTES:
        raise SmokeFailure(f"{Path(command[0]).name} stdout exceeded the smoke bound.")
    if len(completed.stderr) > MAX_CAPTURE_BYTES:
        raise SmokeFailure(f"{Path(command[0]).name} stderr exceeded the smoke bound.")
    if completed.returncode != 0:
        detail = completed.stderr[-8192:].decode("utf-8", errors="replace")
        raise SmokeFailure(
            f"{Path(command[0]).name} returned {completed.returncode}: {detail.strip()}"
        )
    return completed


def _text(command: Sequence[str]) -> str:
    return _run(command).stdout.decode("utf-8", errors="strict")


def _pcm_payload(
    *,
    sample_rate: int,
    frames: int,
    channels: int,
    bits: int,
    byteorder: Literal["little", "big"],
) -> bytes:
    if bits not in {16, 24} or byteorder not in {"little", "big"}:
        raise ValueError("Unsupported synthetic PCM geometry.")
    scale = (1 << (bits - 1)) - 1
    payload = bytearray()
    for frame in range(frames):
        seconds = frame / sample_rate
        frequency = 173.0 + 29.0 * ((frame // sample_rate) % 7)
        value = 0.42 * math.sin(2.0 * math.pi * frequency * seconds)
        value += 0.13 * math.sin(2.0 * math.pi * 997.0 * seconds)
        integer = max(-scale - 1, min(scale, round(scale * value)))
        for channel in range(channels):
            sample = integer if channel % 2 == 0 else -(integer // 2)
            if bits == 16:
                payload.extend(struct.pack("<h" if byteorder == "little" else ">h", sample))
            else:
                if sample < 0:
                    sample += 1 << 24
                payload.extend(sample.to_bytes(3, byteorder=byteorder, signed=False))
    return bytes(payload)


def _write_wav(path: Path, *, sample_rate: int, frames: int, channels: int, bits: int) -> None:
    pcm = _pcm_payload(
        sample_rate=sample_rate,
        frames=frames,
        channels=channels,
        bits=bits,
        byteorder="little",
    )
    block_align = channels * bits // 8
    fmt = struct.pack(
        "<HHIIHH",
        1,
        channels,
        sample_rate,
        sample_rate * block_align,
        block_align,
        bits,
    )
    riff_size = 4 + 8 + len(fmt) + 8 + len(pcm)
    path.write_bytes(
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVEfmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def _extended_rate(sample_rate: int) -> bytes:
    exponent = sample_rate.bit_length() - 1
    mantissa = round(sample_rate * (1 << (63 - exponent)))
    return struct.pack(">HQ", 16383 + exponent, mantissa)


def _aiff_chunk(name: bytes, payload: bytes) -> bytes:
    padded = payload + (b"\x00" if len(payload) % 2 else b"")
    return name + struct.pack(">I", len(payload)) + padded


def _write_aiff(path: Path, *, sample_rate: int, frames: int, channels: int, bits: int) -> None:
    pcm = _pcm_payload(
        sample_rate=sample_rate,
        frames=frames,
        channels=channels,
        bits=bits,
        byteorder="big",
    )
    comm = struct.pack(">hIh", channels, frames, bits) + _extended_rate(sample_rate)
    chunks = _aiff_chunk(b"COMM", comm) + _aiff_chunk(b"SSND", struct.pack(">II", 0, 0) + pcm)
    path.write_bytes(b"FORM" + struct.pack(">I", 4 + len(chunks)) + b"AIFF" + chunks)


def _probe(ffprobe: Path, path: Path) -> dict[str, Any]:
    completed = _run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            (
                "stream=index,codec_name,codec_type,sample_rate,channels,"
                "bits_per_raw_sample,bits_per_sample,sample_fmt,duration_ts,time_base:"
                "stream_disposition=attached_pic"
            ),
            "-of",
            "json",
            _tool_path(path),
        ]
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"ffprobe returned invalid JSON for {path.name}.") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise SmokeFailure(f"ffprobe returned no stream inventory for {path.name}.")
    return cast(dict[str, Any], payload)


def _audio_stream(probe: dict[str, Any]) -> dict[str, Any]:
    streams = [
        cast(dict[str, Any], item)
        for item in cast(list[Any], probe["streams"])
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if len(streams) != 1:
        raise SmokeFailure("Expected exactly one audio stream.")
    return streams[0]


def _presentation_samples(stream: dict[str, Any]) -> int:
    rate = int(stream["sample_rate"])
    value = Fraction(int(stream["duration_ts"])) * Fraction(str(stream["time_base"]))
    samples = value * rate
    if samples.denominator != 1:
        raise SmokeFailure("Presentation duration does not map to a whole sample count.")
    return samples.numerator


def _decode_raw(ffmpeg: Path, path: Path, codec: str, raw_format: str) -> bytes:
    return _run(
        [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            _tool_path(path),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-c:a",
            codec,
            "-f",
            raw_format,
            "pipe:1",
        ]
    ).stdout


def _assert_geometry(
    ffprobe: Path,
    path: Path,
    *,
    codec: str,
    sample_rate: int,
    channels: int,
    frames: int,
) -> dict[str, Any]:
    probe = _probe(ffprobe, path)
    stream = _audio_stream(probe)
    actual = (
        str(stream.get("codec_name")),
        int(stream.get("sample_rate", 0)),
        int(stream.get("channels", 0)),
        _presentation_samples(stream),
    )
    expected = (codec, sample_rate, channels, frames)
    if actual != expected:
        raise SmokeFailure(f"Unexpected geometry for {path.name}: {actual!r} != {expected!r}")
    return stream


def _encode_flac(ffmpeg: Path, source: Path, output: Path, *, sample_format: str) -> None:
    _run(
        [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            _tool_path(source),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-map_metadata",
            "-1",
            "-c:a",
            "flac",
            "-compression_level",
            "8",
            "-sample_fmt",
            sample_format,
            _tool_path(output),
        ]
    )


def _cover_export(
    ffmpeg: Path,
    ffprobe: Path,
    source: Path,
    cover: Path,
    output: Path,
    *,
    output_format: str,
    expected_frames: int,
) -> dict[str, Any]:
    command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-n",
        "-i",
        _tool_path(source),
        "-i",
        _tool_path(cover),
        "-map",
        "0:a:0",
        "-map",
        "1:v:0",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-af",
        ("atrim=start_sample=4410:end_sample=220500,asettb=expr=1/44100,asetpts=N"),
        "-ar",
        "44100",
        "-metadata",
        "title=Groove Serpent capability smoke",
    ]
    if output_format == "flac":
        command.extend(["-c:a", "flac", "-compression_level", "8", "-sample_fmt", "s16"])
    else:
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "256k",
                "-movflags",
                "+faststart",
                "-movie_timescale",
                "44100",
                "-f",
                "ipod",
            ]
        )
    command.extend(
        [
            "-c:v",
            "copy",
            "-disposition:v:0",
            "attached_pic",
            "-metadata:s:v:0",
            "title=Album cover",
            "-metadata:s:v:0",
            "comment=Cover (front)",
            _tool_path(output),
        ]
    )
    _run(command)
    probe = _probe(ffprobe, output)
    stream = _audio_stream(probe)
    expected_codec = "flac" if output_format == "flac" else "aac"
    if stream.get("codec_name") != expected_codec:
        raise SmokeFailure(f"Wrong codec in {output.name}.")
    if _presentation_samples(stream) != expected_frames:
        raise SmokeFailure(f"Wrong presentation length in {output.name}.")
    pictures = [
        item
        for item in probe["streams"]
        if item.get("codec_type") == "video"
        and item.get("disposition", {}).get("attached_pic") == 1
    ]
    if len(pictures) != 1:
        raise SmokeFailure(f"Expected one attached cover in {output.name}.")
    expected_picture_codec = "png" if cover.suffix == ".png" else "mjpeg"
    if pictures[0].get("codec_name") != expected_picture_codec:
        raise SmokeFailure(f"Wrong attached-cover codec in {output.name}.")
    extracted = _run(
        [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            _tool_path(output),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-c:v",
            "copy",
            "-f",
            "image2pipe",
            "pipe:1",
        ]
    ).stdout
    if extracted != cover.read_bytes():
        raise SmokeFailure(f"Cover bytes changed in {output.name}.")
    _run(
        [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            _tool_path(output),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-f",
            "null",
            "-",
        ]
    )
    return {
        "artwork_codec": expected_picture_codec,
        "audio_codec": expected_codec,
        "output_sha256": _sha256_file(output),
        "presentation_samples": expected_frames,
    }


def run_smoke(runtime_dir: Path, work_dir: Path) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    work_dir = work_dir.resolve()
    ffmpeg = runtime_dir / "ffmpeg.exe"
    ffprobe = runtime_dir / "ffprobe.exe"
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise SmokeFailure("The runtime must contain ffmpeg.exe and ffprobe.exe.")
    if not work_dir.name.startswith("groove-serpent-windows-media-smoke-"):
        raise SmokeFailure("The disposable work directory has an unsafe name.")
    if (
        work_dir == runtime_dir
        or runtime_dir in work_dir.parents
        or work_dir in runtime_dir.parents
    ):
        raise SmokeFailure("The disposable work directory overlaps the runtime.")
    if work_dir.exists() and (work_dir.is_symlink() or not work_dir.is_dir()):
        raise SmokeFailure("The disposable work path is not an ordinary directory.")
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    version = _text([str(ffmpeg), "-nostdin", "-version"])
    probe_version = _text([str(ffprobe), "-version"])
    buildconf = _text([str(ffmpeg), "-nostdin", "-buildconf"])
    license_text = _text([str(ffmpeg), "-nostdin", "-L"])
    if not version.startswith("ffmpeg version 8.1.2"):
        raise SmokeFailure("Unexpected FFmpeg version.")
    if not probe_version.startswith("ffprobe version 8.1.2"):
        raise SmokeFailure("Unexpected ffprobe version.")
    for forbidden in ("--enable-gpl", "--enable-nonfree", "--enable-version3"):
        if forbidden in buildconf:
            raise SmokeFailure(f"Forbidden FFmpeg configure option present: {forbidden}")
    if "--disable-network" not in buildconf or "--disable-static" not in buildconf:
        raise SmokeFailure("The build configuration does not prove the intended profile.")
    if "--enable-shared" not in buildconf or "--enable-chromaprint" not in buildconf:
        raise SmokeFailure("The required shared Chromaprint profile is absent.")
    if "--enable-libsoxr" not in buildconf or "--enable-zlib" not in buildconf:
        raise SmokeFailure("The required libsoxr/zlib profile is absent.")
    if "gnu lesser general public" not in license_text.casefold():
        raise SmokeFailure("FFmpeg did not report the expected LGPL license text.")

    protocols = _text([str(ffmpeg), "-nostdin", "-hide_banner", "-protocols"])
    protocol_sets: dict[str, set[str]] = {"input": set(), "output": set()}
    protocol_section: str | None = None
    for line in protocols.splitlines():
        value = line.strip()
        if value == "Input:":
            protocol_section = "input"
        elif value == "Output:":
            protocol_section = "output"
        elif protocol_section is not None and value:
            protocol_sets[protocol_section].add(value)
    protocol_names = protocol_sets["input"] | protocol_sets["output"]
    forbidden_protocols = {"http", "https", "tcp", "tls", "udp"}
    if forbidden_protocols.intersection(protocol_names):
        raise SmokeFailure("A network protocol is enabled in the minimal runtime.")
    if not {"file", "pipe"}.issubset(protocol_names):
        raise SmokeFailure("The file and pipe protocols are not both enabled.")

    sources = [
        ("wav16-44100-stereo.wav", "wav", 44100, 2, 16, 44100 * 12),
        ("wav24-96000-mono.wav", "wav", 96000, 1, 24, 96000),
        ("aiff16-48000-mono.aiff", "aiff", 48000, 1, 16, 48000),
        ("aiff24-192000-stereo.aiff", "aiff", 192000, 2, 24, 96000),
    ]
    source_results: list[dict[str, Any]] = []
    for name, container, rate, channels, bits, frames in sources:
        source = work_dir / name
        if container == "wav":
            _write_wav(
                source,
                sample_rate=rate,
                frames=frames,
                channels=channels,
                bits=bits,
            )
        else:
            _write_aiff(
                source,
                sample_rate=rate,
                frames=frames,
                channels=channels,
                bits=bits,
            )
        expected_codec = f"pcm_s{bits}{'le' if container == 'wav' else 'be'}"
        _assert_geometry(
            ffprobe,
            source,
            codec=expected_codec,
            sample_rate=rate,
            channels=channels,
            frames=frames,
        )
        decoded = _decode_raw(ffmpeg, source, "pcm_s32le", "s32le")
        if len(decoded) != frames * channels * 4:
            raise SmokeFailure(f"Decoded PCM length changed for {name}.")
        source_results.append(
            {
                "bits": bits,
                "channels": channels,
                "codec": expected_codec,
                "container": container,
                "decoded_s32le_sha256": _sha256_bytes(decoded),
                "frames": frames,
                "sample_rate": rate,
                "source_sha256": _sha256_file(source),
            }
        )

    wav16 = work_dir / sources[0][0]
    wav24 = work_dir / sources[1][0]
    flac16 = work_dir / "lossless-16.flac"
    flac24 = work_dir / "lossless-24.flac"
    _encode_flac(ffmpeg, wav16, flac16, sample_format="s16")
    _encode_flac(ffmpeg, wav24, flac24, sample_format="s32")
    _assert_geometry(
        ffprobe,
        flac16,
        codec="flac",
        sample_rate=44100,
        channels=2,
        frames=44100 * 12,
    )
    _assert_geometry(
        ffprobe,
        flac24,
        codec="flac",
        sample_rate=96000,
        channels=1,
        frames=96000,
    )
    wav16_pcm = _decode_raw(ffmpeg, wav16, "pcm_s16le", "s16le")
    flac16_pcm = _decode_raw(ffmpeg, flac16, "pcm_s16le", "s16le")
    wav24_pcm = _decode_raw(ffmpeg, wav24, "pcm_s32le", "s32le")
    flac24_pcm = _decode_raw(ffmpeg, flac24, "pcm_s32le", "s32le")
    if wav16_pcm != flac16_pcm or wav24_pcm != flac24_pcm:
        raise SmokeFailure("FLAC did not preserve exact decoded PCM.")

    exact_range_command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        _tool_path(flac16),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        "atrim=start_sample=12345:end_sample=54321,asettb=expr=1/44100,asetpts=N",
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]
    exact_range_a = _run(exact_range_command).stdout
    exact_range_b = _run(exact_range_command).stdout
    expected_range_bytes = (54321 - 12345) * 2 * 4
    if len(exact_range_a) != expected_range_bytes or exact_range_a != exact_range_b:
        raise SmokeFailure("Exact bounded float-PCM decode was not deterministic.")

    _run(
        [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "0.02",
            "-af",
            "aresample=44100:resampler=soxr",
            "-f",
            "null",
            "-",
        ]
    )
    corrected = work_dir / "speed-corrected.flac"
    _run(
        [
            str(ffmpeg),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            _tool_path(flac16),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-af",
            (
                "asetrate=45841,"
                "aresample=44100:resampler=soxr:precision=33:cutoff=0.99,"
                "atrim=start_sample=0:end_sample=509097,"
                "asettb=expr=1/44100,asetpts=N"
            ),
            "-ar",
            "44100",
            "-c:a",
            "flac",
            "-compression_level",
            "8",
            "-sample_fmt",
            "s16",
            _tool_path(corrected),
        ]
    )
    corrected_stream = _audio_stream(_probe(ffprobe, corrected))
    corrected_samples = _presentation_samples(corrected_stream)
    if corrected_samples != 509097:
        raise SmokeFailure("The exact libsoxr speed-correction trim length changed.")

    png = work_dir / "cover.png"
    jpeg = work_dir / "cover.jpg"
    png.write_bytes(PNG_BYTES)
    jpeg.write_bytes(JPEG_BYTES)
    cover_results: dict[str, Any] = {}
    for cover in (png, jpeg):
        for output_format in ("flac", "m4a"):
            key = f"{cover.suffix[1:]}-{output_format}"
            output = work_dir / f"cover-{key}.{output_format}"
            cover_results[key] = _cover_export(
                ffmpeg,
                ffprobe,
                flac16,
                cover,
                output,
                output_format=output_format,
                expected_frames=216090,
            )

    fingerprint_command = [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        _tool_path(flac16),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        "atrim=start_sample=0:end_sample=529200,asetpts=PTS-STARTPTS",
        "-ar",
        "11025",
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-fp_format",
        "base64",
        "-f",
        "chromaprint",
        "pipe:1",
    ]
    fingerprint_a = _run(fingerprint_command).stdout.strip()
    fingerprint_b = _run(fingerprint_command).stdout.strip()
    if fingerprint_a != fingerprint_b or len(fingerprint_a) < 32:
        raise SmokeFailure("Chromaprint output was empty or nondeterministic.")
    try:
        fingerprint_a.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SmokeFailure("Chromaprint output was not ASCII base64.") from exc

    return {
        "schema": REPORT_SCHEMA,
        "result": "passed",
        "scope": "synthetic capability execution; not a real-capture quality judgment",
        "runtime": {
            "ffmpeg": version.splitlines()[0],
            "ffprobe": probe_version.splitlines()[0],
            "build_configuration_sha256": _sha256_bytes(buildconf.encode("utf-8")),
            "license_output_sha256": _sha256_bytes(license_text.encode("utf-8")),
            "network_protocols_absent": sorted(forbidden_protocols),
            "protocols": {direction: sorted(names) for direction, names in protocol_sets.items()},
        },
        "source_decode": source_results,
        "lossless": {
            "flac_16_sha256": _sha256_file(flac16),
            "flac_24_sha256": _sha256_file(flac24),
            "pcm_16_sha256": _sha256_bytes(flac16_pcm),
            "pcm_24_sha256": _sha256_bytes(flac24_pcm),
            "pcm_equal": True,
        },
        "exact_float_pcm": {
            "bytes": len(exact_range_a),
            "sha256": _sha256_bytes(exact_range_a),
            "repeat_equal": True,
        },
        "speed_correction": {
            "filter": "asetrate + libsoxr precision=33 cutoff=0.99",
            "presentation_samples": corrected_samples,
            "sha256": _sha256_file(corrected),
        },
        "cover_art_stream_copy": cover_results,
        "chromaprint": {
            "algorithm": 1,
            "backend": "FFmpeg chromaprint muxer + Chromaprint 1.6.0 kissfft",
            "bytes": len(fingerprint_a),
            "repeat_equal": True,
            "sha256": _sha256_bytes(fingerprint_a),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run_smoke(args.runtime_dir, args.work_dir)
        encoded = (json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode(
            "utf-8"
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes(encoded)
    except (OSError, SmokeFailure, ValueError) as exc:
        print(f"capability smoke failed: {exc}", file=sys.stderr)
        return 1
    print(f"Capability smoke passed: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
