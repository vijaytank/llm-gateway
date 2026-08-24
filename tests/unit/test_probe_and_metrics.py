"""Unit tests for wizard/provider_probe.py and brain/metrics.py — F-M15 gap."""

import pytest

import wizard.provider_probe as pp
from brain.metrics import GatewayMetrics, HAS_PROM


# ---------------------------------------------------------------------------
# provider_probe
# ---------------------------------------------------------------------------

def test_auth_headers_none_type():
    assert pp._auth_headers("none", "ANY_ENV") == {}
    assert pp._auth_headers("bearer", "") == {}


def test_auth_headers_bearer(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc123")
    h = pp._auth_headers("bearer", "MY_KEY")
    assert h == {"Authorization": "Bearer abc123"}


def test_auth_headers_custom_header(monkeypatch):
    monkeypatch.setenv("MY_KEY", "abc123")
    h = pp._auth_headers("header", "MY_KEY")  # no '=' → default header name
    assert h == {"X-API-Key": "abc123"}


def test_list_models_connection_failure():
    r = pp.list_models(base_url="http://127.0.0.1:1", auth_type="none",
                       api_key_env="", timeout=0.5)
    assert r.ok is False
    assert "connection failed" in r.error


def test_probe_provider_content_filter_is_ok(httpx_mock=None):
    """400 + content_filter body counts as reachable (Issue 4)."""
    calls = {}

    class Resp:
        status_code = 400
        text = '{"error": {"message": "content_filter: blocked"}}'

    def fake_post(url, **kw):
        calls["url"] = url
        return Resp()

    import wizard.provider_probe as m
    original = m.httpx.post
    m.httpx.post = fake_post
    try:
        r = pp.probe_provider("http://up", model="m", auth_type="none")
    finally:
        m.httpx.post = original
    assert r.ok is True
    assert r.status_code == 400


def test_probe_provider_429_counts_as_reachable():
    class Resp:
        status_code = 429
        text = "rate limited"

    import wizard.provider_probe as m
    original = m.httpx.post
    m.httpx.post = lambda url, **kw: Resp()
    try:
        r = pp.probe_provider("http://up", model="m", auth_type="none")
    finally:
        m.httpx.post = original
    assert r.ok is True


def test_probe_provider_auth_failure():
    class Resp:
        status_code = 401
        text = "unauthorized"

    import wizard.provider_probe as m
    original = m.httpx.post
    m.httpx.post = lambda url, **kw: Resp()
    try:
        r = pp.probe_provider("http://up", model="m", auth_type="none")
    finally:
        m.httpx.post = original
    assert r.ok is False
    assert "authentication" in r.error


# ---------------------------------------------------------------------------
# brain/metrics
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_PROM, reason="prometheus_client not installed")
def test_metrics_render_contains_families(fake_redis):
    m = GatewayMetrics()
    m.observe_request(model="m1", provider="prov", status="success", latency_ms=120)
    m.set_circuit_state("m1", "open")
    m.set_score("m1", 0.87)
    m.set_quota_used_ratio("m1", 0.42)
    body = m.render().decode()
    assert "gateway_requests_total" in body
    assert "gateway_circuit_state" in body
    assert "gateway_score" in body
    assert "gateway_quota_used_ratio" in body
    assert 'model="m1"' in body


@pytest.mark.skipif(not HAS_PROM, reason="prometheus_client not installed")
def test_metrics_invalid_circuit_state_ignored():
    m = GatewayMetrics()
    # Unknown state must not raise (gauge simply unset)
    m.set_circuit_state("m-x", "weird_state")
