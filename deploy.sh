#!/usr/bin/env bash
set -euo pipefail

INITIAL_CAPITAL="${INITIAL_CAPITAL:-7900}"

echo "Pulling latest code..."
git pull origin main

echo "Ensuring host directories exist..."
mkdir -p state logs

if [ ! -f state/equity.json ]; then
    echo "{\"equity\": ${INITIAL_CAPITAL}.0}" > state/equity.json
    echo "Initialized equity.json with \$${INITIAL_CAPITAL}"
fi

if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Copy .env.example and fill in your credentials."
    exit 1
fi

echo "Building and starting container..."
docker compose up -d --build

echo "Done. Container status:"
docker compose ps

echo ""
echo "View logs: docker compose logs -f algo"
