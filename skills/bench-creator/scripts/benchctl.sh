#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -n "${AI_WORK_BENCH_PYTHON:-}" ]; then
  exec "$AI_WORK_BENCH_PYTHON" -B "$SCRIPT_DIR/benchctl.py" "$@"
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 -B "$SCRIPT_DIR/benchctl.py" "$@"
fi

if command -v python >/dev/null 2>&1; then
  exec python -B "$SCRIPT_DIR/benchctl.py" "$@"
fi

echo "Python 3 was not found. Set AI_WORK_BENCH_PYTHON to an absolute Python executable path." >&2
exit 127
