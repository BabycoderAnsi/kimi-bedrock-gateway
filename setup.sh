#!/usr/bin/env bash
# One-command installer for Ubuntu and Apple Silicon macOS.
# Safe to re-run: it reuses a healthy virtual environment, keeps existing credentials
# and the proxy master key, and resumes after an interruption. Never runs sudo.
#   ./setup.sh                       interactive
#   ./setup.sh --help                all options (region, classifier, non-interactive ...)
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/scripts/lib.sh"
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  kgw_find_python
  exec "$KGW_PY" "$KGW_ROOT/scripts/manage.py" setup --help
fi
printf '==> Kimi Bedrock Gateway setup (%s %s)\n' "$(uname -s)" "$(uname -m)"
kgw_find_python
exec "$KGW_PY" "$KGW_ROOT/scripts/manage.py" bootstrap -- "$@"
