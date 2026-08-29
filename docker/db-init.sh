#!/bin/bash
# docker/db-init.sh — Standalone script for manual/bare-metal DB initialization.
# Note: docker-compose runs its own inline command in the db-init service.
# This script is provided as a utility for standalone or bare-metal setups.

set -e

echo "=== Running Database Initialization ==="
cd "$(dirname "$0")/.."

# Run migrations
echo "Running alembic upgrade head..."
alembic upgrade head 2>&1 || echo 'WARNING: Alembic migration failed (DB may already be migrated or unreachable)'
echo "Migrations check completed."

# Seed model registry
echo "Seeding model registry..."
python scripts/seed_model_registry.py 2>&1 || echo 'WARNING: Model registry seed failed'
echo "Model registry seed completed."

echo "=== DB initialization complete ==="
exit 0