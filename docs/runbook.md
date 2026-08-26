# LLM Gateway — Operational Runbook

Procedures for a self-hosting operator. All commands run from the repo root
unless noted.

**Production** stack:

```bash
docker compose -f docker/docker-compose.yml --profile full <cmd>
```

**Development/testing** adds an overlay with mock providers and test hooks:

```bash
docker compose -f docker/docker-compose.yml \
               -f tests/integration/docker-compose.testing.yml \
               --profile full --profile testing <cmd>
```

---

## 1. How to add a provider

### Via the UI (preferred)
1. Log in at `http://localhost:4002`.
2. Go to **Providers → Add provider**.
3. Fill in: name, base URL, auth type, model list (or "detect via ping"), free/premium tier.
4. Submit — the UI probes the endpoint first; an invalid/unreachable provider is rejected.
5. The gateway regenerates its LiteLLM config and restarts gracefully; the new
   provider is routable within ~60 seconds.

### Via config file
1. Edit `gateway_config.yaml`: add the provider under `providers:` and its
   models under the relevant `virtual_models[].fallback_chain`.
2. Restart the gateway: `docker compose ... restart gateway`
   (LiteLLM reads config only at startup — no hot reload by design).
3. Verify: `curl http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY"`.

---

## 2. How to remove a provider

1. UI → Providers → toggle the provider **off** (or delete it).
2. If editing manually: remove it from `providers:` and from every
   `fallback_chain:` that references it, then `restart gateway`.
3. Confirm removal: `docker exec docker-redis-1 redis-cli --scan --pattern 'gateway:model:<provider>*'`
   — stale score/circuit keys expire on their own TTLs; no manual cleanup needed.

⚠️ Removing a provider that is the ONLY member of a fallback chain will make
that virtual model return 503 (`all_free_models_exhausted`).

---

## 3. How to reset a stuck circuit breaker

Circuit state lives in Redis: `gateway:model:{model}:circuit`
(absent = closed).

```bash
# Inspect state + failure count + cooldowns
docker exec docker-redis-1 redis-cli get gateway:model:alpha-primary:circuit
docker exec docker-redis-1 redis-cli get gateway:model:alpha-primary:failure_count
docker exec docker-redis-1 redis-cli keys 'gateway:model:alpha-primary:cooldown:*'

# Reset one model to closed and clear its counters
docker exec docker-redis-1 redis-cli del \
  gateway:model:alpha-primary:circuit \
  gateway:model:alpha-primary:failure_count \
  gateway:model:alpha-primary:cooldown:rate_limit \
  gateway:model:alpha-primary:cooldown:server_error \
  gateway:model:alpha-primary:cooldown:auth
```

The next successful request re-seeds the score automatically. The brain's own
recovery path (`transition_to_closed`) does the same thing when a probe succeeds.

**Provider-level flag:** if the whole provider was deprioritized
(3+ model circuits opened within 5 min), clear it with:

```bash
docker exec docker-redis-1 redis-cli del \
  gateway:provider:{provider}:priority \
  gateway:provider:{provider}:circuit_open_events
```

Or programmatically: `ProviderCircuitManager(redis).clear_provider_flag("{provider}")`.

---

## 4. How to clear the request log

Request logs are Postgres rows in `request_logs`; retention is automatic
(aggregator deletes rows older than 10 days every hour). To force-clear now:

```bash
docker exec docker-postgres-1 psql -U llm_gateway -d llm_gateway -c \
  "DELETE FROM request_logs WHERE timestamp < NOW() - INTERVAL '1 day';"
```

Hourly/daily aggregates are separate tables (`model_stats_hourly`,
`model_stats_daily`) — clearing request_logs does not touch them.

---

## 5. How to upgrade LiteLLM version

1. Check the pinned version in `requirements.txt` (`litellm[proxy]==x.y.z`).
2. Review LiteLLM's release notes for breaking changes in:
   `custom_callbacks` / CustomLogger dispatch, router settings, proxy CLI flags.
3. Bump the pin, then rebuild and smoke-test:

   ```bash
   docker compose ... build gateway
   docker compose ... up -d gateway
   # Smoke: health + one real request
   curl http://localhost:4000/health/liveliness
   ```

4. Watch the first minute of logs for callback-registration failures — a
   silent "no logs written" symptom means the CustomLogger inheritance broke
   in the new version (known pitfall; see the dependency audit notes).
5. Run the unit suite before deploying further: `python -m pytest tests/unit -q`.

Rollback: revert the pin and rebuild.

---

## 6. How to restore from backup

Backups are tar.gz archives produced by the config backup CLI:

```bash
# Create a backup
python scripts/config_backup.py export --out gateway-backup.tar.gz

# Restore into this instance (prompts for confirmation)
python scripts/config_backup.py import gateway-backup.tar.gz

# Non-interactive (CI/scripted restore)
python scripts/config_backup.py import gateway-backup.tar.gz --yes
```

Import validates the archived `gateway_config.yaml` against the GatewayConfig
schema BEFORE writing anything — a malformed archive aborts with the existing
config untouched. The registry is restored as an exact snapshot: rows present
in the archive are upserted, rows absent are deleted.

**Full disaster recovery** (fresh machine): install Docker Desktop / Engine,
clone the repo, create `.env` (wizard or manual, `chmod 600`), then
`import` the backup and `docker compose up -d`.

---

## Appendix A — Health & diagnostics quick reference

| What | Where |
|---|---|
| Gateway health | `GET http://localhost:4000/health/liveliness` |
| Adapter health | `GET http://localhost:4001/health` |
| UI health | `GET http://localhost:4002/health` |
| Prometheus metrics | `GET http://localhost:4003/metrics` |
| Live routing state | Redis: `gateway:model:*` keys |
| Request metadata log | Postgres `request_logs` table |
| Aggregated stats | `model_stats_hourly` / `model_stats_daily` |

## Appendix B — Common symptoms

| Symptom | Likely cause | Fix |
|---|---|---|
| 200s but zero rows in request_logs | CustomLogger not registered | check `litellm_settings.callbacks` in generated config |
| `No api key passed in` auth errors | adapter forwarding without master key | verify `GATEWAY_API_KEY` set on adapter service |
| Model never routed despite healthy | circuit open | see §3 reset procedure |
| All requests 503 `all_free_models_exhausted` | every chain member circuited/offline | inspect `gateway:model:*:circuit`, `gateway:offline_mode` |
| `.env` warning at boot | file group/world readable | `chmod 600 .env` |
