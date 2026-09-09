#!/bin/bash
set -euo pipefail

# Pin the compose project name so the volume/network stay consistent whether
# this runs on the host (repo dir "agenttune") or inside the agenttune
# container (where the compose file lives at /app -> project "app").
export COMPOSE_PROJECT_NAME=agenttune

echo "Stopping postgres container..."
docker compose stop postgres

echo "Removing postgres container..."
docker compose rm -f postgres

echo "Removing pgdata volume..."
docker volume rm agenttune_pgdata

echo "Recreating postgres container..."
docker compose up -d postgres

echo "Waiting for postgres to become healthy..."
for _ in $(seq 1 60); do
    status=$(docker inspect -f '{{.State.Health.Status}}' agenttune-pg 2>/dev/null || echo "missing")
    if [ "$status" = "healthy" ]; then
        echo "postgres is healthy."
        exit 0
    fi
    sleep 1
done

echo "postgres did not become healthy within 60s" >&2
exit 1