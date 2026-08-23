#!/bin/bash
# docker/entrypoint.sh — Entrypoint for gateway Docker service
# Runs config generator, then starts LiteLLM proxy with custom callbacks

set -e

echo "=== LLM Gateway Docker Entrypoint ==="

# Step 1: Run config generator
echo "Running config generator..."
python -m gateway.config_generator
echo "Config generation complete."

# Step 2: Wait for Postgres and Redis to be ready (health checks handled by docker-compose)
echo "Waiting for dependencies to be ready..."
# Note: docker-compose handles depends_on with condition: service_healthy
# This script just continues; the gateway will wait internally

# Step 3: Start LiteLLM proxy with custom logger
echo "Starting LiteLLM proxy with CustomLogger..."
exec python -m uvicorn gateway.main:app --host 0.0.0.0 --port 4000