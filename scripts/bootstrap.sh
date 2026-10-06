#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
launch=0
for arg in "$@"; do
  case "$arg" in
    --launch) launch=1 ;;
  esac
done

python_cmd="${PYTHON:-python3}"
command -v "$python_cmd" >/dev/null || { echo "Python 3.10+가 필요합니다."; exit 1; }
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

if [[ "$launch" -eq 1 ]]; then
  exec .venv/bin/python -m redteaming.cli
fi
echo "준비가 끝났습니다. 실행: ./scripts/start.sh"
