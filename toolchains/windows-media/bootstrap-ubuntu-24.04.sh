#!/usr/bin/bash -p
set -euo pipefail
export PATH=/usr/bin:/bin
export LANG=C
export LC_ALL=C
export TZ=UTC
export DEBIAN_FRONTEND=noninteractive
umask 0022
IFS=$' \t\n'
unset \
  APT_CONFIG BASH_ENV CDPATH CONFIG_SITE DEBCONF_DB_OVERRIDE \
  DEBCONF_DB_REPLACE DPKG_ADMINDIR DPKG_DATADIR DPKG_ROOT ENV GLOBIGNORE \
  http_proxy https_proxy ftp_proxy all_proxy no_proxy \
  HTTP_PROXY HTTPS_PROXY FTP_PROXY ALL_PROXY NO_PROXY \
  LD_LIBRARY_PATH LD_PRELOAD PYTHONHOME PYTHONPATH SUDO_ASKPASS SUDO_COMMAND \
  SUDO_GID SUDO_PROMPT SUDO_UID SUDO_USER

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PACKAGE_FILE="$SCRIPT_DIR/ubuntu-24.04-packages.txt"

if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify the build operating system." >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
  echo "This reference builder requires Ubuntu 24.04; found ${ID:-unknown} ${VERSION_ID:-unknown}." >&2
  exit 1
fi

mapfile -t pinned_packages < <(
  sed -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$PACKAGE_FILE"
)
if ((${#pinned_packages[@]} == 0)); then
  echo "The pinned package inventory is empty." >&2
  exit 1
fi

declare -A pinned_versions=()
while IFS='=' read -r package expected; do
  [[ -n "$package" ]] || continue
  pinned_versions["$package"]="$expected"
done < "$PACKAGE_FILE"

# Updating or installing is privileged. Refuse to cross that boundary unless
# the already-installed package manager and privilege broker are the exact
# root-owned providers pinned by this recipe.
bootstrap_contract=(
  "apt-get|/usr/bin/apt-get|apt"
  "sudo|/usr/bin/sudo|sudo"
)
for contract in "${bootstrap_contract[@]}"; do
  IFS='|' read -r command_name expected_path package <<<"$contract"
  observed="$(command -v -- "$command_name" || true)"
  resolved="$(readlink -f -- "$observed" 2>/dev/null || true)"
  actual="$(dpkg-query -W -f='${Version}' "$package" 2>/dev/null || true)"
  expected="${pinned_versions[$package]:-}"
  mapfile -t owners < <(dpkg-query -S "$resolved" 2>/dev/null || true)
  owner="${owners[0]:-}"
  owner="${owner%%:*}"
  file_owner="$(stat -c '%u:%g' "$resolved" 2>/dev/null || true)"
  file_mode="$(stat -c '%a' "$resolved" 2>/dev/null || true)"
  unsafe_mode=true
  if [[ "$file_mode" =~ ^[0-7]{3,4}$ ]] && \
    (( (8#$file_mode & 0022) == 0 )); then
    unsafe_mode=false
  fi
  if [[ "$resolved" != "$expected_path" || ${#owners[@]} -ne 1 || \
    "$owner" != "$package" || "$file_owner" != "0:0" || \
    "$unsafe_mode" != false || \
    -z "$expected" || "$actual" != "$expected" ]]; then
    echo "Unsafe bootstrap provider: $command_name must be $expected_path from $package=$expected." >&2
    exit 1
  fi
done

/usr/bin/sudo /usr/bin/apt-get update
/usr/bin/sudo DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get install \
  -y --no-install-recommends \
  "${pinned_packages[@]}"

/usr/bin/bash --noprofile --norc -p "$SCRIPT_DIR/verify_build_host.sh"

echo "Pinned Ubuntu 24.04 build-host dependencies are installed."
