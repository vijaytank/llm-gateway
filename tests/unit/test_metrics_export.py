"""Phase 5 unit tests: Prometheus metrics exporter (brain/metrics.py)."""

import pytest

from brain.metrics import GatewayMetrics


@pytest.fixture
def metrics():
    return GatewayMetrics()


def test_requests_counter_increments(metrics):
    metrics.observe_request("m1", "mock-alpha", "success", 100)
    metrics.observe_request("m1", "mock-alpha", "success", 150)
    metrics.observe_request("m2", "mock-beta", "error", 200)
    out = metrics.render().decode()
    assert 'gateway_requests_total{model="m1",provider="mock-alpha",status="success"}' in out
    assert 'gateway_requests_total{model="m1",provider="mock-alpha",status="success"} 2.0' in out
    assert 'gateway_requests_total{model="m2",provider="mock-beta",status="error"} 1.0' in out


def test_latency_summary_observed(metrics):
    metrics.observe_request("lat-model", "prov", "success", 250)
    out = metrics.render().decode()
    assert "gateway_latency_ms_count" in out
    assert "gateway_latency_ms_sum" in out


def test_circuit_state_gauge_mapping(metrics):
    metrics.set_circuit_state("c-open", "open")
    metrics.set_circuit_state("c-closed", "closed")
    metrics.set_circuit_state("c-half", "half_open")
    out = metrics.render().decode()
    assert 'gateway_circuit_state{model="c-open"} 2.0' in out
    assert 'gateway_circuit_state{model="c-closed"} 0.0' in out
    assert 'gateway_circuit_state{model="c-half"} 1.0' in out


def test_score_and_quota_gauges(metrics):
    metrics.set_score("s-model", 0.87)
    metrics.set_quota_used_ratio("q-model", 0.4)
    out = metrics.render().decode()
    assert 'gateway_score{model="s-model"} 0.87' in out
    assert 'gateway_quota_used_ratio{model="q-model"} 0.4' in out


def test_render_is_valid_prometheus_text(metrics):
    metrics.observe_request("x", "y", "success", 10)
    out = metrics.render().decode()
    assert "# HELP" in out and "# TYPE" in out


def test_unknown_status_uses_placeholder(metrics):
    metrics.observe_request("m", None, None, None)
    out = metrics.render().decode()
    assert 'gateway_requests_total{model="m",provider="unknown",status="unknown"}' in out
