#!/usr/bin/env bash
# Compatible entry point; Python implementation also runs directly on Windows.
set -euo pipefail
command -v python3 >/dev/null || { echo 'python3 not installed' >&2; exit 3; }
exec python3 "$(cd -- "$(dirname -- "$0")" && pwd)/wait_reply.py" "$@"
