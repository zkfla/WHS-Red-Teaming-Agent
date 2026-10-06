#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python_cmd="${PYTHON:-python3}"
command -v "$python_cmd" >/dev/null || { echo "Python 3.10+ is required." >&2; exit 1; }
recreate=0
if [[ ! -x .venv/bin/python ]]; then
  recreate=1
elif ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
  recreate=1
fi
if [[ "${recreate}" -eq 1 ]]; then
  if [[ -d .venv ]]; then
    echo "Existing .venv is stale or invalid; recreating it."
    rm -rf .venv
  fi
  "$python_cmd" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[llm]'
echo "Ready. Run: source .venv/bin/activate && redteaming"
