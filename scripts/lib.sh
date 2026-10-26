# shellcheck shell=bash
# Shared helpers for the Unix wrappers (Ubuntu, Apple Silicon macOS). Sourced, not executed.

KGW_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export KGW_ROOT

kgw_die() {
  printf '[FAIL] %s\n' "$1" >&2
  [ -n "${2:-}" ] && printf '       %s\n' "$2" >&2
  exit 1
}

# Print "major.minor machine" for a Python interpreter, or nothing if it is unusable.
kgw_py_info() {
  "$1" -c 'import sys, platform; print("%d.%d %s" % (sys.version_info[0], sys.version_info[1], platform.machine()))' 2>/dev/null
}

# Find a supported Python (3.11-3.14) and export KGW_PY. Honors $KGW_PYTHON.
kgw_find_python() {
  local os arch candidates cand info ver machine
  os="$(uname -s)"
  arch="$(uname -m)"
  if [ -n "${KGW_PYTHON:-}" ]; then
    candidates="$KGW_PYTHON"
  else
    candidates="python3.13 python3.12 python3.11 python3.14 python3"
    if [ "$os" = "Darwin" ]; then
      # Native Apple Silicon locations first; never prefer Intel Homebrew (/usr/local) on arm64.
      candidates="/opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
/opt/homebrew/bin/python3.14 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 $candidates"
    fi
  fi
  for cand in $candidates; do
    command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ] || continue
    info="$(kgw_py_info "$cand")" || continue
    [ -n "$info" ] || continue
    ver="${info%% *}"
    machine="${info##* }"
    case "$ver" in
      3.11|3.12|3.13|3.14) ;;
      *) continue ;;
    esac
    if [ "$os" = "Darwin" ] && [ "$arch" = "arm64" ] && [ "$machine" != "arm64" ]; then
      printf '[WARN] Skipping %s: it is an x86_64 (Rosetta) build on Apple Silicon.\n' "$cand" >&2
      continue
    fi
    KGW_PY="$(command -v "$cand" 2>/dev/null || printf '%s' "$cand")"
    export KGW_PY
    return 0
  done
  case "$os" in
    Darwin) kgw_die "No supported native Python (3.11-3.14) found." \
      "Install one: https://www.python.org/downloads/macos/ (universal2) or 'brew install python@3.12' (Homebrew in /opt/homebrew)." ;;
    Linux) kgw_die "No supported Python (3.11-3.14) found." \
      "Ubuntu 24.04: sudo apt install python3 python3-venv   (Ubuntu 22.04: sudo apt install python3.11 python3.11-venv)" ;;
    *) kgw_die "No supported Python (3.11-3.14) found." ;;
  esac
}

# Use the project virtual environment; tell the user to run setup if it is missing.
kgw_venv_python() {
  if [ -x "$KGW_ROOT/.venv/bin/python" ]; then
    KGW_PY="$KGW_ROOT/.venv/bin/python"
    export KGW_PY
  else
    kgw_die "The gateway is not installed yet." "Run ./setup.sh first."
  fi
}
