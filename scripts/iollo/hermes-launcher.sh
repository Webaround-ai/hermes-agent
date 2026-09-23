#!/bin/sh
set -eu
runtime=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
unset PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
PATH="$runtime/bin:$runtime/python/bin:$PATH"
export PATH
if [ -z "${HERMES_HOME:-}" ]; then
  HERMES_HOME=$("$runtime/python/bin/python3" -I -B -c 'from hermes_constants import get_hermes_home; print(get_hermes_home())')
fi
export HERMES_HOME
exec "$runtime/python/bin/python3" -I -B -m hermes_cli.main "$@"
