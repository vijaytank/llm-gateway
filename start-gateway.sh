#!/usr/bin/env bash
#
# LLM Gateway — Unix / macOS / Linux Launcher
# Starts Docker Compose stack, polls health, and opens web UI.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NO_BROWSER=false
BUILD=false
TIMEOUT=90

for arg in "$@"; do
    case "$arg" in
        --no-browser) NO_BROWSER=true ;;
        --build) BUILD=true ;;
        --timeout=*) TIMEOUT="${arg#*=}" ;;
        *) ;;
    esac
done

echo ""
echo "========================================================"
echo "           LLM Gateway — Starting Services              "
echo "========================================================"
echo ""

# 1. Preflight check
echo "--> Checking Docker installation..."
if ! command -v docker &>/dev/null; then
    echo "ERROR: 'docker' CLI not found on PATH."
    echo "Please install Docker Desktop or Docker Engine."
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "ERROR: Docker daemon is not running."
    echo "Please start Docker Desktop / daemon service."
    exit 1
fi
echo "  [OK] Docker is installed and running."

# 2. Config setup
if [ ! -f ".env" ] || [ ! -f "gateway_config.yaml" ]; then
    echo "--> Initializing configuration..."
    if command -v python3 &>/dev/null; then
        python3 wizard/setup.py --docker --regenerate-config --force || true
    elif command -v python &>/dev/null; then
        python wizard/setup.py --docker --regenerate-config --force || true
    fi
fi

# 3. Launch Docker Compose
echo "--> Launching Docker Compose stack (profile: full)..."

# Resolve UI port from .env (default 4002) so the health-check URL is accurate
# even if the operator has customised UI_PORT.
UI_PORT=4002
if [ -f ".env" ]; then
    _port=$(grep -m1 '^UI_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d '\r')
    [ -n "$_port" ] && UI_PORT="$_port"
fi
COMPOSE_ARGS=("compose" "-f" "docker/docker-compose.yml" "--profile" "full" "up" "-d")
if [ "$BUILD" = true ]; then
    COMPOSE_ARGS+=("--build")
fi

docker "${COMPOSE_ARGS[@]}"

# 4. Wait for health
echo "--> Waiting for services to become healthy (timeout: ${TIMEOUT}s)..."
START_TIME=$(date +%s)
HEALTHY=false

while [ $(($(date +%s) - START_TIME)) -lt "$TIMEOUT" ]; do
    if curl -s -f "http://localhost:${UI_PORT}/health" >/dev/null 2>&1; then
        HEALTHY=true
        break
    fi
    sleep 2
done

if [ "$HEALTHY" = false ]; then
    echo "WARNING: Services did not report healthy within ${TIMEOUT} seconds."
    echo "Check container logs with: docker compose -f docker/docker-compose.yml logs"
else
    echo "  [OK] All services healthy!"
fi

echo ""
echo "========================================================"
echo "         LLM Gateway is Ready and Serving!              "
echo "========================================================"
echo "  * Web UI:         http://localhost:${UI_PORT}"
echo "  * OpenAI Gateway: http://localhost:4000"
echo "  * Anthropic Port: http://localhost:4001"
echo "========================================================"
echo ""

if [ "$NO_BROWSER" = false ]; then
    echo "Opening Web UI in your default browser..."
    if command -v xdg-open &>/dev/null; then
        xdg-open "http://localhost:4002" >/dev/null 2>&1 || true
    elif command -v open &>/dev/null; then
        open "http://localhost:4002" >/dev/null 2>&1 || true
    fi
fi
