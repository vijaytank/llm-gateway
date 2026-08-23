# Dockerfile for LiteLLM Gateway Service
# Builds the gateway service with custom callbacks, config generator, and health checks
#
# This Dockerfile:
# 1. Uses Python 3.11 slim as base
# 2. Installs LiteLLM and all dependencies
# 3. Copies gateway code including custom callbacks
# 4. Sets up the config generator to run on startup
# 5. Exposes port 4000 for OpenAI-compatible API
# 6. Uses supervisord to manage multiple processes

FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy gateway code
COPY gateway/ ./gateway/

# Copy schemas
COPY schemas/ ./schemas/

# Copy migrations
COPY migrations/ ./migrations/

# Copy scripts
COPY scripts/ ./scripts/

# Copy the db-init script
COPY docker/db-init.sh ./docker/db-init.sh

# Make db-init script executable
RUN chmod +x ./docker/db-init.sh

# Copy entrypoint script
COPY docker/entrypoint.sh ./docker/entrypoint.sh
RUN chmod +x ./docker/entrypoint.sh

# Expose LiteLLM proxy port
EXPOSE 4000

# Expose custom logger metrics (optional)
EXPOSE 4001

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    GATEWAY_CONFIG_PATH=/app/gateway_config.yaml \
    REDIS_URL=redis://redis:6379/0 \
    DATABASE_URL=postgresql://llm_gateway:${POSTGRES_PASSWORD:-llm_gateway_pass}@postgres:5432/llm_gateway

# Entry point: runs the config generator then starts LiteLLM with custom logger
ENTRYPOINT ["/app/docker/entrypoint.sh"]