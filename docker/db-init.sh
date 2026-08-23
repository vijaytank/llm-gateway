#!/bin/bash
# docker/db-init.sh — Runs Alembic migrations + model registry seed
# This runs as a one-shot service in docker-compose

set -e

echo "=== Running Alembic migrations ==="
cd /app

# Install dependencies if not already installed
pip install -q alembic sqlalchemy psycopg2-binary pydantic pyyaml

# Run migrations
echo "Running alembic upgrade head..."
alembic upgrade head
echo "Migrations completed successfully."

# Seed model registry
echo "Seeding model registry..."
python scripts/seed_model_registry.py
echo "Model registry seeded successfully."

echo "=== db-init.sh complete ==="
exit 0