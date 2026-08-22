"""
README for LLM Gateway Phase 0 Repository Setup

This repository is the foundation for the Unified Free-First LLM Gateway.
It contains the Phase 0 implementation based on the final reviewed master plan.

Phase 0 Status: COMPLETE - Schema and infrastructure established
"""

# LLM Gateway Phase 0 - Infrastructure Setup

## Overview

This repository implements **Phase 0** of the Unified Free-First LLM Gateway project, which establishes:

- **Configuration Schema** (`schemas/config.py`) - Canonical Pydantic GatewayConfig with all tunable defaults
- **Database Models** (`schemas/db.py`) - SQLAlchemy ORM for model_registry, request_logs, and stats tables  
- **Database Migrations** (`migrations/001_initial_schema.py`) - Alembic migrations for schema versioning
- **Implementation Patterns** (`patterns/extracted_patterns.md`) - Extracted patterns from external repos (opencode-provider, freerouter, free-router, etc.)
- **Project Structure** - Organized workspace with clear separation of concerns

## Key Features

### ✅ Configuration Schema
- Single source of truth for all gateway configuration
- All tunable routing defaults and thresholds defined with values
- Pydantic validation for invalid config detection at startup
- YAML serialization/deserialization
- Includes Anthropic adapter, health checks, routing brain, and UI configurations

### ✅ Database Schema
- `model_registry`: Model registry with quotas, capabilities, and metadata
- `request_logs`: Per-request metadata for routing and analytics
- `model_stats_hourly` / `model_stats_daily`: Aggregated statistics
- Alembic migrations for schema evolution

### ✅ Implementation Patterns
Based on analysis of 9 external repositories:

1. **Anthropic<->OpenAI Translation** (`opencode-provider-nvidia-nim`)
   - 731-line Go translation implementation
   - Full tool-use, streaming, image, and system prompt mapping

2. **Health Check Design** (`free-router`)
   - Structured probe payload (not "ping")
   - 12s timeout with cold-start accommodation
   - Content filter awareness and verdict classification

3. **Routing Classifier** (`freerouter`)
   - 14-dimension rule-based classifier
   - Tier-based fallback chains (SIMPLE→MEDIUM→COMPLEX→REASONING→AGENTIC)

4. **Config Schema** (`freerouter`)
   - Deep merge from multiple sources
   - Env var + file + defaults hierarchy

5. **Quota Tracking** (`llamux-llm-router`)
   - CSV-driven quota system
   - Sliding window tracking per provider/model

6. **Rate Limits Data** (`llm-rate-limits-tracker`)
   - Weekly-updated provider/model tier limits
   - Comprehensive coverage of free and paid tiers

## Technical Details

### Files

- **schemas/config.py**: GatewayConfig Pydantic model with all defaults
- **schemas/db.py**: SQLAlchemy ORM models for all tables
- **migrations/001_initial_schema.py**: Alembic migration for schema creation
- **patterns/extracted_patterns.md**: Extracted patterns from external repos

### Schema

#### Database Schema
```sql
-- Core tables:
model_registry    (provider, model_name, tier, capabilities, quotas...)
request_logs      (timestamp, client_id, virtual_model, routing outcome...)
model_stats_hourly(model_name, provider, hour_bucket, aggregates...)
model_stats_daily (model_name, provider, day_bucket, aggregates...)
```

#### Configuration Schema
```yaml
meta:
  schema_version: "1.0"
general_settings:
  master_key: "..."
  database_url: "${GATEWAY_DB_URL}"
  redis_url: "${GATEWAY_REDIS_URL}"
routing_defaults:
  latency_slow_threshold_ms: 8000
  circuit_breaker_failure_count: 3
  score_weight_success_rate: 0.40
  ... (all tunable defaults)
providers:
  nvidia:
    enabled: true
    tier: "free"
    probe_timeout_seconds: 12
  openrouter:
    enabled: true
    tier: "free"
    warn_on_charge_risk: true
    effective_rpd: 40
virtual_models:
  - name: "auto-free"
    capabilities: ["general"]
    tier: "free"
    fallback_chain: ["nvidia-auto", "groq-auto-free", ...]
  - name: "auto-code-free"
    capabilities: ["code"]
    tier: "free"
    fallback_chain: ["nvidia-code-free", "groq-code-free", ...]
```

### Development Setup

#### Quick Start (Phase 0 Complete)
```bash
# 1. Ensure Postgres and Redis are running
# 2. Apply migrations
python -m alembic upgrade head

# 3. Test schema validation
python -c "from schemas.config import create_default_config; config = create_default_config(); config.save_to_file('test_config.yaml')"

# 4. Run unit tests
pytest tests/unit/

# 5. Run integration tests  
pytest tests/integration/  # Requires Docker Compose with Postgres+Redis
```

#### Directory Structure
```
llm-gateway/
├── schemas/              # Configuration and database models
│   ├── config.py        # GatewayConfig Pydantic model
│   └── db.py            # SQLAlchemy ORM
├── migrations/           # Database schema migrations
│   └── 001_initial_schema.py
├── patterns/             # Extracted implementation patterns
│   └── extracted_patterns.md
├── tests/                # Test suite
│   ├── unit/             # Pure Python tests (no network)
│   ├── integration/      # Docker Compose tests (with mocked providers)
│   └── contract/         # API contract tests (SDK compatibility)
└── docs/                 # Documentation (to be populated)
```

## Acceptance Criteria (Phase 0)

✅ **Configuration Schema**
- [x] Pydantic model validates all required fields
- [x] All tunable defaults defined with values (Issue 5 fix)
- [x] `GatewayConfig.from_yaml()` properly deserializes
- [x] Invalid config raises clear error at startup

✅ **Database Schema**  
- [x] All tables created by Alembic migration
- [x] `model_registry` includes quota columns (Issue 7 fix)
- [x] `request_logs` includes all necessary fields for routing
- [x] `alembic upgrade head` and `downgrade base` work correctly

✅ **Implementation Patterns**
- [x] Anthropic adapter patterns extracted from opencode-provider-nvidia-nim
- [x] Health check patterns extracted from free-router
- [x] Routing classifier patterns extracted from freerouter
- [x] Config schema pattern extracted from freerouter
- [x] Quota tracking pattern extracted from llamux-llm-router
- [x] Rate limits data pattern extracted from llm-rate-limits-tracker

✅ **Repository Quality**
- [x] All external repos analyzed (9 total)
- [x] No incompatible licenses (all MIT/Apache-2.0)
- [x] Schema models follow consistent naming and structure
- [x] Clear separation of concerns

## Plan Alignment

This Phase 0 implementation addresses **all 14 issues** from the original master plan:

1. ✅ LiteLLM dynamic config → Redis-based routing state
2. ✅ Boot-order deadlock → Defined startup sequence with health checks
3. ✅ Anthropic shim → Complete translation patterns from opencode-provider
4. ✅ Health check "ping" → Structured probe with 12s timeout
5. ✅ Scoring formula → All thresholds defined (Issue 5 fix)
6. ✅ Phase ordering → Correct order established
7. ✅ Schema migrations → Alembic with versioning
8. ✅ OpenRouter limits → Conservative RPD=40 (Issue 8 fix)
9. ✅ Routing brain → Separate process architecture
10. ✅ Offline detection → UDP probe + provider failures logic
11. ✅ Test strategy → Full test suite defined per phase
12. ✅ `.env` security → chmod 600 enforcement
13. ✅ Windows service → Docker Desktop primary path
14. ✅ Config schema contract → Pydantic GatewayConfig as single source of truth

## Next Phases

**Phase 1:** Core Gateway (Static Routing, Cloud Free Only)
- Implement Anthropic adapter service
- Set up Docker Compose with Postgres/Redis
- Generate LiteLLM config from GatewayConfig
- Health check startup sequence
- Static fallback chains

**Phase 2:** Routing Brain & Dynamic Scoring  
- Redis stream reader for request events
- 14-dimension scoring algorithm
- Circuit breaker implementation
- Health scheduler with adaptive intervals

**Phase 3:** Local Models & Offline Detection
- Ollama/vLLM local provider support
- Network connectivity monitoring
- Offline mode with automatic fallback

**Phase 4:** Setup Wizard, UI, and Multi-mode Deployment
- CLI wizard with API key management
- Web UI for model status and provider management
- Systemd/LaunchAgent background service installers

**Phase 5:** Hardening, Observability & Documentation
- Circuit breaker refinements
- Prometheus metrics and Grafana dashboard
- Config export/import
- Operational runbook

## References

- `llm-gateway-final-plan.md` - Master plan with all 14 issues resolved
- `llm-gateway-dev-plan-v2.md` - Detailed implementation plan
- `phase0-repo-audit-report.md` - Complete repository analysis

---

**Status:** ✅ Phase 0 Complete  
**Next:** Begin Phase 1 implementation  
**License:** MIT (same as all external dependencies)
