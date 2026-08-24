"""
brain/main.py — Routing brain supervisor entrypoint

Per Issue 9: the brain is a separate process (started by supervisord alongside
LiteLLM in the same container). It runs:
  1. stream_reader          — XREADGROUP consumer on gateway:requests:stream
  2. health_scheduler       — adaptive probe loop
  3. connectivity_monitor   — UDP offline detection (Phase 3)
  4. aggregator             — hourly stats rollup (APScheduler, Phase 2 d7)

If any component crashes, supervisord restarts it; LiteLLM keeps routing
on last-known Redis state.
"""

import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web  # metrics HTTP server (Phase 5)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def start_aggregator_scheduler() -> object | None:
    """
    Schedule the hourly aggregation job (Phase 2 deliverable 7):
    - runs at the top of every hour
    - aggregates previous hour into model_stats_hourly
    - midnight run also writes model_stats_daily
    - applies retention: request_logs > 10 days, hourly stats > 30 days
    """
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from brain.aggregator import run_aggregation
    except ImportError as e:
        print(f"[brain] aggregator unavailable ({e}); stats rollup disabled")
        return None

    scheduler = AsyncIOScheduler()
    # Top of every hour. run_aggregation() itself decides whether the
    # midnight daily rollup also fires.
    scheduler.add_job(run_aggregation, "cron", minute=0, id="hourly_aggregation")
    scheduler.start()
    print("[brain] hourly aggregation scheduled (cron minute=0)")
    return scheduler


async def amain() -> None:
    import redis as redis_lib

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = redis_lib.from_url(redis_url, decode_responses=True)

    from brain.stream_reader import StreamReader
    from brain.health_scheduler import HealthScheduler
    from brain.connectivity_monitor import ConnectivityMonitor

    # Real provider probe targets (review F-H2): base URLs come from env —
    # same variables gateway/main.py uses for startup probes. Providers with
    # no configured URL are skipped by the scheduler, never faked healthy.
    provider_configs = {}
    for name, url_env in (
        ("nvidia", "NVIDIA_BASE_URL"),
        ("groq", "GROQ_BASE_URL"),
        ("cerebras", "CEREBRAS_BASE_URL"),
        ("openrouter", "OPENROUTER_BASE_URL"),
    ):
        url = os.environ.get(url_env, "")
        if url:
            provider_configs[name] = {"base_url": url}
    # Local endpoints are probe targets too, but NEVER count toward offline
    # detection — they ARE the offline fallback (review F-L10 keeps the cloud
    # list separate from the local pool).
    for name, url_env in (("ollama", "OLLAMA_BASE_URL"), ("vllm", "VLLM_BASE_URL")):
        url = os.environ.get(url_env, "")
        if url:
            provider_configs[name] = {"base_url": url}

    reader = StreamReader(redis_client=client)
    scheduler = HealthScheduler(redis_client=client, provider_configs=provider_configs)
    # Cloud-only list for offline accounting — local endpoints are probe
    # targets above but NEVER offline indicators (review F-L10).
    monitor = ConnectivityMonitor(
        redis_client=client,
        cloud_providers=[n for n in provider_configs if n not in ("ollama", "vllm")] or None,
    )
    agg_scheduler = start_aggregator_scheduler()

    # Phase 5: Prometheus metrics exporter on its own port.
    metrics_runner = None
    try:
        from brain.metrics import GatewayMetrics, start_metrics_server, get_metrics_port
        metrics = GatewayMetrics()
        reader.metrics = metrics  # stream events feed counters/gauges
        metrics_runner = start_metrics_server(metrics, get_metrics_port())
        await metrics_runner.setup()
        site = web.TCPSite(metrics_runner, "0.0.0.0", get_metrics_port())
        await site.start()
        print(f"[brain] /metrics serving on port {get_metrics_port()}")
    except Exception as e:
        print(f"[brain] metrics exporter unavailable ({e}); continuing without")

    # Run stream consumption, health scheduling, and connectivity monitoring
    # concurrently on one loop. StreamReader.start() is a blocking loop — push
    # it to a worker thread.
    try:
        await asyncio.gather(
            asyncio.to_thread(reader.start),
            scheduler.start(),
            monitor.start(),
        )
    finally:
        if agg_scheduler is not None:
            agg_scheduler.shutdown(wait=False)
        if metrics_runner is not None:
            await metrics_runner.cleanup()


def main() -> int:
    print("[brain] Routing brain starting")
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("[brain] Routing brain stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
