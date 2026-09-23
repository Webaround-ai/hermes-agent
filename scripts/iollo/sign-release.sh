#!/usr/bin/env bash
# Secrets are passed through environment/stdin, never command arguments or tracing.
set +x
set -euo pipefail
if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 <tag> <artifact-directory> [--require-both]" >&2
  exit 2
fi
script_dir=$(cd "$(dirname "$0")" && pwd)
tag=$1
directory=$2
shift 2
if [[ $# == 1 ]]; then
  [[ $1 == --require-both ]] || exit 2
fi
if [[ -z "${IOLLO_MINISIGN_SECRET_KEY:-}" || -z "${IOLLO_MINISIGN_PASSWORD:-}" ]]; then
  python3 "$script_dir/release.py" create "$tag" "$directory" "$@"
  # A rerun without secrets must not accidentally publish old signatures.
  rm -f "$directory"/*.minisig "$directory/iollo.minisign.pub"
  echo "UNSIGNED: both IOLLO_MINISIGN secrets are required to sign."
  exit 0
fi
umask 077
work=$(mktemp -d "${TMPDIR:-/tmp}/iollo-sign.XXXXXX")
trap 'rm -rf "$work"' EXIT
printf '%s\n' "$IOLLO_MINISIGN_SECRET_KEY" > "$work/secret.key"
unset IOLLO_MINISIGN_SECRET_KEY
printf '%s\n' "$IOLLO_MINISIGN_PASSWORD" | minisign -R -s "$work/secret.key" -p "$work/public.key" >/dev/null
python3 "$script_dir/release.py" create "$tag" "$directory" --signed "$@"
for asset in "$directory"/*.tar.zst "$directory/release.json" "$directory/SHA256SUMS"; do
  printf '%s\n' "$IOLLO_MINISIGN_PASSWORD" | minisign -S -s "$work/secret.key" -m "$asset" \
    -t "Iollo $tag $(basename "$asset")"
  minisign -Vm "$asset" -p "$work/public.key"
done
# Public material only. CI uses it for the same native verification as operators.
cp "$work/public.key" "$directory/iollo.minisign.pub"
