#!/usr/bin/env bash
# Remove what setup created. Asks before deleting the venv, settings or credentials;
# credentials are kept unless you choose to delete them. The repository is never deleted.
#   ./uninstall.sh [--remove-venv] [--remove-config] [--remove-credentials] [--yes]
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/scripts/lib.sh"
if [ -x "$KGW_ROOT/.venv/bin/python" ]; then
  KGW_PY="$KGW_ROOT/.venv/bin/python"
else
  kgw_find_python
fi
# Run from outside the venv so the venv itself can be removed.
KGW_NO_REEXEC=1 exec "$KGW_PY" "$KGW_ROOT/scripts/manage.py" uninstall "$@"
