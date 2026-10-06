#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: ./scripts/start_compose_testbed.sh IMAGE [HOST_PORT] [CONTAINER_PORT]"
  exit 2
fi

target_image="$1"
host_port=3000
container_port=3000
if [[ $# -ge 2 ]]; then host_port="$2"; fi
if [[ $# -ge 3 ]]; then container_port="$3"; fi

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
export REDTEAM_TARGET_IMAGE="$target_image"
export REDTEAM_TARGET_HOST_PORT="$host_port"
export REDTEAM_TARGET_CONTAINER_PORT="$container_port"
exec docker compose -f deploy/docker/compose.testbed.yaml up --build
