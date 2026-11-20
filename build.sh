#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if docker compose version >/dev/null 2>&1; then COMPOSE=(docker compose); else COMPOSE=(docker-compose); fi
if ! docker info >/dev/null 2>&1; then echo "ERROR: Docker is not running. Start Docker Desktop (or the Docker daemon) and run build.sh again." >&2; exit 1; fi
if [ -z "$("${COMPOSE[@]}" ps -q 2>/dev/null)" ]; then
  for port in 8080 5173; do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then echo "WARNING: port $port is already in use on this machine; this project may fail to start. Stop the other project or free the port." >&2; fi
  done
fi
"${COMPOSE[@]}" up --build -d --wait --wait-timeout 360
bash seed-data.sh
