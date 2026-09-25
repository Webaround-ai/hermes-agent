#!/usr/bin/env bash
# Writes release.json and SHA256SUMS for the archives in <artifact-directory>.
# Nothing is signed here: the Mac app pins the tarball's SHA-256 and Apple notarization covers the app.
set -euo pipefail
if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <tag> <artifact-directory> [--box-digest sha256:<hex>] [--require-both]" >&2
  exit 2
fi
script_dir=$(cd "$(dirname "$0")" && pwd)
python3 "$script_dir/release.py" create "$@"
