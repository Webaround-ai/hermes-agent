#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <archive.tar.zst>" >&2
  echo "Place release.json beside the archive." >&2
  exit 2
fi
script_dir=$(cd "$(dirname "$0")" && pwd)
archive=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
python3 "$script_dir/release.py" check-archive "$archive"
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
