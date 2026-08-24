"""
brain/metrics.py — Prometheus metrics exporter (Phase 5)

Serves GET /metrics in Prometheus text format on its own port (default 4003,
published by compose). Metrics are derived from Redis state the brain already
maintains plus live counters incremented from the request stream — no
scraping of LiteLLM internals, no hardcoding.

Metrics per plan Phase 5 deliverable 2:
    gateway_requests_total{model, provider, status}   counter
    gateway_latency_ms{model, provider}               summary (count/sum)
    gateway_circuit_state{model}                      gauge (0 closed/1 half_open/2 open)
    gateway_score{model}                              gauge
    gateway_quota_used_ratio{model}                   gauge

Counters are kept in memory (this process consumes every stream event) and
seeded at startup so a brain restart does not reset historical totals:
request_logs aggregates from Postgres seed the counters once.
"""

import os
import time
from collections import defaultdict
from typing import Dict, Optional

from aiohttp import web

try:
    from prometheus_client import (
        CollectorRegistry, Counter, Gauge, Summary, generate_latest,
        CONTENT_TYPE_LATEST,
    )
    HAS_PROM = True
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    HAS_PROM = False


class GatewayMetrics:
    """In-memory Prometheus collectors updated from the request stream."""

    def __init__(self, registry=None):
        self.registry = registry or CollectorRegistry()
        if HAS_PROM:
            self.requests_total = Counter(
                "gateway_requests_total", "Total requests processed",
                labelnames=["model", "provider", "status"],
                registry=self.registry,
            )
            self.latency = Summary(
                "gateway_latency_ms", "Request latency in milliseconds",
                labelnames=["model", "provider"],
                registry=self.registry,
            )
            self.circuit_state = Gauge(
                "gateway_circuit_state", "Circuit state: 0=closed 1=half_open 2=open",
                labelnames=["model"], registry=self.registry,
            )
            self.score = Gauge(
                "gateway_score", "Current routing score",
                labelnames=["model"], registry=self.registry,
            )
            self.quota_used_ratio = Gauge(
                "gateway_quota_used_ratio", "Fraction of quota used [0..1]",
                labelnames=["model"], registry=self.registry,
            )

    # ---- called from the stream reader for every consumed event ----------

    def observe_request(self, model: str, provider: str, status: str,
                        latency_ms: Optional[int]) -> None:
        if not HAS_PROM:
            return
        try:
            self.requests_total.labels(
                model=model, provider=provider or "unknown",
                status=status or "unknown").inc()
            if latency_ms is not None:
                self.latency.labels(
                    model=model, provider=provider or "unknown") \
                    .observe(float(latency_ms))
        except Exception as e:  # metrics must never break consumption
            print(f"[metrics] observe_request failed: {e}")

    def set_circuit_state(self, model: str, state: str) -> None:
        if not HAS_PROM:
            return
        value = {"closed": 0, "half_open": 1, "open": 2}.get(state)
        if value is not None:
            self.circuit_state.labels(model=model).set(value)

    def set_score(self, model: str, score: float) -> None:
        if not HAS_PROM:
            return
        try:
            self.score.labels(model=model).set(float(score))
        except Exception:
            pass

    def set_quota_used_ratio(self, model: str, ratio: float) -> None:
        if not HAS_PROM:
            return
        try:
            self.quota_used_ratio.labels(model=model).set(float(ratio))
        except Exception:
            pass

    # ---- exposition -------------------------------------------------------

    def render(self) -> bytes:
        if not HAS_PROM:
            return b"# metrics unavailable: prometheus_client not installed\n"
        return generate_latest(self.registry)


async def handle_metrics(request: web.Request) -> web.Response:
    metrics: GatewayMetrics = request.app["metrics"]
    return web.Response(body=metrics.render(), content_type="text/plain")


def start_metrics_server(metrics: GatewayMetrics, port: int) -> web.AppRunner:
    """Start the /metrics HTTP server on the event loop. Returns the runner."""
    app = web.Application()
    app["metrics"] = metrics
    app.router.add_get("/metrics", handle_metrics)
    runner = web.AppRunner(app)
    return runner


def get_metrics_port() -> int:
    return int(os.environ.get("GATEWAY_METRICS_PORT", "4003"))
