# Contributing to LLM Gateway

Thanks for your interest! LLM Gateway is a self-hosted, free-first LLM routing
gateway: a LiteLLM proxy (port 4000), an Anthropic↔OpenAI adapter (4001), a
FastAPI web UI (4002), and a Redis-backed "routing brain" (scoring, circuit
breakers, health probes), with state in Postgres.

## Development Setup

Requirements: **Python ≥ 3.11**, Docker + Docker Compose, git.

```bash
git clone https://github.com/vijaytank/llm-gateway.git
cd llm-gateway
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env          # or run the wizard: python wizard/setup.py
```

You do **not** need cloud provider keys for most development — unit tests use
fakeredis and mocks.

## Running Tests

```bash
pytest tests/unit/            # fast, no network — run before every PR
pytest tests/integration/     # requires the compose testing stack; see below
```

Integration tests boot their own overlay stack:

```bash
docker compose -f docker/docker-compose.yml \
               -f tests/integration/docker-compose.testing.yml \
               --profile full --profile testing up -d --wait
pytest tests/integration/
```

Rules:
- Never run two pytest sessions against one compose stack concurrently.
- Run the boot-order subset first if debugging startup failures.
- Coverage gate is **70%** (`--cov-fail-under=70`), enforced in CI.

## Project Layout

See the Repository Layout section of the [README](README.md). Key contracts:

- `schemas/config.py` (`GatewayConfig`, Pydantic v2) is the single source of
  truth for configuration — validate changes through
  `GatewayConfig.model_validate()`.
- Routing thresholds belong in `gateway_config.yaml` under `routing_defaults:`.
- Provider API keys are referenced **by env-var name only**, never inlined.

## Pull Requests

1. Branch from `Develop`; keep PRs focused on one concern.
2. Run `ruff check gateway brain adapter ui schemas wizard scripts tests`
   and the unit suite; CI runs both plus `pip-audit` and a secret scan.
3. Update `CHANGELOG.md` under an "Unreleased" heading.
4. If you change `schemas/config.py`, check whether
   `alembic/versions/` or `gateway_config.yaml` need a matching migration.
5. New user-facing behavior needs a unit test and a README/runbook touch.

## Code Style

- Type hints on public functions; module-level docstring in every new module.
- Fail-safe bias: routing-state reads (Redis) must degrade gracefully when the
  brain is down — see `gateway/router_hook.py` for the established pattern.
- No hardcoded secrets or absolute personal paths anywhere, including tests
  and docs.

## Reporting Bugs

Open a GitHub issue with the bug template. For security issues, see
[SECURITY.md](SECURITY.md) instead.

By participating in this project you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).
