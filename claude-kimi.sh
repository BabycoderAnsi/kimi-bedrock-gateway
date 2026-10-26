#!/usr/bin/env bash
# Launch Claude Code through the local gateway with Kimi K3 as the model.
# Environment variables are set for this Claude Code process only.
#   ./claude-kimi.sh [claude arguments...]
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/scripts/lib.sh"
kgw_venv_python
exec "$KGW_PY" "$KGW_ROOT/scripts/manage.py" launch-claude -- "$@"
