#!/usr/bin/bash -p

CLEAN_ENVIRONMENT=false
if [[ "${GROOVE_SERPENT_WINDOWS_MEDIA_CLEAN_ENV:-}" == "1" ]] && \
  /usr/bin/python3.12 -I -B - <<'PY'
import os

allowed = {
    "DIST_DIR",
    "GROOVE_SERPENT_WINDOWS_MEDIA_CLEAN_ENV",
    "GROOVE_SERPENT_LAUNCH_AUTHORITY_SHA256",
    "JOBS",
    "LANG",
    "LC_ALL",
    "PATH",
    "PWD",
    "SHLVL",
    "TZ",
    "WSL_DISTRO_NAME",
    "_",
}
required = {
    "GROOVE_SERPENT_WINDOWS_MEDIA_CLEAN_ENV": "1",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "TZ": "UTC",
}
clean = set(os.environ) <= allowed and all(
    os.environ.get(name) == value for name, value in required.items()
)
raise SystemExit(0 if clean else 1)
PY
then
  CLEAN_ENVIRONMENT=true
fi
if [[ "$CLEAN_ENVIRONMENT" != true ]]; then
  DIST_DIR_INPUT="${DIST_DIR-}"
  JOBS_INPUT="${JOBS-}"
  WSL_DISTRO_NAME_INPUT="${WSL_DISTRO_NAME-}"
  AUTHORITY_SHA256_INPUT="${GROOVE_SERPENT_LAUNCH_AUTHORITY_SHA256-}"
  builtin exec /usr/bin/env -i \
    DIST_DIR="$DIST_DIR_INPUT" \
    JOBS="$JOBS_INPUT" \
    WSL_DISTRO_NAME="$WSL_DISTRO_NAME_INPUT" \
    GROOVE_SERPENT_LAUNCH_AUTHORITY_SHA256="$AUTHORITY_SHA256_INPUT" \
    PATH=/usr/bin:/bin \
    LANG=C \
    LC_ALL=C \
    TZ=UTC \
    GROOVE_SERPENT_WINDOWS_MEDIA_CLEAN_ENV=1 \
    /usr/bin/bash --noprofile --norc -p "${BASH_SOURCE[0]}" "$@"
fi

set -euo pipefail
export PATH=/usr/bin:/bin
export LANG=C
export LC_ALL=C
export TZ=UTC
export ZIPOPT=
umask 0022
while IFS= read -r inherited_function; do
  builtin unset -f "$inherited_function"
done < <(builtin compgen -A function)
builtin unset CLEAN_ENVIRONMENT GROOVE_SERPENT_WINDOWS_MEDIA_CLEAN_ENV
builtin unset \
  AR AS BASH_ENV CC CFLAGS CMAKE_GENERATOR CMAKE_GENERATOR_PLATFORM \
  CMAKE_GENERATOR_TOOLSET CMAKE_PREFIX_PATH CMAKE_TOOLCHAIN_FILE \
  CMAKE_BUILD_PARALLEL_LEVEL CMAKE_INSTALL_MODE CMAKE_PROJECT_INCLUDE \
  CMAKE_PROJECT_INCLUDE_BEFORE CMAKE_USER_MAKE_RULES_OVERRIDE \
  CMAKE_USER_MAKE_RULES_OVERRIDE_C CMAKE_USER_MAKE_RULES_OVERRIDE_CXX \
  COMPILER_PATH CONFIG_SITE CONFIG_SHELL CPATH CPPFLAGS CPLUS_INCLUDE_PATH \
  CXX CXXFLAGS DESTDIR ENV \
  GCC_EXEC_PREFIX GLOBIGNORE GREP_OPTIONS LD LDFLAGS LD_LIBRARY_PATH LD_PRELOAD \
  LIBRARY_PATH LIBS GNUMAKEFLAGS MAKEFILES MAKEFLAGS MFLAGS NINJA_STATUS NM \
  OBJCOPY OBJDUMP PKG_CONFIG_LIBDIR PKG_CONFIG_PATH PKG_CONFIG_SYSROOT_DIR \
  POSIXLY_CORRECT PYTHONHOME PYTHONINSPECT PYTHONPATH PYTHONSTARTUP \
  PYTHONUSERBASE RANLIB RC SHELL STRIP WINDRES

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
readonly WORK_ROOT="/tmp/groove-serpent-windows-media-v1"
readonly SOURCE_DATE_EPOCH="1781664539"
DIST_DIR="${DIST_DIR:-}"
readonly JOBS="${JOBS:-$(nproc)}"
readonly DOWNLOADS="$WORK_ROOT/downloads"
readonly SOURCES="$WORK_ROOT/sources"
readonly BUILDS="$WORK_ROOT/build"
readonly PREFIX="$WORK_ROOT/prefix"
readonly LOGS="$WORK_ROOT/logs"
readonly RUNTIME_STAGE="$WORK_ROOT/stage/runtime"
readonly SOURCE_STAGE="$WORK_ROOT/stage/source"
readonly RECIPE_SNAPSHOT="$WORK_ROOT/recipe-authority"
readonly RUNTIME_ARCHIVE_NAME="groove-serpent-windows-media-8.1.2-x86_64.zip"
readonly SOURCE_ARCHIVE_NAME="groove-serpent-windows-media-8.1.2-corresponding-source.zip"
WORK_ROOT_IDENTITY=""
PUBLISH_STAGE=""
RECIPE_AUTHORITY=""

fail() {
  echo "windows-media build failed: $*" >&2
  exit 1
}

fetch_exact() {
  local url="$1"
  local output="$2"
  local expected_sha256="$3"
  curl --disable --fail --location --proto '=https' --tlsv1.2 --retry 3 \
    --output "$output" "$url"
  local actual_sha256
  actual_sha256="$(sha256sum "$output" | awk '{print $1}')"
  [[ "$actual_sha256" == "$expected_sha256" ]] || {
    fail "SHA-256 mismatch for $(basename "$output"): $actual_sha256"
  }
}

verify_signature() {
  local archive="$1"
  local signature="$2"
  local key="$3"
  local expected_fingerprint="$4"
  local label="$5"
  local home="$WORK_ROOT/gnupg-$label"
  local record="$LOGS/$label-signature-verification.txt"
  mkdir -m 0700 "$home"
  GNUPGHOME="$home" gpg --batch --import "$key" >/dev/null 2>&1
  local fingerprint
  fingerprint="$(
    GNUPGHOME="$home" gpg --batch --with-colons --fingerprint |
      awk -F: '$1 == "fpr" {print $10; exit}'
  )"
  [[ "$fingerprint" == "$expected_fingerprint" ]] || {
    fail "$label signing-key fingerprint mismatch: $fingerprint"
  }
  local status
  status="$(
    GNUPGHOME="$home" gpg --batch --status-fd 1 \
      --verify "$signature" "$archive" 2>"$LOGS/$label-gpg-diagnostic.txt"
  )"
  grep -Fq "[GNUPG:] VALIDSIG $expected_fingerprint " <<<"$status" || {
    fail "$label detached signature did not produce the pinned VALIDSIG."
  }
  {
    echo "archive=$(basename "$archive")"
    echo "signature=$(basename "$signature")"
    echo "signing_fingerprint=$expected_fingerprint"
    grep -F "[GNUPG:] VALIDSIG $expected_fingerprint " <<<"$status"
  } > "$record"
}

extract_safe() {
  local archive="$1"
  local expected_root="$2"
  /usr/bin/python3.12 -I -B - "$archive" "$SOURCES" "$expected_root" <<'PY'
import sys
import tarfile
import posixpath
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1])
destination = Path(sys.argv[2])
expected_root = sys.argv[3]
with tarfile.open(archive, "r:*") as bundle:
    members = bundle.getmembers()
    if not members:
        raise SystemExit(f"empty source archive: {archive.name}")
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SystemExit(f"unsafe source member: {member.name!r}")
        if path.parts[0] != expected_root:
            raise SystemExit(f"unexpected source root: {member.name!r}")
        if member.issym() or member.islnk():
            target = PurePosixPath(member.linkname)
            if target.is_absolute():
                raise SystemExit(f"unsafe source link: {member.name!r}")
            if member.issym():
                resolved = posixpath.normpath(
                    posixpath.join(posixpath.dirname(member.name), member.linkname)
                )
            else:
                resolved = posixpath.normpath(member.linkname)
            resolved_path = PurePosixPath(resolved)
            if (
                not resolved_path.parts
                or resolved_path.parts[0] != expected_root
                or resolved == ".."
                or resolved.startswith("../")
            ):
                raise SystemExit(f"unsafe source link: {member.name!r}")
    bundle.extractall(destination, members=members, filter="data")
PY
}

copy_runtime_file() {
  local name="$1"
  install -m 0755 "$PREFIX/bin/$name" "$RUNTIME_STAGE/$name"
}

deterministic_zip() {
  local source_dir="$1"
  local output="$2"
  [[ ! -e "$output" ]] || fail "Refusing to replace existing artifact: $output"
  find "$source_dir" -type f -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
  (
    cd "$source_dir"
    TZ=UTC LC_ALL=C find . -type f -printf '%P\n' | sort |
      TZ=UTC zip -X -q "$output" -@
  )
}

normalize_staged_modes() {
  find "$RUNTIME_STAGE" "$SOURCE_STAGE" -type d \
    -exec /usr/bin/chmod 0755 {} +
  find "$RUNTIME_STAGE" "$SOURCE_STAGE" -type f \
    -exec /usr/bin/chmod 0644 {} +
  local binary
  for binary in \
    ffmpeg.exe ffprobe.exe avcodec-62.dll avdevice-62.dll avfilter-11.dll \
    avformat-62.dll avutil-60.dll swresample-6.dll libchromaprint.dll libsoxr.dll; do
    /usr/bin/chmod 0755 "$RUNTIME_STAGE/$binary"
  done
}

create_publication_stage() {
  PUBLISH_STAGE="$(
    /usr/bin/python3.12 -I -B "$RECIPE_SNAPSHOT/verify_artifact.py" \
      --create-publication-stage "$DIST_DIR"
  )" || fail "Could not create or capability-probe the publication stage."
  [[ -n "$PUBLISH_STAGE" && -d "$PUBLISH_STAGE" && ! -L "$PUBLISH_STAGE" ]] || {
    fail "Publication-stage helper returned an invalid directory."
  }
}

verify_recipe_snapshot() {
  /usr/bin/python3.12 -I -B "$RECIPE_SNAPSHOT/verify_artifact.py" \
    --verify-recipe-snapshot "$RECIPE_SNAPSHOT" "$RECIPE_AUTHORITY" || {
    fail "The private recipe authority snapshot changed after binding."
  }
}

clean_work_root_contents() {
  local item
  for item in \
    "$DOWNLOADS" "$SOURCES" "$BUILDS" "$PREFIX" "$LOGS" \
    "$WORK_ROOT/stage" "$RECIPE_SNAPSHOT" \
    "$WORK_ROOT/gnupg-ffmpeg" "$WORK_ROOT/gnupg-zlib" \
    "$WORK_ROOT/groove-serpent-windows-media-smoke-synthetic"; do
    if [[ -e "$item" || -L "$item" ]]; then
      rm -rf --one-file-system -- "$item" || return 1
    fi
  done
  if find "$WORK_ROOT" -mindepth 1 -print -quit | grep -q .; then
    return 1
  fi
}

acquire_work_root() {
  local descriptor_identity
  local metadata
  if ! mkdir -m 0700 -- "$WORK_ROOT" 2>/dev/null; then
    [[ -d "$WORK_ROOT" && ! -L "$WORK_ROOT" ]] || {
      fail "The deterministic work root is not a plain directory."
    }
  fi
  metadata="$(stat -c '%u' -- "$WORK_ROOT" 2>/dev/null || true)"
  [[ "$metadata" == "$EUID" ]] || {
    fail "The deterministic work root must be owned by this user."
  }
  exec 9<"$WORK_ROOT" || fail "Could not open the deterministic work root."
  /usr/bin/flock --exclusive --nonblock 9 || {
    fail "Another Windows media build already owns the deterministic work root."
  }
  /usr/bin/chmod 0700 "$WORK_ROOT"
  WORK_ROOT_IDENTITY="$(stat -c '%d:%i' -- "$WORK_ROOT")"
  descriptor_identity="$(stat -L -c '%d:%i' -- "/proc/$$/fd/9")"
  metadata="$(stat -c '%u:%a' -- "$WORK_ROOT")"
  [[ "$descriptor_identity" == "$WORK_ROOT_IDENTITY" && \
        "$metadata" == "$EUID:700" ]] || {
    fail "The deterministic work root changed while its build lock was acquired."
  }
  clean_work_root_contents || {
    fail "The deterministic work root contains unexpected or unremovable evidence."
  }
}

cleanup_build() {
  local status=$?
  local current_identity
  local descriptor_identity
  trap - EXIT
  if [[ -n "$PUBLISH_STAGE" ]]; then
    if [[ -e "$PUBLISH_STAGE" || -L "$PUBLISH_STAGE" ]]; then
      echo "Publication stage retained for inspection: $PUBLISH_STAGE" >&2
    elif [[ -e "$DIST_DIR" || -L "$DIST_DIR" ]]; then
      if /usr/bin/python3.12 -I -B "$RECIPE_SNAPSHOT/verify_artifact.py" \
        --verify-publication-directory "$DIST_DIR"; then
        echo "Publication exists but its pre-commit identity can no longer be proven: $DIST_DIR" >&2
      else
        echo "Publication may have committed at: $DIST_DIR" >&2
      fi
    fi
  fi
  if [[ -n "$WORK_ROOT_IDENTITY" ]]; then
    current_identity="$(stat -c '%d:%i' -- "$WORK_ROOT" 2>/dev/null || true)"
    descriptor_identity="$(stat -L -c '%d:%i' -- "/proc/$$/fd/9" 2>/dev/null || true)"
    if [[ -L "$WORK_ROOT" || ! -d "$WORK_ROOT" || \
          "$current_identity" != "$WORK_ROOT_IDENTITY" || \
          "$descriptor_identity" != "$WORK_ROOT_IDENTITY" ]]; then
      echo "Work-root cleanup lost ownership; evidence was preserved." >&2
      status=1
    elif ! clean_work_root_contents; then
      echo "Work-root cleanup retained unexpected or unremovable evidence." >&2
      status=1
    fi
  fi
  exit "$status"
}

[[ "$WORK_ROOT" == "/tmp/groove-serpent-windows-media-v1" ]] || {
  fail "Internal work-root safety assertion failed."
}
[[ -n "$DIST_DIR" ]] || {
  fail "Set DIST_DIR to a new, absent absolute directory on a supported filesystem."
}
[[ "$DIST_DIR" == /* && "$DIST_DIR" != "/" ]] || {
  fail "DIST_DIR must be an absolute directory other than the filesystem root."
}
[[ ! -e "$DIST_DIR" && ! -L "$DIST_DIR" ]] || {
  fail "DIST_DIR already exists; whole-directory publication never replaces it."
}
readonly DIST_DIR
readonly LAUNCH_AUTHORITY_SHA256="${GROOVE_SERPENT_LAUNCH_AUTHORITY_SHA256:-}"
builtin unset GROOVE_SERPENT_LAUNCH_AUTHORITY_SHA256
[[ "$LAUNCH_AUTHORITY_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
  fail "The supported launcher did not bind the complete toolchain authority."
}
trap cleanup_build EXIT
acquire_work_root
RECIPE_AUTHORITY="$(
  /usr/bin/python3.12 -I -B "$SCRIPT_DIR/verify_artifact.py" \
    --create-recipe-snapshot \
    "$SCRIPT_DIR" "$RECIPE_SNAPSHOT" "$LAUNCH_AUTHORITY_SHA256"
)" || fail "Could not create the private recipe authority snapshot."
readonly RECIPE_AUTHORITY
verify_recipe_snapshot
/usr/bin/bash --noprofile --norc -p "$RECIPE_SNAPSHOT/verify_build_host.sh"
verify_recipe_snapshot
/usr/bin/python3.12 -I -B "$RECIPE_SNAPSHOT/verify_artifact.py" \
  --verify-build-layout "$DIST_DIR" "$WORK_ROOT" || {
  fail "DIST_DIR overlaps the deterministic build work root."
}
create_publication_stage

mkdir -p "$DOWNLOADS" "$SOURCES" "$BUILDS" "$PREFIX" "$LOGS"
mkdir -p "$RUNTIME_STAGE/LICENSES" "$SOURCE_STAGE/inputs"

fetch_exact \
  "https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz" \
  "$DOWNLOADS/ffmpeg-8.1.2.tar.xz" \
  "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
fetch_exact \
  "https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz.asc" \
  "$DOWNLOADS/ffmpeg-8.1.2.tar.xz.asc" \
  "0a0963fccd70597838073f3e31b20f4a4d8cc2b5e577472c9a5a1f22624246f8"
fetch_exact \
  "https://github.com/acoustid/chromaprint/releases/download/v1.6.0/chromaprint-1.6.0.tar.gz" \
  "$DOWNLOADS/chromaprint-1.6.0.tar.gz" \
  "9d33482e56a1389a37a0d6742c376139fa43e3b8a63d29003222b93db2cb40da"
fetch_exact \
  "https://downloads.sourceforge.net/project/soxr/soxr-0.1.3-Source.tar.xz" \
  "$DOWNLOADS/soxr-0.1.3-Source.tar.xz" \
  "b111c15fdc8c029989330ff559184198c161100a59312f5dc19ddeb9b5a15889"
fetch_exact \
  "https://zlib.net/zlib-1.3.2.tar.xz" \
  "$DOWNLOADS/zlib-1.3.2.tar.xz" \
  "d7a0654783a4da529d1bb793b7ad9c3318020af77667bcae35f95d0e42a792f3"
fetch_exact \
  "https://zlib.net/zlib-1.3.2.tar.xz.asc" \
  "$DOWNLOADS/zlib-1.3.2.tar.xz.asc" \
  "03ce710347e2f84fa7ed0a6ae6a93467b08031a3022fc296da40220a83b96667"

verify_recipe_snapshot
verify_signature \
  "$DOWNLOADS/ffmpeg-8.1.2.tar.xz" \
  "$DOWNLOADS/ffmpeg-8.1.2.tar.xz.asc" \
  "$RECIPE_SNAPSHOT/keys/ffmpeg-release-signing-key.asc" \
  "FCF986EA15E6E293A5644F10B4322F04D67658D8" \
  "ffmpeg"
verify_signature \
  "$DOWNLOADS/zlib-1.3.2.tar.xz" \
  "$DOWNLOADS/zlib-1.3.2.tar.xz.asc" \
  "$RECIPE_SNAPSHOT/keys/zlib-mark-adler.asc" \
  "5ED46A6721D365587791E2AA783FCD8E58BCAFBA" \
  "zlib"
verify_recipe_snapshot

extract_safe "$DOWNLOADS/ffmpeg-8.1.2.tar.xz" "ffmpeg-8.1.2"
extract_safe "$DOWNLOADS/chromaprint-1.6.0.tar.gz" "chromaprint-1.6.0"
extract_safe "$DOWNLOADS/soxr-0.1.3-Source.tar.xz" "soxr-0.1.3-Source"
extract_safe "$DOWNLOADS/zlib-1.3.2.tar.xz" "zlib-1.3.2"

verify_recipe_snapshot
{
  echo "os=Ubuntu 24.04"
  while IFS= read -r pinned; do
    [[ -n "$pinned" ]] && echo "apt:$pinned"
  done < "$RECIPE_SNAPSHOT/ubuntu-24.04-packages.txt"
  echo "cc=$(x86_64-w64-mingw32-gcc-win32 --version | sed -n '1p')"
  echo "cxx=$(x86_64-w64-mingw32-g++-win32 --version | sed -n '1p')"
  echo "binutils=$(x86_64-w64-mingw32-ld --version | sed -n '1p')"
  echo "cmake=$(cmake --version | sed -n '1p')"
  echo "ninja=$(ninja --version)"
  echo "source_date_epoch=$SOURCE_DATE_EPOCH"
} > "$LOGS/BUILD-ENVIRONMENT.txt"
verify_recipe_snapshot

export SOURCE_DATE_EPOCH
readonly PREFIX_MAP=(
  "-ffile-prefix-map=$WORK_ROOT=/usr/src/groove-serpent-windows-media"
  "-fdebug-prefix-map=$WORK_ROOT=/usr/src/groove-serpent-windows-media"
)
readonly C_RELEASE_FLAGS="-O2 -DNDEBUG ${PREFIX_MAP[*]}"
readonly CXX_RELEASE_FLAGS="-O2 -DNDEBUG ${PREFIX_MAP[*]}"
readonly SHARED_LINK_FLAGS="-Wl,--no-insert-timestamp -static-libgcc"

cmake -S "$SOURCES/zlib-1.3.2" -B "$BUILDS/zlib" -G Ninja \
  -DCMAKE_SYSTEM_NAME=Windows \
  -DCMAKE_SYSTEM_PROCESSOR=x86_64 \
  -DCMAKE_C_COMPILER=x86_64-w64-mingw32-gcc-win32 \
  -DCMAKE_RC_COMPILER=x86_64-w64-mingw32-windres \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_FLAGS_RELEASE="$C_RELEASE_FLAGS" \
  -DZLIB_BUILD_TESTING=OFF \
  -DZLIB_BUILD_SHARED=OFF \
  -DZLIB_BUILD_STATIC=ON \
  -DZLIB_INSTALL=ON \
  >"$LOGS/zlib-configure.log"
cmake --build "$BUILDS/zlib" --parallel "$JOBS" >"$LOGS/zlib-build.log"
cmake --install "$BUILDS/zlib" >"$LOGS/zlib-install.log"
ln -s libzs.a "$PREFIX/lib/libz.a"

cmake -S "$SOURCES/soxr-0.1.3-Source" -B "$BUILDS/soxr" -G Ninja \
  -DCMAKE_SYSTEM_NAME=Windows \
  -DCMAKE_SYSTEM_PROCESSOR=x86_64 \
  -DCMAKE_C_COMPILER=x86_64-w64-mingw32-gcc-win32 \
  -DCMAKE_RC_COMPILER=x86_64-w64-mingw32-windres \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_FLAGS_RELEASE="$C_RELEASE_FLAGS" \
  -DCMAKE_SHARED_LINKER_FLAGS="$SHARED_LINK_FLAGS" \
  -DBUILD_SHARED_LIBS=ON \
  -DBUILD_TESTS=OFF \
  -DBUILD_EXAMPLES=OFF \
  -DWITH_OPENMP=OFF \
  -DWITH_LSR_BINDINGS=OFF \
  -DWITH_DEV_TRACE=OFF \
  >"$LOGS/soxr-configure.log"
cmake --build "$BUILDS/soxr" --parallel "$JOBS" >"$LOGS/soxr-build.log"
cmake --install "$BUILDS/soxr" >"$LOGS/soxr-install.log"

cmake -S "$SOURCES/chromaprint-1.6.0" -B "$BUILDS/chromaprint" -G Ninja \
  -DCMAKE_SYSTEM_NAME=Windows \
  -DCMAKE_SYSTEM_PROCESSOR=x86_64 \
  -DCMAKE_C_COMPILER=x86_64-w64-mingw32-gcc-win32 \
  -DCMAKE_CXX_COMPILER=x86_64-w64-mingw32-g++-win32 \
  -DCMAKE_RC_COMPILER=x86_64-w64-mingw32-windres \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_FLAGS_RELEASE="$C_RELEASE_FLAGS" \
  -DCMAKE_CXX_FLAGS_RELEASE="$CXX_RELEASE_FLAGS" \
  -DCMAKE_SHARED_LINKER_FLAGS="-Wl,--no-insert-timestamp -static-libgcc -static-libstdc++" \
  -DBUILD_SHARED_LIBS=ON \
  -DBUILD_TESTS=OFF \
  -DBUILD_TOOLS=OFF \
  -DFFT_LIB=kissfft \
  -DAUDIO_PROCESSOR_LIB=none \
  -DUSE_INTERNAL_AVRESAMPLE=ON \
  >"$LOGS/chromaprint-configure.log"
cmake --build "$BUILDS/chromaprint" --parallel "$JOBS" \
  >"$LOGS/chromaprint-build.log"
cmake --install "$BUILDS/chromaprint" >"$LOGS/chromaprint-install.log"

readonly FFMPEG_ARGS=(
  "--prefix=$PREFIX"
  "--arch=x86_64"
  "--target-os=mingw32"
  "--cross-prefix=x86_64-w64-mingw32-"
  "--cc=x86_64-w64-mingw32-gcc-win32"
  "--cxx=x86_64-w64-mingw32-g++-win32"
  "--pkg-config=pkg-config"
  "--enable-cross-compile"
  "--disable-autodetect"
  "--disable-everything"
  "--disable-static"
  "--enable-shared"
  "--enable-ffmpeg"
  "--enable-ffprobe"
  "--enable-avcodec"
  "--enable-avformat"
  "--enable-avfilter"
  "--enable-avdevice"
  "--enable-swresample"
  "--disable-swscale"
  "--disable-network"
  "--disable-doc"
  "--disable-debug"
  "--disable-htmlpages"
  "--disable-manpages"
  "--disable-podpages"
  "--disable-txtpages"
  "--disable-iconv"
  "--enable-zlib"
  "--disable-bzlib"
  "--disable-lzma"
  "--disable-schannel"
  "--disable-securetransport"
  "--disable-vulkan"
  "--disable-xlib"
  "--disable-sdl2"
  "--disable-x86asm"
  "--enable-libsoxr"
  "--enable-chromaprint"
  "--enable-protocol=file,pipe"
  "--enable-indev=lavfi"
  "--enable-demuxer=aiff,flac,wav,mov,image2,pcm_s16le,pcm_s32le"
  "--enable-muxer=flac,ipod,chromaprint,image2pipe,null,pcm_s16le,pcm_s32le,pcm_f32le"
  "--enable-decoder=aac,flac,pcm_s16le,pcm_s24le,pcm_s16be,pcm_s24be,pcm_s32le,pcm_f32le,pcm_u8,png,mjpeg"
  "--enable-encoder=aac,flac,pcm_s16le,pcm_s32le,pcm_f32le"
  "--enable-parser=aac,flac,png,mjpeg"
  "--enable-bsf=aac_adtstoasc"
  "--enable-filter=abuffer,abuffersink,aformat,anull,anullsrc,aresample,asetpts,asetrate,asettb,atrim"
  "--extra-cflags=-I$PREFIX/include -O2 ${PREFIX_MAP[*]}"
  "--extra-ldflags=-L$PREFIX/lib -Wl,--no-insert-timestamp -static-libgcc"
)
mkdir -p "$BUILDS/ffmpeg"
(
  cd "$BUILDS/ffmpeg"
  export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
  export PKG_CONFIG_PATH=""
  "$SOURCES/ffmpeg-8.1.2/configure" "${FFMPEG_ARGS[@]}" \
    >"$LOGS/ffmpeg-configure-summary.txt"
  make -j"$JOBS" >"$LOGS/ffmpeg-build.log"
  make install >"$LOGS/ffmpeg-install.log"
)

grep -q '^#define CONFIG_GPL 0$' "$BUILDS/ffmpeg/config.h" || fail "GPL enabled"
grep -q '^#define CONFIG_NONFREE 0$' "$BUILDS/ffmpeg/config.h" || fail "nonfree enabled"
grep -q '^#define CONFIG_VERSION3 0$' "$BUILDS/ffmpeg/config.h" || fail "v3 enabled"
grep -q '^#define CONFIG_NETWORK 0$' "$BUILDS/ffmpeg/config.h" || fail "network enabled"
grep -q '^#define CONFIG_STATIC 0$' "$BUILDS/ffmpeg/config.h" || fail "static FFmpeg enabled"
grep -q '^#define CONFIG_SHARED 1$' "$BUILDS/ffmpeg/config.h" || fail "shared disabled"
grep -Fq 'License: LGPL version 2.1 or later' "$LOGS/ffmpeg-configure-summary.txt" || {
  fail "FFmpeg configure did not report LGPL-2.1-or-later."
}

for binary in \
  ffmpeg.exe ffprobe.exe avcodec-62.dll avdevice-62.dll avfilter-11.dll \
  avformat-62.dll avutil-60.dll swresample-6.dll libchromaprint.dll libsoxr.dll; do
  copy_runtime_file "$binary"
done

install -m 0644 "$SOURCES/ffmpeg-8.1.2/LICENSE.md" \
  "$RUNTIME_STAGE/LICENSES/FFmpeg-LICENSE.md"
install -m 0644 "$SOURCES/ffmpeg-8.1.2/COPYING.LGPLv2.1" \
  "$RUNTIME_STAGE/LICENSES/FFmpeg-COPYING.LGPLv2.1"
install -m 0644 "$SOURCES/chromaprint-1.6.0/LICENSE.md" \
  "$RUNTIME_STAGE/LICENSES/Chromaprint-LICENSE.md"
install -m 0644 "$SOURCES/chromaprint-1.6.0/src/3rdparty/kissfft/COPYING" \
  "$RUNTIME_STAGE/LICENSES/KissFFT-COPYING"
install -m 0644 \
  "$SOURCES/chromaprint-1.6.0/src/3rdparty/kissfft/LICENSES/BSD-3-Clause" \
  "$RUNTIME_STAGE/LICENSES/KissFFT-BSD-3-Clause.txt"
install -m 0644 "$SOURCES/soxr-0.1.3-Source/LICENCE" \
  "$RUNTIME_STAGE/LICENSES/libsoxr-LICENCE"
install -m 0644 "$SOURCES/soxr-0.1.3-Source/COPYING.LGPL" \
  "$RUNTIME_STAGE/LICENSES/libsoxr-COPYING.LGPL"
install -m 0644 "$SOURCES/zlib-1.3.2/LICENSE" \
  "$RUNTIME_STAGE/LICENSES/zlib-LICENSE"
install -m 0644 /usr/share/doc/gcc-mingw-w64-base/copyright \
  "$RUNTIME_STAGE/LICENSES/GCC-MinGW-RUNTIME-NOTICE.txt"
install -m 0644 /usr/share/common-licenses/GPL-3 \
  "$RUNTIME_STAGE/LICENSES/GPL-3.0.txt"
install -m 0644 /usr/share/doc/mingw-w64-x86-64-dev/copyright \
  "$RUNTIME_STAGE/LICENSES/MinGW-w64-NOTICE.txt"
install -m 0644 "$LOGS/ffmpeg-signature-verification.txt" \
  "$RUNTIME_STAGE/FFMPEG-SIGNATURE-VERIFICATION.txt"
install -m 0644 "$LOGS/zlib-signature-verification.txt" \
  "$RUNTIME_STAGE/ZLIB-SIGNATURE-VERIFICATION.txt"
install -m 0644 "$LOGS/BUILD-ENVIRONMENT.txt" \
  "$RUNTIME_STAGE/BUILD-ENVIRONMENT.txt"
{
  echo "FFmpeg 8.1.2 configure arguments:"
  printf '%s\n' "${FFMPEG_ARGS[@]}"
} > "$RUNTIME_STAGE/FFMPEG-CONFIGURE.txt"
cat > "$RUNTIME_STAGE/README.txt" <<EOF
Groove Serpent minimal Windows x64 media runtime

This directory contains a narrow FFmpeg 8.1.2 shared-library build for
Groove Serpent. It has no network protocols and enables only the capture,
PCM, FLAC, AAC/M4A, artwork, libsoxr, and Chromaprint paths exercised by the
application. Read BUILD-MANIFEST.json and CAPABILITY-SMOKE.json before use.

This evidence bundle is not legal advice or a legal-compliance certification.
This product includes software based in part on the work of the Independent
JPEG Group.
The exact source archive distributed alongside this runtime is:
$SOURCE_ARCHIVE_NAME
EOF

verify_recipe_snapshot
/usr/bin/python3.12 -I -B "$RECIPE_SNAPSHOT/capability_smoke.py" \
  --runtime-dir "$RUNTIME_STAGE" \
  --work-dir "$WORK_ROOT/groove-serpent-windows-media-smoke-synthetic" \
  --report "$RUNTIME_STAGE/CAPABILITY-SMOKE.json"
/usr/bin/python3.12 -I -B "$RECIPE_SNAPSHOT/make_manifest.py" \
  --runtime-dir "$RUNTIME_STAGE" \
  --configure-file "$RUNTIME_STAGE/FFMPEG-CONFIGURE.txt" \
  --environment-file "$RUNTIME_STAGE/BUILD-ENVIRONMENT.txt" \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --output "$RUNTIME_STAGE/BUILD-MANIFEST.json"
verify_recipe_snapshot
(
  cd "$RUNTIME_STAGE"
  # SHA256SUMS is explicitly excluded from the input inventory.
  # shellcheck disable=SC2094
  find . -type f ! -name SHA256SUMS -printf '%P\0' | sort -z |
    xargs -0 sha256sum > SHA256SUMS
)

for input in \
  chromaprint-1.6.0.tar.gz ffmpeg-8.1.2.tar.xz ffmpeg-8.1.2.tar.xz.asc \
  soxr-0.1.3-Source.tar.xz zlib-1.3.2.tar.xz zlib-1.3.2.tar.xz.asc; do
  install -m 0644 "$DOWNLOADS/$input" "$SOURCE_STAGE/inputs/$input"
done
mkdir -p "$SOURCE_STAGE/recipe/keys"
verify_recipe_snapshot
install -m 0644 "$RECIPE_SNAPSHOT/keys/ffmpeg-release-signing-key.asc" \
  "$SOURCE_STAGE/recipe/keys/ffmpeg-release-signing-key.asc"
install -m 0644 "$RECIPE_SNAPSHOT/keys/zlib-mark-adler.asc" \
  "$SOURCE_STAGE/recipe/keys/zlib-mark-adler.asc"
for recipe_file in \
  README.md bootstrap-ubuntu-24.04.sh build.py build.sh capability_smoke.py \
  make_manifest.py ubuntu-24.04-packages.txt verify_artifact.py \
  verify_build_host.sh; do
  install -m 0644 \
    "$RECIPE_SNAPSHOT/$recipe_file" "$SOURCE_STAGE/recipe/$recipe_file"
done
{
  cd "$SOURCE_STAGE"
  find inputs recipe -type f -printf '%p\0' | sort -z |
    xargs -0 sha256sum > SHA256SUMS
}
verify_recipe_snapshot

normalize_staged_modes

readonly STAGED_RUNTIME_ARCHIVE="$PUBLISH_STAGE/$RUNTIME_ARCHIVE_NAME"
readonly STAGED_SOURCE_ARCHIVE="$PUBLISH_STAGE/$SOURCE_ARCHIVE_NAME"

deterministic_zip "$RUNTIME_STAGE" "$STAGED_RUNTIME_ARCHIVE"
deterministic_zip "$SOURCE_STAGE" "$STAGED_SOURCE_ARCHIVE"
STAGED_RUNTIME_SHA256="$(/usr/bin/sha256sum "$STAGED_RUNTIME_ARCHIVE" | awk '{print $1}')"
STAGED_SOURCE_SHA256="$(/usr/bin/sha256sum "$STAGED_SOURCE_ARCHIVE" | awk '{print $1}')"
readonly STAGED_RUNTIME_SHA256 STAGED_SOURCE_SHA256
verify_recipe_snapshot
/usr/bin/python3.12 -I -B "$RECIPE_SNAPSHOT/verify_artifact.py" \
  --runtime-zip "$STAGED_RUNTIME_ARCHIVE" \
  --source-zip "$STAGED_SOURCE_ARCHIVE" \
  --runtime-sha256 "$STAGED_RUNTIME_SHA256" \
  --source-sha256 "$STAGED_SOURCE_SHA256" \
  --verify-signatures \
  --execute-smoke \
  > "$LOGS/final-artifact-verification.json"
/usr/bin/printf '%s  %s\n%s  %s\n' \
  "$STAGED_RUNTIME_SHA256" "$RUNTIME_ARCHIVE_NAME" \
  "$STAGED_SOURCE_SHA256" "$SOURCE_ARCHIVE_NAME" \
  > "$PUBLISH_STAGE/SHA256SUMS"
/usr/bin/chmod 0644 \
  "$STAGED_RUNTIME_ARCHIVE" "$STAGED_SOURCE_ARCHIVE" "$PUBLISH_STAGE/SHA256SUMS"
/usr/bin/chmod 0755 "$PUBLISH_STAGE"
(
  cd "$PUBLISH_STAGE"
  /usr/bin/sha256sum --check --strict --status SHA256SUMS
) || fail "Staged archives changed after their final verified digests were saved."
verify_recipe_snapshot
/usr/bin/python3.12 -I -B "$RECIPE_SNAPSHOT/verify_artifact.py" \
  --publish-directory-no-replace \
  "$PUBLISH_STAGE" "$DIST_DIR" \
  "$STAGED_RUNTIME_SHA256" "$STAGED_SOURCE_SHA256"
PUBLISH_STAGE=""

echo "Built runtime: $DIST_DIR/$RUNTIME_ARCHIVE_NAME"
echo "Built exact source: $DIST_DIR/$SOURCE_ARCHIVE_NAME"
cat "$DIST_DIR/SHA256SUMS"
