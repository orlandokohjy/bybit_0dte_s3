#!/usr/bin/env bash
set -euo pipefail

echo "Pulling latest code..."
git pull origin main

echo "Building and starting container..."
docker compose up -d --build

echo "Done. Container status:"
docker compose ps
