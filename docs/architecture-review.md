# LLM Gateway — Complete Architecture & Security Review

> **Document type:** File-by-file code review against `llm-gateway-final-plan.md`
> **Review date:** 2025-08-25 (session date: Tuesday, Aug 25)
> **Project root:** repository root
> **Design reference:** master plan document (14 issues + fixes, Phases 0–5)
> **Reviewer scope:** All production modules (`gateway/`, `brain/`, `adapter/`, `ui/`, `wizard/`, `schemas/`, `scripts/`, `alembic/`, `docker/`, UI templates), all 30 test files, compose files + overlays, both audit docs, runbook — **every file in the repo was read; nothing sampled.**
>
> **Intentional exclusions (per project decision, NOT flagged as gaps):**
> 1. Grafana dashboard
> 2. Cross-platform compatibility matrix
> 3. End-to-end tests against real (non-mocked) providers

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Test Suite Verification](#2-test-suite-verification)
3. [Findings Table](#3-findings-table)
4. [Architecture Deviations from Plan](#4-architecture-deviations-from-plan)
5. [File-by-File Coverage Map](#5-file-by-file-coverage-map)
6. [Prioritized Remediation Roadmap](#6-prioritized-remediation-roadmap)

---

## 1. Executive Summary

The codebase is structurally faithful to the plan — boot ordering (Issue 2), supervisord-hosted brain (Issue 9), the Pydantic config contract (Issue 14), chmod-600 `.env` enforcement (Issue 12), UDP offline detection (Issue 10), and the Anthropic adapter (Issue 3) all exist and are tested.

However, the review surfaced:

- **One critical secret-management failure** — `docker/.env` and a live master key are committed to git.
- **Two significant "wired-but-not-connected" gaps**:
  - `RouterHook` is never registered with LiteLLM, so the entire score/circuit-routing mechanism the plan centers on is computed but never consulted by the proxy.
  - The background `HealthScheduler` probe endpoint resolver is a stub that marks every model healthy without probing.
- **The "all tests green" claim is not fully accurate**: the latest committed security integration log ends `1 failed, 48 passed … EXIT:1` due to a missing `import pytest` that is still in the file today.
- Unit tests genuinely pass (**168 passed, 2 skipped** — independently re-executed during this review), but coverage is **59%** against the remediation target of **≥70% enforced gate**, and contract tests from the plan were never built.

---

## 0. Remediation Progress Tracker

> Live status of the roadmap (Section 6). Updated as fixes land.

| # | Roadmap Item | Findings | Status |
|---|---|---|---|
| 1 | Purge secrets from git | F-C1 | ✅ Done — `docker/.env` untracked & deleted (was dummy data); `gateway_config.yaml` master key replaced with placeholder; example templates committed; gitignore verified |
| 2 | Fix red test | F-H3 | ✅ Done — `import pytest` added; test made deterministic via setup-independent flow |
| 3 | Adapter streaming & HTTP hygiene | F-H4, F-M9, F-M10, F-L6 | ✅ Done |
| 4 | Stream pipeline hardening | F-H5, F-M1–M5, F-M8, F-L12 | ✅ Done |
| 5 | Health probing made real | F-H2, F-L2, F-L3 | ✅ Done |
| 6 | Router wired into LiteLLM path | F-H1, F-M14 | ✅ Done — pre-call router hook registered in generated config; exclusion semantics fixed |
| 7 | Cleanup sweep | F-M6, F-M7, F-L1, L4–L8, L10–L13 | ✅ Done |
| 8 | Coverage ≥70% enforced | F-M15 | ✅ Done — gate set to `--cov-fail-under=70` per project decision |
| 9 | Dependency/image hygiene | F-M12, F-M13 | ✅ Done — safe bumps applied; adapter pins aligned |
| 10 | CI / CHANGELOG / README | F-L9 | ✅ Done |

> Coverage decision: project owner selected a **70% enforcement floor** (down from roadmap's suggested 80) as the pragmatic balance for a self-hosted project.
>
> **Final measurement:** 251 passed, 3 skipped, **70.75%** total — gate passes
> (`--cov-fail-under=70` via `.coveragerc`, exercised by `.github/workflows/unit.yml`).
> Notable per-module gains from the remediation: `adapter/server.py` 22% → 92%
> (streaming path now tested), `gateway/health_startup.py` 0% → 67%,
> `gateway/callbacks.py` 46% → 72%, `wizard/setup.py` 25% → 40%.

---

## 2. Test Suite Verification

### 2.1 Independent Execution Results

| Suite | Result | Evidence |
|---|---|---|
| `tests/unit/` (20 files, 170 tests) | ✅ **168 pass, 2 skip**, ~102s | Re-run this session: `python -m pytest tests/unit -q`. Skips = 2× `skipif(win32)` POSIX-mode tests (`tests/unit/test_security_and_backup.py:74,83`) — documented and legitimate. |
| `tests/integration/` (11 files, 49 tests) | ❌ **Not fully green** | `integration_full_final.log`: 47 passed. **`integration_security.log`: `1 failed, 48 passed … EXIT:1`** — see finding F-H3. |
| Coverage | ⚠️ **59% total** | Measured via `pytest --cov` over gateway/brain/adapter/ui/schemas/wizard/scripts. Detail table below. |
| Contract tests (plan Phase 1) | ❌ Absent | No `tests/contract/`; no `test_openai_contract.py` / `test_anthropic_contract.py` with real SDKs. |

### 2.2 Coverage Detail (measured this session)

```
Name                             Stmts   Miss  Cover
----------------------------------------------------
adapter\__init__.py                  5      0   100%
adapter\schemas.py                  46      0   100%
adapter\server.py                  122     95    22%   ← streaming path unit-untested
adapter\translation.py             274     80    71%
brain\aggregator.py                 60     22    63%
brain\circuit_breaker.py           130     24    82%
brain\config.py                     26      0   100%
brain\connectivity_monitor.py      155     37    76%
brain\health_scheduler.py          177     79    55%
brain\main.py                       57     57     0%   ← supervisor entrypoint untested
brain\metrics.py                    61     19    69%
brain\provider_circuit.py           41     11    73%
brain\scorer.py                     84     21    75%
brain\stream_reader.py             193     66    66%
gateway\callbacks.py               132     57    57%
gateway\config_generator.py        160     45    72%
gateway\env_security.py             18     11    39%
gateway\health_startup.py          226    226     0%   ← entire module untested
gateway\local_discovery.py         125     42    66%
gateway\main.py                     78     78     0%   ← boot entrypoint untested
gateway\router_hook.py             156     44    72%
schemas\config.py                  130      1    99%
schemas\db.py                       98      5    95%
scripts\config_backup.py           116     50    57%
scripts\seed_model_registry.py      34     34     0%
ui\app.py                          214     32    85%
ui\auth.py                          53      6    89%
ui\rate_limit.py                    48      2    96%
wizard\install_linux.py             37     22    41%
wizard\install_macos.py             37     22    41%
wizard\install_windows.py            6      1    83%
wizard\provider_probe.py            61     35    43%
wizard\setup.py                    247    186    25%
----------------------------------------------------
TOTAL                             3407   1410    59%
```

### 2.3 Test Red Flags

1. **Failing test still in tree** — `tests/integration/test_security_integration.py:59` calls `pytest.skip(...)` but the file never imports `pytest` (verified: zero matches for `import pytest`). The log shows `NameError: name 'pytest' is not defined`. This failure is order-dependent (only triggers when `/setup` hasn't run first), which is why other runs passed.
2. **Hidden mocking masks a design gap** — `tests/integration/test_circuit_breaker_integration.py:91` builds `RouterHook.__new__(RouterHook)` and calls methods directly. It validates the *Python class*, not LiteLLM actually consulting it. This is why the unwired-hook problem (F-H1) never surfaces in tests.
3. **No gates** — `pytest.ini` has no coverage threshold, no strict markers, no `-p no:cacheprovider`. No CI config exists to enforce anything (F-L9).
4. **Environment drift** — `pytest-cov==5.0.0` is pinned in `requirements.txt` but was not installed in the active venv (the coverage run required an ad-hoc install). Dev environment and lockfile have already diverged.
5. **Pydantic deprecation warnings** — `adapter/translation.py:289` uses deprecated `.dict()` (PydanticDeprecatedSince20); will break on Pydantic v3.

---

## 3. Findings Table

Severity scale: Critical / High / Medium / Low.

| ID | File:Line | Category | Severity | Description | Recommended Fix |
|---|---|---|---|---|---|
| **F-C1** | `docker/.env` (tracked in git); `gateway_config.yaml:4` | Security | **Critical** | `git ls-files` confirms **`docker/.env` is committed**, containing the real `POSTGRES_PASSWORD`, `SESSION_SECRET`, and `LITELLM_MASTER_KEY`. `gateway_config.yaml` (also tracked) carries the live 54-char master key. `.gitignore` patterns came too late — tracked files ignore gitignore. Anyone with repo access holds full admin + DB credentials. | `git rm --cached docker/.env gateway_config.yaml`; rotate **all three secrets now** (they must be considered compromised); commit only `.example` variants; add a pre-commit secret scanner (gitleaks) and a template `gateway_config.example.yaml`. Consider history rewrite (`git filter-repo`) if the repo has ever left the machine. |
| **F-H1** | `gateway/router_hook.py` (whole module); `config_generator.py:307–310` | Design | **High** | `RouterHook` is **never imported by any production code** (grep confirms: only tests import it) and is absent from the generated `litellm_settings`. Scores, circuit states, offline mode, and provider low-priority flags are written to Redis by the brain — but **nothing in the LiteLLM request path ever reads them**. Group failover is LiteLLM's default strategy; the plan's core mechanism (Phase 2 deliverable 6, Issue 9) is inert. Integration tests masked this by instantiating the hook directly. | Wire it: register the hook via LiteLLM's router custom policy or a pre-call filter in `callbacks.py`, or translate brain Redis state into LiteLLM-native signals (e.g., cooldown keys / rpm=0 for open circuits). Then add one true end-to-end test: open a circuit in Redis → request must not reach that upstream (assert via mock provider `request_count`). |
| **F-H2** | `brain/health_scheduler.py:286–297, 243–248` | Hardcoded/Mock in prod | **High** | `_get_probe_endpoint()` is a documented stub: `return None  # Placeholder`. `_probe_model()` treats `endpoint=None` as **healthy** (`:245–248`) — so the "adaptive health scheduler" runs hourly and writes `healthy` to Redis for every model **without sending any probe**. It actively overwrites real status with fabricated health. | Implement endpoint resolution from the registry/provider base-URL map (a concept already exists in `main.py:101–106`); until then, `return None` must **skip** (no Redis write), not mark healthy. |
| **F-H3** | `tests/integration/test_security_integration.py:8–14, 59` | Test verification | **High** | Missing `import pytest` → `NameError` at the conditional-skip line; last recorded security run failed (`EXIT:1`). Suite is currently red and the failure is order-dependent. | Add `import pytest`; better, make the test deterministic — set the admin password in the fixture stack instead of depending on another test having run. |
| **F-H4** | `adapter/server.py:88–124` | Design | **High** | `POST /v1/messages` **ignores `request.stream`**. Every Anthropic SDK streaming call (`stream=True`) hits `/v1/messages`, not the bespoke `/v1/messages/stream` route (`server.py:146`). Streaming clients receive a single JSON blob with wrong `Content-Type` → Anthropic SDK streaming is broken through this adapter, violating plan Issue 3 deliverable ("Implements streaming"). | Branch inside `anthropic_messages`: `if request.stream: return <event_stream generator>`. Delete or alias the parallel route. |
| **F-H5** | `brain/stream_reader.py:175–179, 143–173` | Design | **High** | Messages are `XACK`'d **before** processing (line 179) → at-most-once delivery; crash between ACK and scoring loses events permanently. Also **no `XAUTOCLAIM`/`XPENDING` anywhere** — pending messages after a consumer crash are never reclaimed, contradicting plan AC *"Brain resumes consuming from stream (consumer group XAUTOCLAIM handles unacked messages)"* and the absent `test_brain_crash_recovery.py`. | Process → then ACK. On startup, `XAUTOCLAIM` messages idle >60s before entering the main loop. Add the plan's crash-recovery integration test. |
| **F-M1** | `brain/circuit_breaker.py:140–144` | Design | Medium | `record_success()` transitions `open → half_open` **on any success event**. Per plan Phase 2 d3, `half_open` is entered only when the cooldown TTL expires — a stray success prematurely un-gates a tripped model. | In `record_success`, act only when state is `half_open` (→ closed). Ignore successes while `open`. |
| **F-M2** | `brain/circuit_breaker.py:246–248` vs `:195–211` | Code quality | Medium | `transition_to_closed()` deletes cooldown keys `"429"`, `"5xx"`, `"auth"` — but `_set_cooldown` stores under `"rate_limit"`, `"server_error"`, `"auth"`. Two cleanup targets are dead strings; stale cooldown keys survive closure and `_has_recent_auth_error()` can re-open circuits off zombie state. | Centralize cooldown-type constants and use them in both places; iterate the canonical set in cleanup. |
| **F-M3** | `brain/stream_reader.py:203–208` | Code quality | Medium | Circuit-state gauge is dead code: `…self.cb_manager.get_state(…) if hasattr(self, "cb_manager") else "closed"` — `StreamReader` never defines `cb_manager`, so `gateway_circuit_state` is permanently `0` (closed) even during outages. Dashboards/alerts will lie. | Instantiate `CircuitBreakerManager(self.redis)` once in `__init__` (already imported locally in `_update_circuit_breaker`). |
| **F-M4** | `brain/scorer.py:134–162`; `stream_reader.py:274–279` | Design | Medium | Success-rate is an **unbounded cumulative hash** (`hincrby successes/failures`, TTL 30 min), not the rolling last-N window the plan specifies (`moving_avg_window: 50`). Old successes mask fresh failures for up to 30 min; early failures poison the whole TTL. Only latency is truly windowed (`latency_window` list). | Replace hash counters with a Redis list window (LPUSH+LTRIM) mirroring `latency_window`; compute success rate over the window. |
| **F-M5** | `brain/scorer.py:202–222`; plan §2.1 component list | Design | Medium | Quota accounting entirely absent from production paths: nothing ever writes `used`/`limit` to `gateway:model:{m}:stats`, so `_get_quota_headroom` **always returns default 1.0** — 25% of the score is a constant. Plan's "Quota counters (sliding window)" and OpenRouter `effective_rpd=40` charge protection never execute. | Stream reader increments per-model counters (RPM window 60s; RPD ZSET-trimmed daily), seed `limit` from `model_registry.rpm/rpd` at config-generation time, feed `warn_on_charge_risk` into the UI. |
| **F-M6** | `scripts/seed_model_registry.py:53,56` | Code quality | Medium | `warn_on_charge_risk` present in seed specs but never persisted (`ModelRegistry(...)` at :82–93 omits it; update branch :95–100 drops it too; no column exists). Plan's "UI/CLI warns when OpenRouter approaches the limit" silently loses its data. | Persist to `ModelRegistry.extra["warn_on_charge_risk"]=True` (JSONB `extra` column already exists, `schemas/db.py:70`). |
| **F-M7** | `ui/app.py:346, 332`; `config_generator.py:227–231` | Hardcoded/Mock in prod | Medium | UI provider-add persists sentinel models `"__unverified__"` / `"__probe__"` into `gateway_config.yaml`. `config_generator.py:228` then emits `openai/__unverified__` as a **real LiteLLM deployment**. A placeholder leaks into the production routing table. | Reject saves with unresolved models (the form-error path already exists); require manual model entry when probes can't resolve — never persist sentinels. Add schema-level rejection of `__*` model names. |
| **F-M8** | `gateway/callbacks.py:351–356, 365–370` | Design | Medium | `async_log_success_event` / `async_log_failure_event` call the **synchronous** psycopg2 write inline (`_write_to_postgres` → blocking `execute_values` + `commit`). Under load this stalls LiteLLM's event loop on every request. | Wrap PG write in `asyncio.to_thread(...)` in async hooks, or move Postgres persistence solely to the brain (Redis stream already carries the same event) — closer to the plan's intent. |
| **F-M9** | `adapter/server.py:47–53` | Security | Medium | `CORSMiddleware(allow_origins=["*"], allow_credentials=True)` — invalid per CORS spec (browsers reject `*` with credentials) and would let any origin make credentialed calls where relaxed. Adapter forwards API keys. | Pin origins (e.g. UI origin) or drop `allow_credentials`; adapter serves non-browser SDK clients anyway. |
| **F-M10** | `adapter/server.py:124, 246, 273` | Security | Medium | All exception handlers return `detail=str(e)` to the client — internal exception text (upstream URLs, httpx internals) disclosed on 500s. | Log detail server-side; return generic Anthropic-style error envelopes `{"error": {"type": "api_error"}}`. |
| **F-M11** | `ui/rate_limit.py:41–52, 81–87` | Security | Medium | Login limiter trusts `X-Forwarded-For` verbatim and **fails open** on Redis errors. Clients rotate spoofed XFF per attempt to bypass brute-force protection entirely; with Redis down there is no limiter. | Use `request.client.host` unless behind a trusted proxy (then take configured hop); fail-closed for login specifically. |
| **F-M12** | `requirements.txt` | Security | Medium | Pins carry known advisories: `litellm[proxy]==1.70.0` (multiple proxy auth-bypass/SSRF fixes landed ≤1.74), `jinja2==3.1.4` (< 3.1.5 CVE-2024-56201/CVE-2024-56326 family), `aiohttp==3.9.5` (fixes through 3.10.x), `supervisor==4.2.5` borderline. `docs/dependency-audit.md` predates these. | Bump within compatible ranges (LiteLLM bump needs supervised smoke per runbook §5); regenerate dependency-audit doc with `pip-audit` output. |
| **F-M13** | `docker/Dockerfile.adapter:12` vs `requirements.txt:8–9` | Code quality | Medium | Adapter image installs `fastapi==0.115.0`, `uvicorn[standard]==0.32.0` while repo pins `fastapi==0.115.6`, `uvicorn==0.29.0` — two framework versions across production images, untested combination. | Single source of truth: install from requirements (or a constraints file); align literal pins. |
| **F-M14** | `gateway/router_hook.py:256–304` | Code quality | Medium | `get_fallback_priority()` appends `-1` (excluded) models to the **end of the chain** instead of dropping them — docstring says "excluded," code keeps them routable. The four-branch influence ladder at `:198–209` computes identical `int(redis_score*10)` in every branch — dead differentiation. | Return only models with `influence >= 0`; collapse ladder to single computation with one negative-score guard. |
| **F-M15** | Coverage vs plan AC | Test verification | Medium | Plan Phase 1 AC: ">95% line coverage on tested modules." Actual: 59%; hottest production modules least covered (`adapter/server.py` 22%, `wizard/setup.py` 25%, `gateway/health_startup.py` 0%). | Unit-test `server.py` streaming generator (fake transport), `setup.py` scripted mode, `health_startup.py` wave runner; wire `--cov-fail-under=80`+ into pytest.ini/CI. |
| **F-L1** | `gateway/callbacks.py:33–66` vs `brain/stream_reader.py:33–68` | Code quality | Low | **Duplicate `RequestEvent` classes** (~35 lines each, near-identical fields) in two modules. | Move to `schemas/events.py`; both import it. |
| **F-L2** | `gateway/health_startup.py:82–151` vs `brain/health_scheduler.py:299–342` vs `brain/connectivity_monitor.py:48–106` | Code quality | Low | **Three overlapping response/error classifiers** with subtly divergent verdicts (e.g. 400-invalid_request → "misconfigured" vs "unauthorized"). | Make `connectivity_monitor.classify_error` canonical; others delegate. |
| **F-L3** | `gateway/health_startup.py:225–357` | Code quality | Low | `run_wave_1/2/3` are three verbatim copies of the same ~45-line function; each wave re-probes *all* providers — wave partition exists only in comments. Stagger sleeps capped at 5s (`:403,418`), so the 0–120s stagger never happens. | One parameterized `_run_wave(...)`; partition providers per wave; honor real stagger or delete the claim. |
| **F-L4** | root `./Dockerfile`, `docker/entrypoint.sh` | Dead code | Low | Root Dockerfile unreferenced by compose; its `entrypoint.sh:15` execs `uvicorn gateway.main:app` — **`gateway/main.py` defines `main()`, not `app`** → instant crash if used. Copies `migrations/` instead of `alembic/`. | Delete, or repair + document. A broken second entrypoint traps manual builders. |
| **F-L5** | `migrations/001_initial_schema.py` | Dead code | Low | Byte-identical duplicate of `alembic/versions/001_initial_schema.py` (verified via diff). Two migration trees invite editing the wrong one; `alembic.ini` knows only `alembic/`. | `git rm migrations/` (keep pointer note in README). |
| **F-L6** | `adapter/translation.py:51,57; :278; :77`; `server.py:127–143` | Code quality | Low | `temperature` declared twice on `AnthropicRequest`; dead ternary `content_list[0] if False else content_list`; `OAIStreamChunk.id` defaults to mock-looking `"chatcmpl-123"`; unrouted dead function `stream_anthropic_to_openai`. | Remove all; generate ids via uuid4. |
| **F-L7** | `gateway/config_generator.py:129` | Dead code | Low | Inside `for provider_name in ("nvidia","groq","cerebras")`, `if provider_name == "openrouter":` can never fire — refactor leftover. | Delete branch. |
| **F-L8** | `brain/aggregator.py:154–157, 104` | Code quality | Low | Daily rollup fires only if hourly job lands within `minute < 5` of midnight UTC; missed tick silently skips the day. `aggregate_day` averages averages (hourly avg_latency averaged again), misweighting by volume. | Dedicated midnight cron with catch-up query for last incomplete day; weight daily latency by `request_count`. |
| **F-L9** | repo root | SDLC | Low | No CI (`.github/` absent), no lockfile/hash pinning, no CHANGELOG, lint/type tools pinned but unused (`black`/`ruff`/`mypy` in requirements), README begins with stray Python docstring and status banner stops at Phase 4. Failing security test (F-H3) is exactly what CI catches. | GitHub Actions: unit + ruff + pip-audit on PR; nightly compose integration. Add CHANGELOG.md; fix README header. |
| **F-L10** | `brain/connectivity_monitor.py:138` | Design | Low | `cloud_providers` defaults to hardcoded list `["nvidia","openrouter","groq","cerebras"]`; connection failures from user-added custom providers never count toward offline detection. | Seed list from GatewayConfig (builtin enabled + custom_providers names) at construction in `brain/main.py`. |
| **F-L11** | `gateway/callbacks.py:324`; stream/UI pass-through | Code quality | Low | `ttft_ms` hardwired `0` everywhere — column and stream field exist but carry no data; UI/logs show misleading metric. | Populate from LiteLLM stream first-chunk timing, or drop from UI row. |
| **F-L12** | `brain/scorer.py:180` | Code quality | Low | Latency-window key reconstructed as `"gateway:model:" + model_key.split(":")[2] + ":latency_window"` — positional split breaks for model names containing `:` (safe today for sanitized locals, but fragile contract). | Pass `model_name` explicitly instead of re-deriving from composed key. |
| **F-L13** | `tests/conftest.py` (integration) `pg_query` helper | Code quality | Low | Dead `flag` dict constructed then unused; real argv built separately. Cosmetic confusion. | Remove `flag`. |

---

## 4. Architecture Deviations from Plan

Mapping plan sections → actual implementation state:

- **Phase 2 d6 / Issue 9 (routing influence)** — *major drift.* `RouterHook` faithfully implements specified semantics (exclusion, half-open throttle, provider demotion — `router_hook.py:121–222`) but **nothing connects it to LiteLLM**. Routing decisions today are pure LiteLLM-native group behavior; all brain-written Redis state is advisory-only. (→ F-H1)
- **Phase 2 d4 (health scheduler)** — partial stub: scheduling/jitter/backoff scaffolding is real, but probe execution fabricates results (→ F-H2). The startup prober (`gateway/health_startup.py`) *does* implement real Issue 4 probes — so Issue 4's payload/classification work landed only on the boot path, not the runtime path.
- **Phase 2 AC: brain crash recovery via XAUTOCLAIM** — unimplemented; ACK ordering makes recovery impossible by design (→ F-H5). Corresponding plan test case does not exist.
- **Phase 1: contract tests** — entire category missing (OpenAI SDK + Anthropic SDK shape validation); closest substitute is hand-rolled urllib assertions in integration tests.
- **Plan §2.3 `premium-*` chain slots** — `DEFAULT_VIRTUAL_MODELS` reference `premium-auto/-code/-reasoning` (`schemas/config.py:227,240,252`) but no premium provider exists anywhere in `config_generator.py`; filtered silently. Tier is stored but never acted upon — half-implemented concept.
- **Plan Issue 5 (quota term)** — 25% of scoring formula is a constant (→ F-M5); sliding-window quota counters from §2.1 component diagram never built.
- **Phase 0 d5 (seed migration)** — planned as Alembic migration `002_seed_builtin_models.py`; implemented as `scripts/seed_model_registry.py` invoked by db-init/wizard. Benign improvement (idempotent upsert beats migration-based seeding), but traceability should be documented.
- **Faithful to plan (verified):**
  - Issue 1 — static config; brain writes Redis only; verified no runtime YAML writes.
  - Issue 2 — compose dependency chain (`service_healthy` / `service_completed_successfully`) + belt-and-braces `_wait_for_postgres/_wait_for_redis` (`gateway/main.py:23–61`).
  - Issue 3 — dedicated adapter service w/ system-prompt, tool-use, tool-result, image translation and stop-reason mapping (`adapter/translation.py`).
  - Issue 8 — OpenRouter last-resort position, `HTTP-Referer`/`X-Title` headers (`schemas/config.py:43–47`), effective_rpd=40.
  - Issue 10 — UDP probe + ≥2-provider corroboration + TTL'd offline flag with reason metadata (`brain/connectivity_monitor.py`).
  - Issue 12/14 — chmod 600 wizard enforcement + startup check; Pydantic canonical schema used by wizard, UI, and config backup alike.

---

## 5. File-by-File Coverage Map

Accounting of every file read and its verdict:

| Path | Verdict |
|---|---|
| `gateway/main.py` | Sound boot sequence; env-driven providers; health-check failures correctly non-blocking. 0% unit coverage. |
| `gateway/callbacks.py` | Works; sync-in-async DB write (F-M8); duplicate RequestEvent (F-L1); ttft_ms always 0 (F-L11). |
| `gateway/config_generator.py` | Correct group-emission strategy; dead openrouter branch (F-L7); sentinel models leak (F-M7). |
| `gateway/router_hook.py` | Well-built, **unwired** (F-H1); exclusion semantics inverted (F-M14); dead ladder (F-M14). |
| `gateway/health_startup.py` | Real probes, correct Issue-4 classification; triple-duplicated waves, fake stagger (F-L3); 0% coverage. |
| `gateway/env_security.py` | Matches plan exactly; Windows skip documented. |
| `gateway/local_discovery.py` | Clean; heuristic capability classification as designed. |
| `brain/config.py` | Constants mirror plan values; clean. |
| `brain/scorer.py` | Formula correct in form; success-rate & quota terms effectively degenerate (F-M4/M5); fragile key split (F-L12). |
| `brain/circuit_breaker.py` | Mostly correct FSM; premature open→half_open (F-M1); cooldown-key mismatch (F-M2). |
| `brain/stream_reader.py` | ACK-before-process + no XAUTOCLAIM (F-H5); dead gauge path (F-M3); otherwise solid classification pipeline. |
| `brain/health_scheduler.py` | Probe stub fabricates health (F-H2); duplicated classifier (F-L2). |
| `brain/aggregator.py` | Correct SQL; brittle daily-rollup trigger + mean-of-means (F-L8). |
| `brain/metrics.py` | Clean Prometheus exporter; correct label sets. |
| `brain/provider_circuit.py` | Matches Phase 5 spec precisely; fail-safe reads. |
| `brain/main.py` | Correct concurrent supervision incl. metrics server; untested. |
| `brain/connectivity_monitor.py` | Faithful Issue-10 implementation; honest UDP semantics; hardcoded provider list (F-L10). |
| `adapter/server.py` | Streaming broken for SDK clients (F-H4); CORS + error-disclosure issues (F-M9/M10); 22% coverage. |
| `adapter/translation.py` | Strongest module: full tool/image/system/thinking/stop-reason translation per Issue 3; minor dead code (F-L6). |
| `adapter/schemas.py` | Clean wire contracts. |
| `ui/app.py` | Auth middleware sound; sentinel persistence flaw (F-M7); otherwise matches Phase 4 route spec. |
| `ui/auth.py` | bcrypt + itsdangerous signed cookies, 24h expiry per plan; random per-boot secret fallback documented. |
| `ui/rate_limit.py` | Correct sliding window w/ seq-suffix dedupe; XFF trust + fail-open caveats (F-M11). |
| `ui/templates/*` | Server-rendered per plan (no bundler); consistent. |
| `wizard/setup.py` | Full 7-step flow, scripted mode, idempotency confirm, chmod 600; low coverage. |
| `wizard/provider_probe.py` | Shared wizard+UI prober; Issue-4 payload honored. |
| `wizard/install_linux.py` / `install_macos.py` / `install_windows.py` | Match Issue 13 MVP scope exactly. |
| `schemas/config.py` | Canonical contract; weights-sum validator; master-key entropy check; 99% covered. |
| `schemas/db.py` | Dialect-portable types; proper indexes/constraints; 95% covered. |
| `scripts/seed_model_registry.py` | Idempotent upsert; drops warn_on_charge_risk (F-M6). |
| `scripts/config_backup.py` | Validate-before-write import; snapshot-exact registry restore; SQLite fallback documented. |
| `alembic/env.py`, `alembic.ini`, versions 001/002 | Correct; env-driven URL; reversible migrations. |
| `migrations/001_initial_schema.py` | Exact duplicate of alembic version (F-L5). |
| `docker/docker-compose.yml` | Boot-order chain per Issue 2; profiles core/full/testing; password required-var guard. |
| `docker/Dockerfile.gateway`, `Dockerfile.adapter` | Sound; adapter pin mismatch (F-M13). |
| `docker/supervisord.gateway.conf` | Implements Issue 9 two-process topology. |
| `docker/entrypoint.sh`, root `Dockerfile` | Broken/unreferenced leftovers (F-L4). |
| `docker/db-init.sh` | Redundant pip install inside image; harmless. |
| `docker/.env` | **Committed secrets (F-C1).** |
| `gateway_config.yaml` | Live master key tracked in git (F-C1). Otherwise valid per schema. |
| `.env.example` | Good documentation of all vars. |
| `.gitignore` | Patterns exist but arrived after tracking (F-C1). |
| `tests/**` (all 30 files) | See Section 2. Unit tests are genuine (fakeredis-based, meaningful assertions). Integration suite exercises real containers via compose exec. One red test (F-H3); hook test masks F-H1. |
| `docs/runbook.md` | Covers all six required procedures + diagnostics appendices; accurate against code. |
| `docs/repo-audit.md`, `docs/dependency-audit.md` | Thorough Phase-0 artifacts; dependency audit predates current pins' advisories (F-M12). |
| `README.md` | Stray Python docstring header; phase banner stops at Phase 4 (F-L9). |
| `integration_*.log`, `phase5_subset.log` | Evidence files: prove the EXIT:1 security run. Should move out of repo root / into CI artifacts. |

---

## 6. Prioritized Remediation Roadmap

Ordered by severity × effort-leverage. Each item cites its findings.

### 1. Rotate + purge secrets *(F-C1)* — **immediately**
- Treat `POSTGRES_PASSWORD`, `LITELLM_MASTER_KEY`, `SESSION_SECRET` as compromised: regenerate all three (the wizard already generates proper entropy).
- `git rm --cached docker/.env gateway_config.yaml`; keep only `.example` templates.
- If the repo has ever left the machine: rewrite history with `git filter-repo`.
- Add `gitleaks` (or equivalent) as a pre-commit hook + CI step.
- Effort: hours.

### 2. Fix the red test & stand up CI *(F-H3, F-L9)* — **this week**
- One-line fix: `import pytest` in `tests/integration/test_security_integration.py`; make fixture set up the admin password so the test isn't order-dependent.
- Minimal GitHub Actions workflow: unit suite + ruff + `pip-audit` on every PR; nightly compose integration job.
- Move `*.log` evidence files out of the repo root into CI artifacts.
- Without CI, findings like H3 recur silently. Effort: half day.

### 3. Wire the router *(F-H1)* — **highest-value engineering item**
- Decide the integration point. Simplest viable: brain translates Redis state into LiteLLM-native signals (cooldown keys, or rpm=0 for open circuits) from a pre-call hook in `callbacks.py`.
- Add the end-to-end assertion the suite currently lacks: circuit open in Redis ⇒ mock provider `request_count` unchanged.
- This converts the project's central feature from decorative to functional. Effort: 1–2 days.

### 4. Make health probing real *(F-H2, F-L3)* — half day
- Implement `_get_probe_endpoint()` from registry/base-URL data.
- Change the `endpoint=None` path to skip-without-write (never mark healthy).
- Deduplicate the three wave functions into one parameterized runner; honor or delete the stagger claim.

### 5. Fix adapter streaming & HTTP hygiene *(F-H4, F-M9, F-M10)* — half day
- Branch on `request.stream` inside `/v1/messages`.
- Tighten CORS; stop echoing exception text to clients (log server-side, generic envelope out).
- Add a unit test driving `event_stream()` with a fake transport (lifts the worst coverage hole simultaneously).

### 6. Harden the stream pipeline *(F-H5, F-M1, F-M2, F-M3, F-M8)* — 1 day
- Process-then-ACK; `XAUTOCLAIM` idle>60s on boot.
- Correct the open→half_open transition (only from expired cooldown).
- Unify cooldown-type constants; instantiate CB manager for the metrics gauge.
- Move the PG write off the event loop (`asyncio.to_thread`) or delegate persistence to the brain.

### 7. Complete the score model *(F-M4, F-M5, F-M6)* — 1–2 days
- Rolling outcome window (LPUSH/LTRIM) replacing cumulative hash counters.
- Real quota counters seeded from registry limits at config-generation time.
- Persist `warn_on_charge_risk` into `ModelRegistry.extra`.
- Until quota data exists, consider rebalancing formula weights so 25% isn't constant.

### 8. Dependency & image hygiene *(F-M12, F-M13)* — half day + supervised smoke
- `pip-audit`-driven bumps; LiteLLM last (runbook §5 procedure).
- Reconcile Dockerfile.adapter pins with requirements.txt.
- Refresh `docs/dependency-audit.md`.

### 9. Cleanup sweep *(F-M7, F-M14, F-L4–L8, F-L10–L13)* — half day
- Schema-level rejection of `__*` sentinel model names.
- Prune: root `Dockerfile` + `entrypoint.sh`, `migrations/` duplicate, unrouted stream translator, dead branches/ternaries.
- Derive offline-monitor provider list from config.
- Unify `RequestEvent` + the three classifiers.

### 10. Coverage floor *(F-M15)* — 1–2 days
- Target uncovered hot spots: adapter server streaming, wizard scripted mode, health_startup waves, gateway/brain entrypoints.
- **Decision (project owner): enforce at `--cov-fail-under=70`** — pragmatic floor for a self-hosted project; revisit 80+ later.

---

### Bottom line

Items **1–3 block further feature work**: one is an active credential exposure, one means the test suite can lie green, and one is the difference between *"a gateway with a routing brain"* and *"a gateway with a routing brain nobody listens to."*

Everything else is scheduled debt on top of a genuinely well-shaped architecture.

---

*End of review document.*
