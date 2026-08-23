"""
gateway/main.py — LiteLLM proxy entrypoint

Boot sequence per master plan Issue 2 (boot-order deadlock fix):
1. Generate/refresh LiteLLM config from GatewayConfig + DB model registry
2. Run staggered startup health checks (3-wave) writing initial status to Redis
3. Launch litellm proxy with the generated config + custom callbacks

This runs INSIDE the gateway container as the entrypoint process.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

# Allow imports both as `gateway.main` and `python gateway/main.py`
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _wait_for_postgres(timeout: int = 60) -> None:
    """Poll Postgres until reachable (bare-metal safety net; docker-compose
    already enforces service_healthy ordering)."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("[main] DATABASE_URL not set; skipping postgres wait")
        return

    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import psycopg2
            conn = psycopg2.connect(url, connect_timeout=5)
            conn.close()
            print("[main] Postgres reachable")
            return
        except Exception as e:
            print(f"[main] Waiting for Postgres... ({e.__class__.__name__})")
            time.sleep(2)
    raise RuntimeError("Postgres not reachable within timeout")


def _wait_for_redis(timeout: int = 60) -> None:
    """Poll Redis until reachable."""
    import time
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            import redis as redis_lib
            client = redis_lib.from_url(redis_url)
            if client.ping():
                print("[main] Redis reachable")
                return
        except Exception as e:
            print(f"[main] Waiting for Redis... ({e.__class__.__name__})")
            time.sleep(2)
    raise RuntimeError("Redis not reachable within timeout")


def main() -> int:
    print("[main] === LLM Gateway boot sequence ===")

    # 1. Wait for backing services (Issue 2: explicit boot order)
    _wait_for_postgres()
    _wait_for_redis()

    # 2. Generate LiteLLM config.yaml from GatewayConfig + registry
    import yaml as _yaml
    config_out = os.environ.get("LITELLM_CONFIG_PATH", "/app/litellm_config.yaml")
    gateway_config_path = os.environ.get("GATEWAY_CONFIG_PATH", "gateway_config.yaml")

    from gateway.config_generator import generate_litellm_config
    from schemas.config import GatewayConfig

    gw_config = GatewayConfig.load_from_file(gateway_config_path)
    litellm_cfg = generate_litellm_config(gw_config)
    with open(config_out, "w") as f:
        _yaml.safe_dump(litellm_cfg, f, default_flow_style=False, sort_keys=False)
    print(f"[main] LiteLLM config generated at {config_out}")

    # 3. Staggered startup health checks (3-wave), writes status to Redis
    run_health = os.environ.get("SKIP_STARTUP_HEALTH_CHECKS", "").lower() not in ("1", "true")
    if run_health:
        try:
            from gateway.health_startup import run_health_checks
            import redis as _redis

            # Build the provider list from the validated config — no
            # hardcoded endpoints (plan DoD). Cloud providers only; local
            # models are discovered separately in Phase 3 style.
            provider_urls = {
                "nvidia": os.environ.get("NVIDIA_BASE_URL", ""),
                "groq": os.environ.get("GROQ_BASE_URL", ""),
                "cerebras": os.environ.get("CEREBRAS_BASE_URL", ""),
                "openrouter": os.environ.get("OPENROUTER_BASE_URL", ""),
            }
            probe_providers = [
                {"name": name, "base_url": url}
                for name, url in provider_urls.items()
                if url  # skip providers without a configured base URL
            ]

            redis_client = _redis.from_url(
                os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                decode_responses=True,
            )
            if probe_providers:
                asyncio.run(run_health_checks(probe_providers, redis_client))
            else:
                print("[main] no provider base URLs configured; skipping startup probes")
        except Exception as e:
            # Health check failures must not block boot — models get probed again
            print(f"[main] WARNING: startup health checks failed ({e}); continuing")

    # 4. Start LiteLLM proxy with custom callbacks
    callbacks = os.environ.get(
        "LITELLM_CUSTOM_CALLBACKS", "gateway.callbacks.custom_logger"
    )
    port = os.environ.get("GATEWAY_PORT", "4000")

    cmd = [
        "litellm",
        "--config", config_out,
        "--port", port,
        "--host", "0.0.0.0",
    ]
    env = dict(os.environ)
    env.setdefault("LITELLM_MASTER_KEY", "")  # real key comes from .env via compose
    if callbacks:
        env["CUSTOM_CALLBACKS"] = callbacks

    print(f"[main] Starting LiteLLM on port {port}: {' '.join(cmd)}")
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    sys.exit(main())
