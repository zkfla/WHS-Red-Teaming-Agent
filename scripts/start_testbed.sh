#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: ./scripts/start_testbed.sh IMAGE [HOST_PORT] [CONTAINER_PORT] [NAME]" >&2
  exit 2
fi

image="$1"
host_port="${2:-3000}"
container_port="${3:-3000}"
name="${4:-redteaming-testbed}"

command -v docker >/dev/null 2>&1 || {
  echo "Docker is not installed or is not on PATH." >&2
  exit 1
}

exec docker run --rm --name "$name" \
  -p "127.0.0.1:${host_port}:${container_port}" "$image"
