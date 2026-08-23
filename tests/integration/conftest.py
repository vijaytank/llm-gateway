"""
tests/integration/conftest.py — Integration test stack lifecycle & helpers

Session-scoped fixtures:
  - docker_stack: brings the full stack + mock provider up from clean state,
    waits for every service to go healthy (<= 90s per plan Phase 1 AC), and
    tears everything down (including volumes) at session end.

Helpers (plain functions, imported by tests):
  - pg_query(sql, params): run SQL against Postgres in the db container
  - redis_cmd(*args): run a Redis command in the redis container
  - mock_set(model, status/latency/content): script the mock provider
  - gateway_chat(model, ...): POST to the gateway OpenAI endpoint
  - wait_until(fn, timeout, interval): poll until fn() is truthy

All container access goes through `docker compose exec` so the suite needs no
direct database/redis Python drivers on the host — it exercises the exact
containers production runs.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = PROJECT_ROOT / "docker"
COMPOSE_BASE = [
    "docker", "compose",
    "-f", "docker-compose.yml",
    "-f", str(Path("../tests/integration/docker-compose.testing.yml").as_posix()),
]
PROFILES = ["--profile", "full", "--profile", "testing"]

GATEWAY_URL = "http://localhost:4000"
ADAPTER_URL = "http://localhost:4001"
UI_URL = "http://localhost:4002"
MOCK_URL = "http://localhost:5000"

STACK_TIMEOUT_S = 180  # build can be slow on first run; health AC is 90s after start


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------

def _run(args, timeout=120, check=True, cwd=DOCKER_DIR):
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        shell=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(map(str, args))}\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
        )
    return result


def compose(*args, timeout=120, check=True):
    return _run(COMPOSE_BASE + list(PROFILES) + list(args), timeout=timeout, check=check)


def compose_output(*args, timeout=120):
    return compose(*args, timeout=timeout).stdout


def service_health(name: str) -> str:
    out = compose_output("ps", "--format", "json", name, timeout=30)
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("Service") == name or entry.get("Name", "").endswith(name):
            return entry.get("Health", entry.get("State", "unknown"))
    return "not-found"


# ---------------------------------------------------------------------------
# HTTP helpers (urllib — no extra host dependencies)
# ---------------------------------------------------------------------------

def http_json(method: str, url: str, payload=None, headers=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def http_raw(method: str, url: str, payload=None, headers=None, timeout=30):
    """Returns (status, headers, body-bytes) — for SSE / cookie inspection."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def gateway_chat(model="auto-free", messages=None, timeout=60, extra=None):
    payload = {
        "model": model,
        "messages": messages or [{"role": "user", "content": "integration test ping"}],
        "max_tokens": 20,
    }
    if extra:
        payload.update(extra)
    return http_json("POST", f"{GATEWAY_URL}/v1/chat/completions", payload, timeout=timeout)


# ---------------------------------------------------------------------------
# Container-backed Postgres / Redis access
# ---------------------------------------------------------------------------

def pg_query(sql: str, params=None, fetch="all"):
    """Run SQL inside the postgres container. fetch: all|one|none."""
    flag = {"all": "-c", "one": "-t -A -c", "none": "-c"}[fetch]
    args = ["docker", "compose", "exec", "-T", "postgres",
            "psql", "-U", "llm_gateway", "-d", "llm_gateway"]
    if fetch == "one":
        args += ["-t", "-A", "-c", sql]
    else:
        args += ["-c", sql]
    result = _run(args, timeout=60)
    if fetch == "one":
        return result.stdout.strip()
    if fetch == "all":
        rows = [
            [cell.strip() for cell in line.split("|")]
            for line in result.stdout.strip().splitlines()
            if line.strip()
        ]
        return rows
    return None


def redis_cmd(*args):
    """Run a Redis command inside the redis container; returns stdout."""
    result = _run(["docker", "compose", "exec", "-T", "redis", "redis-cli", *map(str, args)], timeout=30)
    out = result.stdout.strip()
    try:
        return json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return out


def mock_set_status(model: str, status):
    """Script the mock upstream for a model: 200/429/500/503/'timeout'."""
    if status is None:
        redis_cmd("del", f"mock:status:{model}")
    else:
        redis_cmd("set", f"mock:status:{model}", status)


def mock_reset():
    """Clear all scripting keys so every test starts from a healthy mock."""
    for pattern in ("mock:status:*", "mock:latency_ms:*", "mock:request_count:*", "mock:content"):
        for key in redis_cmd("keys", pattern) or []:
            if isinstance(key, str):
                redis_cmd("del", key)


def mock_request_count(model: str) -> int:
    val = redis_cmd("get", f"mock:request_count:{model}")
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def wait_until(fn, timeout=60, interval=1.0, desc="condition"):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            result = fn()
            if result:
                return result
            last_err = result
        except Exception as e:  # transient (connection refused etc.)
            last_err = e
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {desc}; last: {last_err!r}")


def wait_service_healthy(name: str, timeout=90):
    return wait_until(
        lambda: service_health(name) == "healthy",
        timeout=timeout, interval=2.0, desc=f"service {name} healthy",
    )


def all_ports_up(timeout=90):
    def _check():
        for url in (f"{GATEWAY_URL}/health/liveliness", f"{ADAPTER_URL}/health", f"{UI_URL}/health"):
            status, _ = http_json("GET", url, timeout=5)
            if status != 200:
                return False
        return True
    return wait_until(_check, timeout=timeout, interval=2.0, desc="gateway/adapter/ui ports")


# ---------------------------------------------------------------------------
# Session-scoped stack lifecycle
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def docker_stack():
    """Boot the whole stack from clean state; teardown removes containers AND volumes."""
    compose("down", "-v", "--remove-orphans", timeout=180, check=False)
    compose("up", "-d", "--build", timeout=600)
    try:
        for svc in ("postgres", "redis"):
            wait_service_healthy(svc, timeout=90)
        wait_until(_db_init_exited_ok, timeout=120, interval=3.0,
                   desc="db-init completed successfully")
        for svc in ("gateway", "adapter", "ui", "mock-provider"):
            wait_service_healthy(svc, timeout=90)
        all_ports_up(timeout=90)
        # Brain must be running as a second process (supervisord program)
        wait_until(
            lambda: "-m brain.main" in _run(
                ["docker", "compose"] + list(PROFILES) +
                ["exec", "-T", "gateway", "python", "-c",
                 "import os; print(' '.join(open(f'/proc/{p}/cmdline').read().replace(chr(0),' ') "
                 "for p in os.listdir('/proc') if p.isdigit()))"],
                timeout=60).stdout,
            timeout=60, interval=2.0, desc="brain process running",
        )
        yield
    finally:
        compose("down", "-v", "--remove-orphans", timeout=180, check=False)


def _db_init_exited_ok() -> bool:
    out = compose_output("ps", "-a", "--format", "json", "db-init", timeout=30)
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("Service") == "db-init":
            return entry.get("State") == "exited" and entry.get("ExitCode") == 0
    return False


def brain_running() -> bool:
    result = _run(
        ["docker", "compose"] + list(PROFILES) +
        ["exec", "-T", "gateway", "python", "-c",
         "import os; print(' '.join(open(f'/proc/{p}/cmdline').read().replace(chr(0), ' ') "
         "for p in os.listdir('/proc') if p.isdigit()))"],
        timeout=60, check=False,
    )
    return "-m brain.main" in result.stdout


@pytest.fixture(autouse=True)
def _clean_routing_state(docker_stack):
    """Before each test: reset mock scripting + brain state keys so tests are
    order-independent. Runs only for integration tests (this conftest's dir)."""
    mock_reset()
    for pattern in ("gateway:model:*", "gateway:offline*", "gateway:connectivity:*"):
        for key in redis_cmd("keys", pattern) or []:
            if isinstance(key, str):
                redis_cmd("del", key)
    yield
