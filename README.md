# LLM Gateway

[![Unit tests](https://github.com/vijaytank/llm-gateway/actions/workflows/unit.yml/badge.svg?branch=develop)](https://github.com/vijaytank/llm-gateway/actions/workflows/unit.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

**One OpenAI-compatible endpoint for every free LLM provider — with automatic failover, scoring-based routing, and local fallback.**

LLM Gateway sits between your app and free-tier LLM providers (NVIDIA NIM,
Groq, Cerebras, OpenRouter) plus your local models (Ollama / vLLM). Each
request is routed to the best available provider; failing providers are
circuit-broken automatically; if the internet goes down, routing falls back
to local models. An Anthropic-compatible adapter lets tools like Claude Code
use the same pool.

## Why

Free LLM tiers are individually rate-limited and flaky. Used alone they hit
429s constantly. LLM Gateway combines them into one reliable endpoint:

- **Fallback chains** — each virtual model (`auto-free`, `auto-code-free`,
  `auto-reasoning-free`) is an ordered chain across providers.
- **Score-based routing** — a brain process continuously scores every model:
  `score = 0.40·success_rate + 0.35·latency + 0.25·quota_headroom`.
- **Circuit breakers** — 429/5xx/auth failures pull a model out of rotation
  with tiered cooldowns (10 min / 30 min / 24 h), half-open recovery probing.
- **Offline detection** — UDP probe + consecutive provider failures flip
  routing to the local pool automatically.

## Quick Start

Requirements: [Docker](https://docs.docker.com/get-docker/) and Python 3.11+.

```bash
git clone https://github.com/vijaytank/llm-gateway.git
cd llm-gateway

# 1. Setup wizard — generates .env (chmod 600) + gateway_config.yaml,
#    probes any custom providers live, sets the UI admin password
python wizard/setup.py

# 2. Start the stack (postgres → redis → db-init → gateway → adapter → ui)
cd docker && docker compose --profile full up -d
#    ...or gateway + adapter only, no UI:
cd docker && docker compose --profile core up -d
```

Verify and use:

```bash
# Health checks
curl http://localhost:4000/health/liveliness   # gateway
curl http://localhost:4001/health              # adapter
curl http://localhost:4002/health              # web UI

# Send a request (OpenAI format) — source .env first so $LITELLM_MASTER_KEY is set
source ../.env   # or: export $(grep -v '^#' .env | xargs)
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "auto-free", "messages": [{"role": "user", "content": "Hello"}]}'

# Anthropic SDK / Claude Code: point it at the adapter
export ANTHROPIC_BASE_URL=http://localhost:4001
```

The web UI at **http://localhost:4002** shows dashboard, stats and request
logs; the first visit prompts you to create the admin password. Providers can
be added/removed live from **Providers → Add custom provider** (each entry is
probed before being saved).

### Background service (bare metal)

```bash
python -m wizard.install_linux    # systemd user unit (Linux)
python -m wizard.install_macos    # LaunchAgent (macOS)
python -m wizard.install_windows  # prints Docker Desktop instructions
```

<details>
<summary>Manual migrations (bare metal without Docker)</summary>

```bash
export DATABASE_URL=postgresql://llm_gateway:<password>@localhost:5432/llm_gateway
alembic upgrade head
python scripts/seed_model_registry.py
```

</details>

## Services

| Service | Port | Purpose |
|---------|------|---------|
| LiteLLM Proxy | 4000 | OpenAI-compatible endpoint, static fallback chains |
| Anthropic Adapter | 4001 | `/v1/messages` → OpenAI translation (tools, images, streaming) |
| Web UI | 4002 | Dashboard, provider management, stats, request logs |
| Routing Brain | 4003 | Scoring, circuit breakers, health scheduler; serves `/metrics` |
| Postgres | 5432 | Model registry, request logs, aggregates, UI settings |
| Redis | 6379 | Live routing state, request stream, cooldown TTLs |

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

Provider API keys are referenced **by env-var name only** in config
(`api_key_env`); actual values stay in `.env`. See `.env.example` for the full
variable list, or let the wizard generate everything.

## Local Models & Custom Providers

- **Local models** — enable `providers.local` in the wizard; Ollama/vLLM
  models are discovered from their endpoints and placed last in every
  fallback chain as the offline safety net.
- **Custom providers** — any OpenAI-compatible endpoint via wizard or UI.
  A failed live probe rejects the entry. Toggling a provider edits the
  canonical `GatewayConfig`, which regenerates the LiteLLM model list on
  restart (~60 s).

## Architecture

```
client ──OpenAI──▶ LiteLLM Proxy :4000 ──▶ free/local providers
  │                     │ callbacks                ▲
  │                     ▼                          │ scores/circuits
  └─Anthropic──▶ Adapter :4001            Redis :6379 ◀── Brain :4003
                 (translate /v1/messages)      ▲           (stream reader,
                                               │            scorer, breakers,
                                        Postgres :5432      health probes)
                                       (logs, registry)
```

Key modules: `gateway/config_generator.py` (registry → LiteLLM YAML),
`gateway/callbacks.py` (request logging → Postgres + Redis stream),
`brain/scorer.py` + `brain/circuit_breaker.py` (routing intelligence),
`adapter/translation.py` (full Anthropic↔OpenAI translation incl. tool use).
See [CONTRIBUTING.md](CONTRIBUTING.md) for layout details and contracts.

## Development

```bash
pip install -r requirements.txt
pytest tests/unit/           # no network needed
```

Integration tests need the Docker stack — see
[CONTRIBUTING.md](CONTRIBUTING.md). CI runs lint, pip-audit, the unit suite
with a 70% coverage gate, and secret scanning on every push/PR.

## Documentation

- [Runbook](docs/runbook.md) — operator procedures: add/remove providers,
  reset stuck circuits, backup/export config, troubleshooting table
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md) — reporting vulnerabilities
- [Contributing](CONTRIBUTING.md) · [Code of Conduct](CODE_OF_CONDUCT.md)

---

**License:** MIT — see [LICENSE](LICENSE).
