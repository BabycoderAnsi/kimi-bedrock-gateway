#!/usr/bin/env bash
# Management commands: status | stop | restart | health [--live] | logs | version | doctor
#   | config show|validate | set-region REGION | classifier status|enable|disable
#   | rotate-master-key | scan-secrets | upgrade --litellm X.Y.Z | uninstall
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/scripts/lib.sh"
kgw_venv_python
if [ "$#" -eq 0 ]; then
  exec "$KGW_PY" "$KGW_ROOT/scripts/manage.py" --help
fi
exec "$KGW_PY" "$KGW_ROOT/scripts/manage.py" "$@"
