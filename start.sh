#!/usr/bin/env bash
# Start the LiteLLM proxy. Foreground by default (Ctrl+C stops it); add --background
# to detach (PID file + log in the private state directory; stop with ./gateway.sh stop).
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/scripts/lib.sh"
kgw_venv_python
exec "$KGW_PY" "$KGW_ROOT/scripts/manage.py" start "$@"
