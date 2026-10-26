#!/usr/bin/env bash
# Replace the Amazon Bedrock bearer token. The token is read without echo, validated
# against the configured model, and only then saved (the old one is kept on failure).
#   ./update-bedrock-key.sh [--restart] [--rollback] [--yes]
set -euo pipefail
# shellcheck source=scripts/lib.sh
. "$(cd "$(dirname "$0")" && pwd)/scripts/lib.sh"
kgw_venv_python
exec "$KGW_PY" "$KGW_ROOT/scripts/manage.py" update-key "$@"
