# Changelog

All notable changes to the LLM Gateway are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [1.1.0] — 2025-08-25 — Architecture Review Remediation

Internal architecture-review document (not included in the public repo).

### Security
- **(F-C1)** Removed committed `docker/.env` from git tracking and disk; added
  `docker/.env.example` template; replaced live master key in
  `gateway_config.yaml` with a placeholder. Secrets must now come from env only.
- **(F-M10)** Adapter error responses no longer echo internal exception text;
  Anthropic-style envelopes with server-side logging.
- **(F-M12)** `jinja2` bumped 3.1.4 → 3.1.6 (CVE-2024-56201/CVE-2024-56326);
  `aiohttp` 3.9.5 → 3.10.11 (accumulated 3.10-line security fixes).
- CI: secret scanning + `pip-audit` on every push/PR.

### Fixed — routing (the plan's core mechanism)
- **(F-H1)** RouterHook is now wired into the LiteLLM request path via
  `async_pre_call_hook` in the registered CustomLogger; generated config sets
  `routing_strategy: latency-based-routing-v2`. Brain-maintained Redis state
  (scores, circuits, offline mode) actually influences routing decisions.
- **(F-M14)** `get_fallback_priority()` drops excluded models instead of
  appending them at the chain tail; removed the backfill loop that silently
  re-added them; collapsed dead influence-threshold ladder.

### Fixed — reliability
- **(F-H5)** Stream reader ACKs AFTER processing (was before → event loss on
  crash); added XAUTOCLAIM reclaim of stale pending messages on startup
  (plan Phase 2 AC); poison messages are deliberately ACKed with a log line.
- **(F-H2)** HealthScheduler no longer fabricates "healthy" for models it
  cannot probe: endpoint resolution implemented from provider base URLs;
  unknown endpoints are SKIPPED without touching Redis status.
- **(F-M1)** Circuit breaker no longer transitions open→half_open on stray
  successes; half-open is reached only via cooldown TTL expiry.
- **(F-M2)** Cooldown key names unified (`rate_limit`/`server_error`/`auth`
  constants); `transition_to_closed` now clears the correct keys.
- **(F-M3)** Prometheus circuit-state gauge reads a real CircuitBreakerManager
  (was permanently "closed").
- **(F-H4)** Adapter `/v1/messages` honors `"stream": true` — Anthropic SDK
  streaming clients get proper SSE; removed the unreachable duplicate route.
- **(F-M8)** Async LiteLLM hooks offload blocking Postgres writes to a worker
  thread (event loop no longer stalls per request).
- **(F-L8)** Aggregator daily rollup fires when the previous hour ends a day
  (was: only if a tick landed within 5 min of midnight UTC).

### Fixed — scoring correctness
- **(F-M4)** Success-rate now computed over a rolling last-N outcome window
  (was an unbounded cumulative hash that masked fresh failures).
- **(F-M5)** Quota headroom term is live: RPM sliding-window counter vs seeded
  registry limits (was constant 1.0).
- **(F-L12)** Latency window keys built from explicit model names, not
  positional key-splitting.
- Startup health classification bug fixed: plain 200 responses were
  misclassified as "slow" when the body lacked a usage object.

### Changed
- **(F-M7)** Custom provider configs reject placeholder model names
  (`__*`) at the schema layer; UI add-provider flow probes discovery BEFORE
  constructing/saving anything.
- **(F-M9)** Adapter CORS tightened (removed wildcard+credentials combo).
- **(F-M13)** Dockerfile.adapter pins extracted from requirements.txt —
  single source of truth for framework versions.
- **(F-L10)** Offline-detection cloud-provider list derived from configured
  providers; local endpoints are probe targets but never offline indicators.

### Removed
- **(F-L4)** Broken root `Dockerfile` + `docker/entrypoint.sh` (unreferenced;
  entrypoint exec'd a nonexistent app object).
- **(F-L5)** Duplicate `migrations/` tree (byte-identical to
  `alembic/versions/001_initial_schema.py`).
- **(F-L3)** Triplicated health-wave functions → one parameterized runner with
  real wave partitioning.
- **(F-L6/F-L7/F-L13)** Dead branches, duplicate `temperature` field,
  mock-looking default ids, unrouted stream translator, unused conftest dict.
- Stray root-level integration log files moved out of the repo.

### Tests
- Coverage raised 59% → **70.75%** with an enforced `--cov-fail-under=70` gate
  (project decision; plan's >95% ambition intentionally relaxed).
- New suites: adapter server (incl. SSE streaming), stream pipeline
  (ACK ordering, XAUTOCLAIM, rolling windows, quota counters), health-startup
  waves, health-scheduler probe resolution, scorer/router edge regressions,
  wizard file generation + interactive helpers, aggregator SQL, metrics export.
- **(F-H3)** Fixed failing security integration test (`import pytest` missing)
  and made it order-independent.
- Two tests updated to encode corrected F-M14 exclusion semantics.

### Added
- GitHub Actions CI: lint, dependency audit, unit suite w/ coverage gate,
  secret scan.
- This changelog; remediation tracker inside the review doc.

## [1.0.0] — 2025-08-24 — Phases 0–5 complete

Initial feature-complete implementation (Phases 0–5):
schema/migrations, core gateway, routing brain, local models & offline
detection, setup wizard + web UI, hardening (half-open throttle,
provider-level circuit, /metrics, config backup, login rate limit).
