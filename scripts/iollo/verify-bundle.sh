#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <archive.tar.zst> [public-key-file-or-base64-key]" >&2
  echo "Place release.json (and signatures for signed releases) beside the archive." >&2
  exit 2
fi
script_dir=$(cd "$(dirname "$0")" && pwd)
archive=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
pubkey=${2:-}
signature=$(python3 "$script_dir/release.py" check-archive "$archive")
if [[ -n "$pubkey" || "$signature" != UNSIGNED ]]; then
  [[ -n "$pubkey" ]] || { echo "Signed release: a trusted public key is required" >&2; exit 1; }
  [[ "$signature" != UNSIGNED ]] || { echo "Public key supplied: refusing an unsigned release" >&2; exit 1; }
  key_arg=-P
  [[ ! -f "$pubkey" ]] || key_arg=-p
  minisign -Vm "$archive" "$key_arg" "$pubkey" -x "$(dirname "$archive")/$signature"
  minisign -Vm "$(dirname "$archive")/release.json" "$key_arg" "$pubkey"
else
  echo "UNSIGNED: checksum integrity only; publisher identity is not verified." >&2
fi
work=$(mktemp -d "${TMPDIR:-/tmp}/iollo-verify.XXXXXX")
trap 'rm -rf "$work"' EXIT
zstd -dq -c "$archive" > "$work/bundle.tar"
python3 "$script_dir/bundle.py" unpack "$archive" "$work/bundle.tar" "$work"
root="$work/$(basename "$archive" .tar.zst)"
mkdir "$work/home" "$work/state"
# Use an empty environment and a different cwd; no developer Python or real user state.
(
  cd "$work"
  env -i HOME="$work/home" HERMES_HOME="$work/state" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
    LANG=C.UTF-8 "$root/bin/hermes" --version
)
echo "Verified and booted $(basename "$archive")"
