# LLM Gateway Phase 1 — Core Gateway (Static Routing, Cloud Free Only)

**Trigger:** Use when implementing Phase 1 after Phase 0 schema/migrations. Covers static routing, Docker Compose with health checks, LiteLLM config generation, 3-wave health probes, Anthropic adapter on port 4001, CLI wizard, and all 14 master plan issue fixes.

**When to Use:** Starting Phase 1 after Phase 0. Follows corrected phase order: Foundation → Gateway → Brain → Local → UI → Hardening. All 14 master plan issues resolved per master plan Part 2.

**What it does:** Complete Phase 1 deliverables: Docker Compose stack (Postgres/Redis + health checks + service dependencies), GatewayConfig-reading config generator (generates LiteLLM model_list YAML), CustomLogger writing to Postgres + Redis stream, 3-wave staggered health checks with structured probe payload (not "ping"), Anthropic Inbound Adapter FastAPI (port 4001) with full translation (system prompts, tool use, tool results, images, stop reasons, streaming), CLI wizard (.env chmod 600 + gateway_config.yaml from GatewayConfig defaults + DB init + model registry seed), static fallback chains (auto-free, auto-code-free, auto-reasoning-free).

**Does NOT do:** Dynamic config reload (state in Redis only), dynamic model addition, UI/Docker modes (Phase 5), local models (Phase 3), circuit breaking (Phase 2).

**Schema:** GatewayConfig Pydantic model (canonical from Phase 0). All tunable thresholds in gateway_config.yaml under routing_defaults:. Version 1.0.0, MIT license.

**Class-level skill** — reusable pattern across LLM Gateway projects.