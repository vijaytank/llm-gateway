"""
README for LLM Gateway

Unified Free-First LLM Gateway — implemented from the final reviewed master plan
(llm-gateway-final-plan.md, all 14 issues resolved).

Phase 0 Status: COMPLETE — Schema, migrations, and infrastructure established
Phase 1 Status: COMPLETE — Core Gateway (static routing, cloud free only)
Phase 2 Status: COMPLETE — Routing Brain (stream reader, scorer, circuit breaker, health scheduler)
Phase 3 Status: COMPLETE — Local Models & Offline Detection (Ollama/vLLM discovery, connectivity monitor)
Phase 4 Status: COMPLETE — Setup Wizard, Web UI (port 4002), Multi-mode Deployment (core/full profiles)
"""

# LLM Gateway — Unified Free-First Routing

## Overview

A self-hosted gateway that routes OpenAI-format requests across free cloud LLM
providers (NVIDIA NIM, Groq, Cerebras, OpenRouter) plus local models
(Ollama/vLLM) and user-defined custom providers, with static fallback chains,
metadata logging to Postgres, live routing state in Redis, an Anthropic
inbound adapter for Claude Code / Anthropic SDK tools, and a server-rendered
web UI for status, stats, and provider management.

| Service | Port | Purpose |
|---------|------|---------|
| LiteLLM Proxy | 4000 | OpenAI-compatible endpoint, static fallback chains |
| Anthropic Adapter | 4001 | `/v1/messages` → OpenAI translation (tools, images, streaming) |
| Web UI | 4002 | Dashboard, provider management, stats, request logs (FastAPI + Jinja2) |
| Routing Brain | (same container) | Redis stream consumer, scoring, circuit breakers, health scheduler |
| Postgres | 5432 | Model registry, request logs, stats, UI settings |
| Redis | 6379 | Live routing state, request stream, cooldown TTLs |

## Repository Layout

```
llm-gateway/
├── adapter/            # Anthropic Inbound Adapter (FastAPI, port 4001)
│   ├── translation.py  #   Anthropic ↔ OpenAI: system, tools, images, stop reasons, streaming
│   ├── schemas.py      #   Wire-format Pydantic models
│   └── server.py       #   POST /v1/messages, GET /health
├── brain/              # Routing Brain (separate process, same container — Issue 9)
│   ├── config.py       #   Versioned thresholds (Issue 5 defaults)
│   ├── stream_reader.py#   XREADGROUP consumer on gateway:requests:stream
│   ├── scorer.py       #   score = 0.40*success + 0.35*latency + 0.25*quota
│   ├── circuit_breaker.py # closed/open/half_open state machine, TTL cooldowns
│   ├── health_scheduler.py # Adaptive probes, jitter, exponential backoff
│   └── main.py         #   supervisord entrypoint (runs reader + scheduler)
├── gateway/            # LiteLLM integration
│   ├── config_generator.py # GatewayConfig + registry → LiteLLM model_list YAML
│   ├── callbacks.py    #   CustomLogger → Postgres request_logs + Redis stream
│   ├── health_startup.py #  3-wave staggered startup probes (0-30s/30-60s/60-120s)
│   ├── router_hook.py  #   Reads scores/circuit state from Redis (fail-safe)
│   └── main.py         #   Container entrypoint: boot order → config → LiteLLM
├── schemas/            # Canonical contracts (Phase 0)
│   ├── config.py       #   GatewayConfig Pydantic v2 — single source of truth
│   │                   #   + CustomProviderConfig (Phase 4 custom providers)
│   └── db.py           #   SQLAlchemy: model_registry, request_logs, stats, ui_settings
├── migrations/         # Alembic migrations (001_initial_schema, 002_add_ui_settings)
├── alembic/            # Alembic env + versions (DATABASE_URL from env)
├── scripts/            # seed_model_registry.py (idempotent upserts)
├── wizard/             # Phase 4 setup wizard + service installers
│   ├── setup.py        #   7-question CLI wizard (.env chmod 600, config validation,
│   │                   #   live-probed custom providers, UI password, install step)
│   ├── provider_probe.py # Shared live probe (/v1/models + Issue-4 chat probe) — used by wizard AND UI
│   ├── install_linux.py#   systemd user unit installer
│   ├── install_macos.py#   LaunchAgent plist installer
│   └── install_windows.py # Docker Desktop guidance (Issue 13 MVP scope)
├── ui/                 # Web UI (FastAPI + Jinja2 SSR, port 4002 — no JS bundler)
│   ├── app.py          #   Dashboard / providers / stats / logs / auth / setup
│   ├── auth.py         #   bcrypt admin password (ui_settings), signed-cookie sessions (24h)
│   └── templates/      #   base/dashboard/login/setup/providers/stats/logs
├── docker/             # docker-compose.yml (core/full profiles), Dockerfiles, supervisord conf
├── tests/{unit,integration,contract}/
└── docs/               # repo-audit.md, dependency-audit.md
```

## Master Plan Issues — All 14 Resolved

1. ✅ LiteLLM dynamic config → Redis-only routing state; static config.yaml
2. ✅ Boot-order deadlock → `depends_on: service_healthy` + entrypoint polling
3. ✅ Anthropic shim → dedicated adapter service (port 4001), full translation
4. ✅ Health probe → structured payload, 12s timeout, content-filter awareness
5. ✅ Scoring formula + thresholds → `brain/config.py` + `routing_defaults:`
6. ✅ Phase order → Foundation → Gateway → Brain → Local → UI → Hardening
7. ✅ Schema migrations → Alembic, `alembic upgrade head` idempotent
8. ✅ OpenRouter → RPD=40 effective, HTTP-Referer header, last-resort position
9. ✅ Routing brain → separate process, same container (supervisord)
10. ✅ Offline detection → UDP probe + ≥2 provider failures (Phase 3)
11. ✅ Test strategy → unit/integration/contract suites per phase
12. ✅ `.env` security → chmod 600, random master key (`sk-litellm-…`)
13. ✅ Windows → Docker Desktop path; native service post-v1
14. ✅ Config contract → Pydantic `GatewayConfig.model_validate()`

## Quick Start

```bash
# 1. First-run setup: 7-question wizard — generates .env (chmod 600) +
#    gateway_config.yaml (validated), probes custom providers live,
#    sets the UI admin password, optionally installs the background service
python wizard/setup.py

# 2. Start the full stack (postgres → redis → db-init → gateway → adapter → ui)
cd docker && docker-compose --profile full up -d
#    ...or gateway + adapter only, no UI:
cd docker && docker-compose --profile core up -d

# 3. Verify
curl http://localhost:4000/health/liveliness   # gateway
curl http://localhost:4001/health              # adapter
curl http://localhost:4002/health              # web UI

# 4. Web UI — first visit prompts you to create the admin password
#    http://localhost:4002  (dashboard / providers / stats / logs)

# 5. Send a request (OpenAI format)
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto-free", "messages": [{"role": "user", "content": "Hello"}]}'

# 6. Anthropic SDK tools: set ANTHROPIC_BASE_URL=http://localhost:4001
```

### Background service (bare metal)

```bash
python -m wizard.install_linux    # systemd user unit (Linux)
python -m wizard.install_macos    # LaunchAgent (macOS)
python -m wizard.install_windows  # prints Docker Desktop instructions (Issue 13)
```

### Migrations (manual / bare metal)

```bash
export DATABASE_URL=postgresql://llm_gateway:pass@localhost:5432/llm_gateway
alembic upgrade head
python scripts/seed_model_registry.py
```

## Custom Providers & Local Models (Phases 3–4)

**Local models** — enable `providers.local` in the wizard; Ollama/vLLM models are
discovered live from their endpoints (`ollama list`, `/v1/models`) and appear
last in all fallback chains. Offline detection (UDP probe to `1.1.1.1:53` +
provider-failure count) switches routing to the local pool automatically.

**Custom providers** — add any OpenAI-compatible endpoint via the wizard or the
UI (`Providers → Add custom provider`). The provider is probed live before it is
saved; a failed probe rejects the entry. Only the API-key *env-var name* is
stored in config — key values stay in `.env`. Toggling a provider edits the
canonical `GatewayConfig`, which regenerates the LiteLLM model list on restart.

## Configuration

All tunable thresholds live in `gateway_config.yaml` under `routing_defaults:`
and are validated by `GatewayConfig` (Pydantic v2) at startup. Key defaults:

```yaml
routing_defaults:
  circuit_breaker_failure_count: 3
  cooldown_429_seconds: 600        # 10 min
  cooldown_5xx_seconds: 1800       # 30 min
  cooldown_auth_seconds: 86400     # 24 h
  score_weight_success_rate: 0.40
  score_weight_latency: 0.35
  score_weight_quota_headroom: 0.25
  moving_avg_window: 50
```

## Development

```bash
pip install -r requirements.txt
pytest tests/unit/           # no network needed
pytest tests/integration/    # requires docker-compose stack
pytest tests/contract/       # SDK contract tests
```

## References

- `llm-gateway-final-plan.md` — master plan (all 14 issues + fixes)
- `docs/repo-audit.md` — 9 external repos analyzed
- `docs/dependency-audit.md` — pinned versions, licenses, Enterprise gate check

---

**Status:** Phases 0–4 complete (142 unit tests passing; UI smoke-tested live)
**Next:** Phase 5 — Hardening, Observability & Documentation (half-open throttle,
provider-level circuit breaker, `/metrics`, Grafana, config export/import, runbook)
**License:** MIT
