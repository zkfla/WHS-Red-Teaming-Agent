#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"

if [[ ! -x ".venv/bin/python" ]]; then
  echo "Local virtual environment not found. Run ./scripts/setup.sh first."
  exit 1
fi

exec ".venv/bin/python" -m redteaming.cli "$@"
