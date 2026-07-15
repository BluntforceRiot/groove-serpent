#!/usr/bin/bash
set -euo pipefail
export PATH=/usr/bin:/bin

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PACKAGE_FILE="$SCRIPT_DIR/ubuntu-24.04-packages.txt"

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
  echo "Reference build requires Ubuntu 24.04." >&2
  exit 1
fi

declare -A pinned_versions=()
while IFS='=' read -r package expected; do
  [[ -n "$package" ]] || continue
  pinned_versions["$package"]="$expected"
  actual="$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null || true)"
  if [[ "$actual" != "$expected" ]]; then
    echo "Pinned package mismatch: $package expected $expected; found ${actual:-missing}." >&2
    exit 1
  fi
done < "$PACKAGE_FILE"

provider_contract=(
  "apt-get|/usr/bin/apt-get|apt"
  "awk|/usr/bin/gawk|gawk"
  "bash|/usr/bin/bash|bash"
  "basename|/usr/bin/basename|coreutils"
  "cat|/usr/bin/cat|coreutils"
  "chmod|/usr/bin/chmod|coreutils"
  "cmake|/usr/bin/cmake|cmake"
  "curl|/usr/bin/curl|curl"
  "dirname|/usr/bin/dirname|coreutils"
  "dpkg-query|/usr/bin/dpkg-query|dpkg"
  "env|/usr/bin/env|coreutils"
  "find|/usr/bin/find|findutils"
  "flock|/usr/bin/flock|util-linux"
  "gpg|/usr/bin/gpg|gpg"
  "grep|/usr/bin/grep|grep"
  "install|/usr/bin/install|coreutils"
  "ln|/usr/bin/ln|coreutils"
  "make|/usr/bin/make|make"
  "mkdir|/usr/bin/mkdir|coreutils"
  "ninja|/usr/bin/ninja|ninja-build"
  "nproc|/usr/bin/nproc|coreutils"
  "pkg-config|/usr/bin/pkgconf|pkgconf-bin"
  "python3|/usr/bin/python3.12|python3.12-minimal"
  "readlink|/usr/bin/readlink|coreutils"
  "rm|/usr/bin/rm|coreutils"
  "sed|/usr/bin/sed|sed"
  "sha256sum|/usr/bin/sha256sum|coreutils"
  "sort|/usr/bin/sort|coreutils"
  "stat|/usr/bin/stat|coreutils"
  "sudo|/usr/bin/sudo|sudo"
  "tar|/usr/bin/tar|tar"
  "touch|/usr/bin/touch|coreutils"
  "xargs|/usr/bin/xargs|findutils"
  "xz|/usr/bin/xz|xz-utils"
  "zip|/usr/bin/zip|zip"
  "x86_64-w64-mingw32-ar|/usr/bin/x86_64-w64-mingw32-ar|binutils-mingw-w64-x86-64"
  "x86_64-w64-mingw32-g++|/usr/bin/x86_64-w64-mingw32-g++-win32|g++-mingw-w64-x86-64-win32"
  "x86_64-w64-mingw32-g++-win32|/usr/bin/x86_64-w64-mingw32-g++-win32|g++-mingw-w64-x86-64-win32"
  "x86_64-w64-mingw32-gcc|/usr/bin/x86_64-w64-mingw32-gcc-win32|gcc-mingw-w64-x86-64-win32"
  "x86_64-w64-mingw32-gcc-win32|/usr/bin/x86_64-w64-mingw32-gcc-win32|gcc-mingw-w64-x86-64-win32"
  "x86_64-w64-mingw32-ld|/usr/bin/x86_64-w64-mingw32-ld|binutils-mingw-w64-x86-64"
  "x86_64-w64-mingw32-objdump|/usr/bin/x86_64-w64-mingw32-objdump|binutils-mingw-w64-x86-64"
  "x86_64-w64-mingw32-ranlib|/usr/bin/x86_64-w64-mingw32-ranlib|binutils-mingw-w64-x86-64"
  "x86_64-w64-mingw32-strip|/usr/bin/x86_64-w64-mingw32-strip|binutils-mingw-w64-x86-64"
  "x86_64-w64-mingw32-windres|/usr/bin/x86_64-w64-mingw32-windres|binutils-mingw-w64-x86-64"
)
for contract in "${provider_contract[@]}"; do
  IFS='|' read -r command_name expected_path expected_package <<<"$contract"
  observed="$(command -v -- "$command_name" || true)"
  resolved="$(readlink -f -- "$observed" 2>/dev/null || true)"
  if [[ "$resolved" != "$expected_path" ]]; then
    echo "Build command provenance mismatch: $command_name expected $expected_path; found ${resolved:-missing}." >&2
    exit 1
  fi
  mapfile -t owners < <(dpkg-query -S "$resolved" 2>/dev/null || true)
  if ((${#owners[@]} != 1)); then
    echo "Build command has ambiguous package ownership: $command_name ($resolved)." >&2
    exit 1
  fi
  owner="${owners[0]:-}"
  owner="${owner%%:*}"
  if [[ "$owner" != "$expected_package" || -z "${pinned_versions[$owner]:-}" ]]; then
    echo "Build command package mismatch: $command_name expected $expected_package; found $owner." >&2
    exit 1
  fi
  file_owner="$(stat -c '%u:%g' "$resolved" 2>/dev/null || true)"
  file_mode="$(stat -c '%a' "$resolved" 2>/dev/null || true)"
  unsafe_mode=true
  if [[ "$file_mode" =~ ^[0-7]{3,4}$ ]] && \
    (( (8#$file_mode & 0022) == 0 )); then
    unsafe_mode=false
  fi
  if [[ "$file_owner" != "0:0" || "$unsafe_mode" != false ]]; then
    echo "Build command is not a root-owned, non-writable provider: $command_name ($resolved)." >&2
    exit 1
  fi
done

echo "Pinned Ubuntu 24.04 build host and absolute command providers verified."
